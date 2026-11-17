"""Authenticated multi-account API, using the existing comic-generation pipeline."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from core.comic.locks import LockBusyError
from web import server as legacy
from web.queue import Scheduler
from web.settings import Settings
from web.store import Problem, Store, digest

COOKIE = "sorvflow_session"
WEB = Path(__file__).resolve().parent
ROOT = WEB.parent


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Input):
    username: str = Field(max_length=32)
    password: str = Field(max_length=256)


class NewUser(Login):
    role: str = "user"


class ChangePassword(Input):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class ResetPassword(Input):
    password: str = Field(min_length=10, max_length=256)


class Active(Input):
    active: bool


class Generate(Input):
    text: str = ""
    format: str = "webtoon"
    style_guide: str = Field(default="", max_length=20000)
    project_id: str | None = None


class Regen(Input):
    format: str = "webtoon"
    style_guide: str = ""
    stale: bool = False
    keys: list[str] = Field(default_factory=list, max_length=2000)


class Review(Input):
    action: str
    new_name: str
    candidate: str


class Confirm(Input):
    character: str
    pages: list[str] | None = None


class Pick(Input):
    choice: str = Field(max_length=16)
    why: str = Field(min_length=1, max_length=5000)


class Compose(Input):
    merge_from: str | None = Field(default=None, max_length=16)
    components: list[str] = Field(default_factory=list, max_length=20)


async def body(request: Request, model: type[BaseModel] | None = None):
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise Problem(415, "请使用 application/json 请求。")
    chunks, length = [], 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > request.app.state.settings.max_body_bytes:
            raise Problem(413, "正文过大，请分章节提交。")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise Problem(400, "请求不是有效的 JSON。") from exc
    if not isinstance(value, dict):
        raise Problem(400, "请求须为 JSON 对象。")
    return model.model_validate(value) if model else value


def current_user(request: Request, *, admin: bool = False, allow_change: bool = False) -> dict:
    session = request.state.session
    if not session:
        raise Problem(401, "请先登录。")
    user = session["user"]
    if user["must_change_password"] and not allow_change:
        raise Problem(403, "首次登录请先修改密码。")
    if admin and user["role"] != "admin":
        raise Problem(403, "此操作需要管理员权限。")
    return user


def check_origin(request: Request, settings: Settings) -> None:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise Problem(403, "不允许跨站请求。")
    expected = settings.public_url
    if not expected:
        if not legacy.is_loopback_hostname(request.url.hostname or ""):
            raise Problem(403, "请先设置 SORVFLOW_PUBLIC_URL 为网站 HTTPS 地址。")
        expected = str(request.base_url).rstrip("/")
    origin = request.headers.get("origin")
    if origin and origin != expected:
        raise Problem(403, "请求来源与网站地址不一致。")


def owned_directory(store: Store, user_id: str, slug: str) -> Path:
    store.project(user_id, slug)
    base = (store.settings.workspace(user_id) / "projects").resolve()
    path = (base / slug).resolve()
    if path.parent != base:
        raise Problem(404, "项目不存在。")
    return path


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings)
    scheduler = Scheduler(store)

    @asynccontextmanager
    async def lifespan(_app):
        if settings.start_scheduler:
            await run_in_threadpool(scheduler.start)
        try:
            yield
        finally:
            if settings.start_scheduler:
                await run_in_threadpool(scheduler.stop)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.settings, app.state.scheduler = store, settings, scheduler

    @app.exception_handler(Problem)
    async def problem_handler(_request, exc: Problem):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(ValidationError)
    async def validation_handler(_request, _exc):
        return JSONResponse({"error": "请求字段格式错误，请检查输入。"}, status_code=422)

    @app.exception_handler(FileNotFoundError)
    async def missing_handler(_request, _exc):
        return JSONResponse({"error": "项目文件不存在。"}, status_code=404)

    @app.exception_handler(ValueError)
    async def value_handler(_request, exc):
        return JSONResponse({"error": str(exc)[:500]}, status_code=400)

    @app.exception_handler(FileExistsError)
    async def exists_handler(_request, _exc):
        return JSONResponse({"error": "输出已存在，请先检查已有结果。"}, status_code=409)

    @app.exception_handler(LockBusyError)
    async def busy_handler(_request, _exc):
        return JSONResponse({"error": "项目正在运行，请稍后再操作。"}, status_code=409)

    @app.middleware("http")
    async def access(request: Request, call_next):
        try:
            request.state.session = await run_in_threadpool(
                store.session, request.cookies.get(COOKIE, "")
            )
            if request.url.path.startswith(("/api/", "/files/")):
                check_origin(request, settings)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                raw_length = request.headers.get("content-length", "0")
                if not raw_length.isdigit():
                    raise Problem(400, "Content-Length 无效。")
                if int(raw_length) > settings.max_body_bytes:
                    raise Problem(413, "正文过大，请分章节提交。")
                if request.url.path != "/api/auth/login":
                    current_user(request, allow_change=True)
                    presented = request.headers.get("x-csrf-token", "")
                    if not hmac.compare_digest(presented, request.state.session["csrf"]):
                        raise Problem(403, "登录状态已更新，请刷新页面后重试。")
            response = await call_next(request)
        except Problem as exc:
            response = JSONResponse({"error": exc.message}, status_code=exc.status)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = legacy._CONTENT_SECURITY_POLICY
        if request.url.path.startswith(("/api/", "/files/")) or request.url.path in {
            "/",
            "/index.html",
        }:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie"
        return response

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "key": legacy._providers_configured(),
            "accounts": True,
            "last_check": store.metadata("provider_check"),
        }

    @app.get("/api/auth/session")
    def session(request: Request):
        current = request.state.session
        return {
            "authenticated": bool(current),
            "setup_required": not store.has_users(),
            **(current or {}),
        }

    @app.post("/api/auth/login")
    async def login(request: Request):
        value = await body(request, Login)
        remote = request.client.host if request.client else "unknown"
        token, current = await run_in_threadpool(
            store.login, value.username, value.password, remote
        )
        response = JSONResponse({"authenticated": True, **current})
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
            max_age=settings.session_hours * 3600,
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        store.logout(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            COOKIE, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict"
        )
        return response

    @app.post("/api/auth/password")
    async def change_password(request: Request):
        user = current_user(request, allow_change=True)
        value = await body(request, ChangePassword)
        await run_in_threadpool(
            store.change_password, user["id"], value.current_password, value.new_password
        )
        response = JSONResponse({"ok": True, "message": "密码已更新，请重新登录。"})
        response.delete_cookie(
            COOKIE, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/admin/users")
    def users(request: Request):
        current_user(request, admin=True)
        return store.users()

    @app.post("/api/admin/users")
    async def add_user(request: Request):
        current_user(request, admin=True)
        value = await body(request, NewUser)
        return await run_in_threadpool(
            store.create_user, value.username, value.password, role=value.role
        )

    @app.post("/api/admin/users/{user_id}/active")
    async def active(user_id: str, request: Request):
        actor = current_user(request, admin=True)
        value = await body(request, Active)
        store.set_active(user_id, value.active, actor["id"])
        return {"ok": True}

    @app.post("/api/admin/users/{user_id}/password")
    async def reset_password(user_id: str, request: Request):
        current_user(request, admin=True)
        value = await body(request, ResetPassword)
        await run_in_threadpool(store.reset_password, user_id, value.password)
        return {"ok": True}

    @app.post("/api/admin/api-check")
    async def api_check(request: Request):
        current_user(request, admin=True)
        await body(request)
        if not legacy._providers_configured():
            raise Problem(400, "请先配置文本和图片接口凭据。")
        started = time.time()
        try:
            provider = legacy.get_chat_provider()
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "health_check",
                        "description": "Return status",
                        "parameters": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                        },
                    },
                }
            ]
            result = await asyncio.wait_for(
                provider.chat_function_call(
                    [{"role": "user", "content": "Call health_check with ok=true."}],
                    tools,
                    {"type": "function", "function": {"name": "health_check"}},
                    max_retries=1,
                ),
                timeout=45,
            )
            if result.get("ok") is not True:
                raise ValueError("Invalid tool response")
            outcome = {
                "ok": True,
                "checked_at": started,
                "kind": "text",
                "message": "文本接口工具调用成功；图片接口尚未实测。",
            }
        except Exception:
            outcome = {
                "ok": False,
                "checked_at": started,
                "kind": "text",
                "message": "文本接口检测失败，请核对模型、工具调用支持和供应商状态。",
            }
        store.metadata("provider_check", outcome)
        return outcome

    @app.post("/api/generate")
    async def generate(request: Request):
        user = current_user(request)
        value = await body(request, Generate)
        if not legacy._providers_configured():
            raise Problem(503, "生成服务尚未配置完整，请联系管理员。")
        job, created = store.enqueue(
            user["id"],
            value.text,
            value.project_id,
            value.style_guide,
            value.format if value.format in {"page", "webtoon"} else "webtoon",
        )
        return {
            "job_id": job["id"],
            "project_id": json.loads(job["request_json"])["slug"],
            "status": job["status"],
            "created": created,
        }

    @app.get("/api/jobs")
    def jobs(request: Request):
        return store.jobs(current_user(request)["id"])

    @app.get("/api/job/{job_id}")
    def job(job_id: str, request: Request):
        return store.snapshot(store.owned_job(current_user(request)["id"], job_id))

    @app.post("/api/job/{job_id}/stop")
    async def stop(job_id: str, request: Request):
        store.stop(current_user(request)["id"], job_id)
        return {"ok": True}

    @app.get("/api/projects")
    def projects(request: Request):
        return [
            {"id": item["slug"], "created_at": item["created_at"]}
            for item in store.projects(current_user(request)["id"])
        ]

    @app.get("/api/project/{slug}")
    def project(slug: str, request: Request):
        item = store.project(current_user(request)["id"], slug)
        return {"project_id": slug, "text": item["text"], "style_guide": item["style"]}

    def experiment_call(user_id: str, function, *args, mutate: bool = False, **kwargs):
        root = settings.workspace(user_id) / "experiments"
        with legacy.experiment_scope(root):
            if not mutate:
                return function(*args, **kwargs)
            directory = legacy._experiment_dir(args[0])
            if not directory.is_dir():
                raise Problem(404, "方案不存在。")
            try:
                with legacy.exclusive_lock(directory / ".web.lock", blocking=False):
                    return function(*args, **kwargs)
            except LockBusyError as exc:
                raise Problem(409, "方案正在处理，请稍后重试。") from exc

    @app.get("/api/experiments")
    def experiments(request: Request):
        return experiment_call(current_user(request)["id"], legacy.list_experiments)

    @app.get("/api/sequences")
    def sequences(request: Request):
        return experiment_call(current_user(request)["id"], legacy.list_sequences)

    @app.get("/api/experiment/{experiment_id}")
    def experiment(experiment_id: str, request: Request):
        return experiment_call(current_user(request)["id"], legacy.experiment_detail, experiment_id)

    @app.get("/api/sequence/{sequence_id}")
    def sequence(sequence_id: str, request: Request):
        return experiment_call(current_user(request)["id"], legacy.sequence_detail, sequence_id)

    @app.post("/api/experiment/{experiment_id}/pick")
    async def pick_experiment(experiment_id: str, request: Request):
        user, value = current_user(request), await body(request, Pick)
        return await run_in_threadpool(
            experiment_call,
            user["id"],
            legacy.pick_experiment_api,
            experiment_id,
            value.choice,
            value.why,
            mutate=True,
        )

    @app.post("/api/sequence/{sequence_id}/pick")
    async def pick_sequence(sequence_id: str, request: Request):
        user, value = current_user(request), await body(request, Pick)
        return await run_in_threadpool(
            experiment_call,
            user["id"],
            legacy.pick_sequence_api,
            sequence_id,
            value.choice,
            value.why,
            mutate=True,
        )

    @app.post("/api/experiment/{experiment_id}/compose")
    async def compose(experiment_id: str, request: Request):
        user, value = current_user(request), await body(request, Compose)
        return await run_in_threadpool(
            experiment_call,
            user["id"],
            legacy.compose_experiment_api,
            experiment_id,
            merge_from=value.merge_from,
            components=value.components,
            mutate=True,
        )

    @app.post("/api/sequence/{sequence_id}/advance")
    async def advance(sequence_id: str, request: Request):
        user, value = current_user(request), await body(request, Compose)
        detail = experiment_call(user["id"], legacy.sequence_detail, sequence_id)
        if not legacy._providers_configured():
            raise Problem(503, "模型凭据未配置完整。")
        job, _ = store.enqueue(
            user["id"],
            "Sequence: " + sequence_id,
            "sequence-" + digest(sequence_id)[:24],
            "",
            "page",
            kind="sequence",
            options={
                "sequence_id": sequence_id,
                "round": detail["current_round"],
                **value.model_dump(),
            },
        )
        return {"job_id": job["id"], "status": job["status"]}

    @app.post("/api/project/{slug}/review")
    async def review(slug: str, request: Request):
        directory = owned_directory(store, current_user(request)["id"], slug)
        value = await body(request, Review)
        result = await run_in_threadpool(
            legacy.apply_review,
            slug,
            value.action,
            value.new_name,
            value.candidate,
            out_dir=directory,
        )
        return {"project_id": slug, **result}

    @app.post("/api/project/{slug}/ledger/confirm")
    async def confirm(slug: str, request: Request):
        directory = owned_directory(store, current_user(request)["id"], slug)
        value = await body(request, Confirm)
        try:
            result = await run_in_threadpool(
                legacy.apply_ledger_confirm, slug, value.character, value.pages, out_dir=directory
            )
        except KeyError as exc:
            raise Problem(404, "角色不存在。") from exc
        return {"project_id": slug, **result}

    @app.post("/api/project/{slug}/regen")
    async def regen(slug: str, request: Request):
        user = current_user(request)
        directory = owned_directory(store, user["id"], slug)
        value = await body(request, Regen)
        if not (directory / "state.json").is_file():
            raise Problem(404, "项目尚未生成检查点。")
        if not legacy._providers_configured():
            raise Problem(503, "模型凭据未配置完整。")
        if not value.stale and not value.keys:
            raise Problem(400, "请选择需要重绘的画面。")
        job, created = store.enqueue(
            user["id"],
            "",
            slug,
            value.style_guide,
            value.format if value.format in {"page", "webtoon"} else "webtoon",
            kind="regen",
            options={"stale": value.stale, "keys": value.keys},
        )
        return {
            "job_id": job["id"],
            "project_id": slug,
            "created": created,
            "status": job["status"],
        }

    @app.get("/files/{relative:path}")
    def files(relative: str, request: Request):
        user = current_user(request)
        if not legacy._public_artifact_rel(relative):
            raise Problem(404, "文件不存在。")
        slug = relative.split("/", 1)[0]
        directory = owned_directory(store, user["id"], slug)
        root = (settings.workspace(user["id"]) / "projects").resolve()
        target = (root / relative).resolve()
        if not legacy._is_within(target, directory) or not legacy._public_artifact_name(
            target.name
        ):
            raise Problem(404, "文件不存在。")
        if target.name == "reader.html" and legacy.chapter_pages(directory / "pages"):
            legacy.write_reader(directory)
        if not target.is_file():
            raise Problem(404, "文件不存在。")
        return FileResponse(target)

    @app.get("/assets/{relative:path}")
    def assets(relative: str):
        root = ROOT / "assets"
        path = (root / relative).resolve()
        if not legacy._is_within(path, root) or not path.is_file():
            raise Problem(404, "文件不存在。")
        return FileResponse(path)

    @app.get("/{path:path}")
    def static(path: str):
        allowed = {
            "": "index.html",
            "index.html": "index.html",
            "i18n.js": "i18n.js",
            "locales/zh-CN.js": "locales/zh-CN.js",
            "accounts.js": "accounts.js",
            "accounts.css": "accounts.css",
        }
        if path not in allowed:
            raise Problem(404, "页面不存在。")
        return FileResponse(WEB / allowed[path])

    return app
