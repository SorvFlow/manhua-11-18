"""SQLite accounts, sessions, owned projects and a transactionally claimed queue."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from web.settings import Settings

PASSWORDS = PasswordHasher(time_cost=2, memory_cost=32768, parallelism=2)
_DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(24))
_USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}$")
_PROJECT = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
ACTIVE = ("queued", "running")


class Problem(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def public_user(row: dict) -> dict:
    return {key: row[key] for key in ("id", "username", "role", "active", "must_change_password")}


def password_hash(password: str) -> str:
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise Problem(400, "密码长度须为 10～256 个字符。")
    return PASSWORDS.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    if not isinstance(password, str) or len(password) > 256:
        return False
    try:
        return PASSWORDS.verify(encoded, password)
    except VerificationError:
        return False


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.state_dir / "accounts.sqlite3"
        settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_limits (
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL, started_at REAL NOT NULL,
                    blocked_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    slug TEXT NOT NULL, text TEXT NOT NULL, style TEXT NOT NULL,
                    created_at REAL NOT NULL, UNIQUE(user_id, slug)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    user_id TEXT NOT NULL REFERENCES users(id), status TEXT NOT NULL,
                    request_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    lease TEXT, pid INTEGER, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job
                    ON jobs(project_id) WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS owner_jobs ON jobs(user_id, created_at);
                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id), message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS log_job ON job_logs(job_id, id);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self, *, write: bool = False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def has_users(self) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def create_user(
        self, username: str, password: str, *, role: str = "user", must_change: bool = True
    ) -> dict:
        if not isinstance(username, str) or not _USERNAME.fullmatch(username):
            raise Problem(400, "账号须为 3～32 位英文字母、数字、下划线、点或短横线。")
        if role not in {"user", "admin"}:
            raise Problem(400, "账号角色无效。")
        encoded = password_hash(password)
        user_id = uuid.uuid4().hex
        try:
            with self.connection(write=True) as db:
                db.execute(
                    "INSERT INTO users VALUES (?,?,?,?,1,?,?)",
                    (user_id, username.lower(), encoded, role, int(must_change), time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise Problem(409, "账号已存在。") from exc
        return self.user(user_id)

    def user(self, user_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise Problem(404, "账号不存在。")
        return public_user(dict(row))

    def users(self) -> list[dict]:
        with self.connection() as db:
            return [
                public_user(dict(row))
                for row in db.execute("SELECT * FROM users ORDER BY created_at")
            ]

    def login(self, username: str, password: str, remote: str) -> tuple[str, dict]:
        username = username.lower() if isinstance(username, str) else ""
        now = time.time()
        keys = (digest("user:" + username), digest("remote:" + remote))
        with self.connection() as db:
            for key in keys:
                limit = db.execute(
                    "SELECT blocked_until FROM login_limits WHERE key=?", (key,)
                ).fetchone()
                if limit and limit[0] > now:
                    raise Problem(429, "登录尝试过多，请 15 分钟后再试。")
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = verify_password(row["password_hash"] if row else _DUMMY_HASH, password)
        if not row or not valid or not row["active"]:
            with self.connection(write=True) as db:
                for key, maximum in zip(keys, (5, 20), strict=True):
                    old = db.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
                    started = old["started_at"] if old and now - old["started_at"] < 900 else now
                    count = old["count"] + 1 if old and started != now else 1
                    db.execute(
                        "INSERT OR REPLACE INTO login_limits VALUES (?,?,?,?)",
                        (key, count, started, now + 900 if count >= maximum else 0),
                    )
            raise Problem(401, "账号或密码错误，或账号已停用。")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            # Password resets and disabling must win races with verification.
            current = db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
            if not current["active"] or current["password_hash"] != row["password_hash"]:
                raise Problem(401, "登录状态已变化，请重新登录。")
            db.execute("DELETE FROM login_limits WHERE key=?", (keys[0],))
            db.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (digest(token), row["id"], csrf, now + self.settings.session_hours * 3600),
            )
        return token, {"user": public_user(dict(current)), "csrf": csrf}

    def session(self, token: str) -> dict | None:
        if not token or len(token) > 200:
            return None
        with self.connection() as db:
            row = db.execute(
                "SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                (digest(token), time.time()),
            ).fetchone()
        return {"user": public_user(dict(row)), "csrf": row["csrf"]} if row else None

    def logout(self, token: str) -> None:
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def change_password(self, user_id: str, old: str, new: str) -> None:
        with self.connection() as db:
            row = db.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or not verify_password(row[0], old):
            raise Problem(400, "当前密码不正确。")
        encoded = password_hash(new)
        with self.connection(write=True) as db:
            changed = db.execute(
                "UPDATE users SET password_hash=?,must_change_password=0 "
                "WHERE id=? AND password_hash=? AND active=1",
                (encoded, user_id, row[0]),
            ).rowcount
            if not changed:
                raise Problem(409, "密码或账号状态已变化，请重新登录。")
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def reset_password(self, user_id: str, password: str, *, must_change: bool = True) -> None:
        encoded = password_hash(password)
        with self.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise Problem(404, "账号不存在。")
            db.execute(
                "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
                (encoded, int(must_change), user_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def set_active(self, user_id: str, active: bool, actor_id: str) -> None:
        if user_id == actor_id and not active:
            raise Problem(400, "不能停用当前管理员账号。")
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise Problem(404, "账号不存在。")
            if not active and row["role"] == "admin":
                count = db.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
                ).fetchone()[0]
                if count <= 1:
                    raise Problem(400, "至少需要保留一个启用的管理员。")
            db.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                db.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=?, "
                    "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END "
                    "WHERE user_id=? AND status IN ('queued','running')",
                    (now, user_id),
                )

    def project(self, user_id: str, slug: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE user_id=? AND slug=?", (user_id, slug)
            ).fetchone()
        if row is None:
            raise Problem(404, "项目不存在。")
        return dict(row)

    def projects(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, slug, created_at FROM projects WHERE user_id=? "
                    "ORDER BY created_at DESC",
                    (user_id,),
                )
            ]

    def enqueue(
        self,
        user_id: str,
        text: str,
        slug: str | None,
        style: str,
        fmt: str,
        *,
        kind: str = "generate",
        options: dict | None = None,
    ) -> tuple[dict, bool]:
        slug = slug or uuid.uuid4().hex[:12]
        if not _PROJECT.fullmatch(slug):
            raise Problem(400, "项目编号须为 1～64 位英文字母、数字、下划线或短横线。")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        style = style.strip()
        now = time.time()
        with self.connection(write=True) as db:
            user = db.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
            if not user or not user[0]:
                raise Problem(401, "账号已停用。")
            project = db.execute(
                "SELECT * FROM projects WHERE user_id=? AND slug=?", (user_id, slug)
            ).fetchone()
            if project:
                if text and text != project["text"]:
                    raise Problem(409, "正文与原项目不同，请使用新的项目编号。")
                if style and style != project["style"]:
                    raise Problem(409, "画风与原项目不同，请使用新的项目编号。")
                text, style = project["text"], project["style"]
                existing = db.execute(
                    "SELECT * FROM jobs WHERE project_id=? AND status IN ('queued','running')",
                    (project["id"],),
                ).fetchone()
                if existing:
                    return dict(existing), False
            if not text:
                raise Problem(400, "请填写小说正文。")
            queued = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_id=? AND status='queued'", (user_id,)
            ).fetchone()[0]
            if queued >= self.settings.per_user_queued:
                raise Problem(429, "你的待处理任务已达到上限，请等待已有任务完成。")
            if not project:
                internal_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO projects VALUES (?,?,?,?,?,?)",
                    (internal_id, user_id, slug, text, style, now),
                )
            else:
                internal_id = project["id"]
            job_id = uuid.uuid4().hex
            request = {
                "text": text,
                "style": style,
                "format": fmt,
                "slug": slug,
                "kind": kind,
                "options": options or {},
            }
            db.execute(
                "INSERT INTO jobs "
                "(id,project_id,user_id,status,request_json,created_at,updated_at) "
                "VALUES (?,?,?,'queued',?,?,?)",
                (job_id, internal_id, user_id, json.dumps(request), now, now),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def owned_job(self, user_id: str, job_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user_id)
            ).fetchone()
        if row is None:
            raise Problem(404, "任务不存在。")
        return dict(row)

    def jobs(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            return [
                self.snapshot(dict(row), db=db, include_logs=False)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                    (user_id,),
                ).fetchall()
            ]

    def snapshot(self, job: dict, *, db=None, include_logs: bool = True) -> dict:
        if db is None:
            with self.connection() as connection:
                return self.snapshot(job, db=connection, include_logs=include_logs)
        result = json.loads(job["result_json"])
        request = json.loads(job["request_json"])
        if job["status"] == "running" and result.get("session_started_at"):
            result["elapsed_seconds"] = result.get("base_elapsed", 0) + max(
                0, time.time() - result["session_started_at"]
            )
        position = None
        if job["status"] == "queued":
            position = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='queued' "
                "AND (created_at<? OR (created_at=? AND id<=?))",
                (job["created_at"], job["created_at"], job["id"]),
            ).fetchone()[0]
        return {
            "panels": [],
            "pdf": None,
            "webtoon": None,
            "reader": None,
            "error": None,
            "progress": 0,
            "stage": job["status"],
            **result,
            "job_id": job["id"],
            "kind": request["kind"],
            "format": request["format"],
            "project_id": request["slug"],
            "status": job["status"],
            "cancel_requested": bool(job["cancel_requested"]),
            "queue_position": position,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "log": (
                [
                    r[0]
                    for r in reversed(
                        db.execute(
                            "SELECT message FROM job_logs WHERE job_id=? "
                            "ORDER BY id DESC LIMIT 200",
                            (job["id"],),
                        ).fetchall()
                    )
                ]
                if include_logs
                else []
            ),
        }

    def stop(self, user_id: str, job_id: str) -> None:
        with self.connection(write=True) as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user_id)
            ).fetchone()
            if job is None:
                raise Problem(404, "任务不存在。")
            if job["status"] in ACTIVE:
                db.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=?, "
                    "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?",
                    (time.time(), job_id),
                )

    def recover(self) -> None:
        # The scheduler's process lock must be held before this transition.
        with self.connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status="
                "CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END, "
                "lease=NULL,pid=NULL,updated_at=? WHERE status='running'",
                (time.time(),),
            )

    def claim(self) -> dict | None:
        with self.connection(write=True) as db:
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            if count >= self.settings.workers:
                return None
            row = db.execute(
                "SELECT j.* FROM jobs j JOIN users u ON u.id=j.user_id "
                "WHERE j.status='queued' AND u.active=1 AND j.cancel_requested=0 AND "
                "(SELECT COUNT(*) FROM jobs r WHERE r.user_id=j.user_id AND r.status='running')<? "
                "ORDER BY j.created_at,j.id LIMIT 1",
                (self.settings.per_user_running,),
            ).fetchone()
            if row is None:
                return None
            lease = secrets.token_hex(16)
            db.execute(
                "UPDATE jobs SET status='running',lease=?,pid=NULL,"
                "attempts=attempts+1,updated_at=? WHERE id=?",
                (lease, time.time(), row["id"]),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def worker_job(self, job_id: str, lease: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND lease=? AND status='running'", (job_id, lease)
            ).fetchone()
        return dict(row) if row else None

    def update_job(
        self,
        job_id: str,
        lease: str,
        *,
        result: dict | None = None,
        status: str | None = None,
        pid: int | None = None,
    ) -> bool:
        with self.connection(write=True) as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE id=? AND lease=? AND status='running'", (job_id, lease)
            ).fetchone()
            if job is None:
                return False
            payload = json.loads(job["result_json"])
            payload.update(result or {})
            db.execute(
                "UPDATE jobs SET result_json=?,status=?,pid=?,updated_at=? WHERE id=? AND lease=?",
                (
                    json.dumps(payload),
                    status or job["status"],
                    pid or job["pid"],
                    time.time(),
                    job_id,
                    lease,
                ),
            )
            return True

    def append_log(self, job_id: str, lease: str, message: str) -> None:
        with self.connection(write=True) as db:
            if not db.execute(
                "SELECT 1 FROM jobs WHERE id=? AND lease=? AND status='running'", (job_id, lease)
            ).fetchone():
                return
            db.execute(
                "INSERT INTO job_logs(job_id,message) VALUES (?,?)", (job_id, message[:4000])
            )
            db.execute(
                "DELETE FROM job_logs WHERE job_id=? AND id NOT IN "
                "(SELECT id FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT 500)",
                (job_id, job_id),
            )

    def metadata(self, key: str, value: dict | None = None) -> dict | None:
        with self.connection(write=value is not None) as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
