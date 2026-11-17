"""Multi-account boundaries and persisted, bounded job scheduling (no provider calls)."""

import json
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from web import server
from web.app import COOKIE, create_app
from web.queue import Scheduler
from web.runtime import bind_available
from web.settings import Settings
from web.store import Problem

PASSWORD = "local-test-password-42"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_providers_configured", lambda: True)
    settings = Settings(state_dir=tmp_path / "state", start_scheduler=False, workers=2)
    app = create_app(settings)
    store = app.state.store
    users = {
        name: store.create_user(
            name, PASSWORD, role="admin" if name == "admin" else "user", must_change=False
        )
        for name in ("admin", "alice", "bobby")
    }
    return app, store, users


def signed(app, name):
    client = TestClient(app, base_url="http://localhost")
    response = client.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return client


def enqueue(store, user, slug="story"):
    return store.enqueue(user["id"], "山巅上，两位剑修相遇。", slug, "国漫", "page")[0]


def test_login_session_csrf_origin_and_logout(setup):
    app, store, _ = setup
    anonymous = TestClient(app, base_url="http://localhost")
    assert anonymous.get("/api/health").json()["accounts"] is True
    assert anonymous.get("/api/jobs").status_code == 401
    alice = signed(app, "alice")
    cookie = alice.cookies[COOKIE]
    assert store.session(cookie)["user"]["username"] == "alice"
    with store.connection() as db:
        assert db.execute("SELECT token_hash FROM sessions").fetchone()[0] != cookie
    assert alice.get("/api/auth/session").json()["authenticated"]
    assert (
        alice.post(
            "/api/generate", json={"text": "x"}, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        alice.post(
            "/api/generate", json={"text": "x"}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert alice.get("/api/jobs", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert alice.post("/api/auth/logout", json={}).status_code == 200
    assert alice.get("/api/jobs").status_code == 401
    assert store.session(cookie) is None


def test_public_origin_secure_cookie_and_strict_body(setup):
    _, store, _ = setup
    app = create_app(replace(store.settings, public_url="https://comic.example"))
    client = TestClient(app, base_url="https://comic.example")
    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    assert (
        client.post(
            "/api/generate", json={"text": "一场相遇"}, headers={"Origin": "https://comic.example"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/generate", json={"text": "x"}, headers={"Origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/generate", content="{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert client.post("/api/generate", json={"text": 42}).status_code == 422
    assert client.post("/api/generate", json={"text": "x" * (8 * 1024 * 1024)}).status_code == 413
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/.env").status_code == 404


def test_admin_create_force_change_disable_and_reset(setup):
    app, _, _ = setup
    admin, alice = signed(app, "admin"), signed(app, "alice")
    assert alice.get("/api/admin/users").status_code == 403
    created = admin.post("/api/admin/users", json={"username": "newuser", "password": PASSWORD})
    assert created.status_code == 200
    new_id = created.json()["id"]
    newcomer = signed(app, "newuser")
    assert newcomer.get("/api/jobs").status_code == 403
    response = newcomer.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD + "changed"},
    )
    assert response.status_code == 200
    assert newcomer.get("/api/jobs").status_code == 401
    assert (
        admin.post(f"/api/admin/users/{new_id}/password", json={"password": PASSWORD}).status_code
        == 200
    )
    newcomer = signed(app, "newuser")
    assert newcomer.get("/api/auth/session").json()["user"]["must_change_password"]
    assert (
        admin.post(f"/api/admin/users/{new_id}/active", json={"active": False}).status_code == 200
    )
    assert newcomer.get("/api/jobs").status_code == 401
    assert (
        newcomer.post(
            "/api/auth/login", json={"username": "newuser", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        admin.post(
            f"/api/admin/users/{admin.get('/api/auth/session').json()['user']['id']}/active",
            json={"active": False},
        ).status_code
        == 400
    )


def test_account_password_hashes_and_login_throttle(setup):
    _, store, _ = setup
    with store.connection() as db:
        assert (
            db.execute("SELECT password_hash FROM users LIMIT 1")
            .fetchone()[0]
            .startswith("$argon2id$")
        )
    for _ in range(5):
        with pytest.raises(Problem) as error:
            store.login("alice", "incorrect", "127.0.0.1")
        assert error.value.status == 401
    with pytest.raises(Problem) as error:
        store.login("alice", PASSWORD, "127.0.0.1")
    assert error.value.status == 429
    assert store.login("bobby", PASSWORD, "127.0.0.2")[1]["user"]["username"] == "bobby"


def test_reset_during_password_verification_cannot_issue_session(setup, monkeypatch):
    _, store, users = setup
    import web.store as module

    original = module.verify_password

    def racing(encoded, password):
        store.reset_password(users["alice"]["id"], PASSWORD + "new")
        return original(encoded, password)

    monkeypatch.setattr(module, "verify_password", racing)
    with pytest.raises(Problem):
        store.login("alice", PASSWORD, "127.0.0.1")
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_private_sources_files_jobs_reviews_and_regeneration(setup):
    app, store, users = setup
    job = enqueue(store, users["alice"])
    root = store.settings.workspace(users["alice"]["id"]) / "projects" / "story"
    root.mkdir(parents=True)
    (root / "page.png").write_bytes(b"private comic")
    (root / "state.json").write_text("secret manuscript")
    (root / "leak.png").symlink_to(store.path)
    alice, bobby = signed(app, "alice"), signed(app, "bobby")
    assert alice.get("/api/project/story").json()["text"] == "山巅上，两位剑修相遇。"
    assert alice.get("/files/story/page.png").content == b"private comic"
    assert alice.get("/files/story/state.json").status_code == 404
    assert alice.get("/files/story/leak.png").status_code == 404
    assert bobby.get("/api/projects").json() == []
    assert bobby.get("/api/jobs").json() == []
    for url in (f"/api/job/{job['id']}", "/api/project/story", "/files/story/page.png"):
        assert bobby.get(url).status_code == 404, url
    for url, payload in (
        (f"/api/job/{job['id']}/stop", {}),
        ("/api/project/story/regen", {"keys": ["p1"]}),
        ("/api/project/story/review", {"action": "merge", "new_name": "a", "candidate": "b"}),
        ("/api/project/story/ledger/confirm", {"character": "a"}),
    ):
        assert bobby.post(url, json=payload).status_code == 404, url
    # The same public slug can safely exist for both users.
    own = enqueue(store, users["bobby"])
    assert own["project_id"] != job["project_id"]
    assert bobby.get("/files/story/page.png").status_code == 404
    assert alice.get(f"/api/job/{job['id']}").headers["cache-control"] == "no-store"


def test_experiments_are_scoped_to_user_even_in_parallel(setup):
    app, store, users = setup
    root = store.settings.workspace(users["alice"]["id"]) / "experiments" / "ab1"
    root.mkdir(parents=True)
    (root / "experiment.json").write_text(json.dumps({"status": "planned", "candidates": {}}))
    alice, bobby = signed(app, "alice"), signed(app, "bobby")

    def request(client):
        return client.get("/api/experiments").json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(request, [alice, bobby]))
    assert first[0]["id"] == "ab1"
    assert second == []
    assert bobby.get("/api/experiment/ab1").status_code == 404
    assert bobby.get("/api/sequence/ab1").status_code == 404
    assert (
        bobby.post("/api/experiment/ab1/pick", json={"choice": "A", "why": "好"}).status_code == 404
    )


def test_duplicate_submission_is_atomic_and_limits_fair_claims(setup):
    _, store, users = setup
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: enqueue(store, users["alice"]), range(12)))
    assert len({job["id"] for job in jobs}) == 1
    for index in range(4):
        enqueue(store, users["alice"], f"a{index}")
    with pytest.raises(Problem) as error:
        enqueue(store, users["alice"], "too-many")
    assert error.value.status == 429
    other = enqueue(store, users["bobby"])
    claimed = store.claim()
    assert claimed["user_id"] == users["alice"]["id"]
    assert store.claim()["id"] == other["id"]  # skip blocked user's queue
    assert store.claim() is None
    with pytest.raises(Problem):
        store.enqueue(users["alice"]["id"], "different text", "story", "国漫", "page")


def test_queued_cancel_recovery_and_stale_lease_fencing(setup):
    _, store, users = setup
    cancelled = enqueue(store, users["alice"], "cancel")
    store.stop(users["alice"]["id"], cancelled["id"])
    assert store.claim() is None
    original = enqueue(store, users["alice"])
    first = store.claim()
    store.append_log(first["id"], first["lease"], "private-log")
    store.recover()
    second = store.claim()
    assert second["id"] == original["id"]
    assert second["lease"] != first["lease"]
    assert not store.update_job(first["id"], first["lease"], status="done")
    store.append_log(first["id"], first["lease"], "stale-log")
    assert store.snapshot(second)["log"] == ["private-log"]
    store.stop(users["alice"]["id"], second["id"])
    store.recover()
    assert store.owned_job(users["alice"]["id"], second["id"])["status"] == "cancelled"
    assert store.claim() is None


def test_disable_cancels_queue_and_running_work(setup):
    _, store, users = setup
    running = enqueue(store, users["alice"])
    store.claim()
    waiting = enqueue(store, users["alice"], "waiting")
    store.set_active(users["alice"]["id"], False, users["admin"]["id"])
    assert store.owned_job(users["alice"]["id"], running["id"])["cancel_requested"]
    assert store.owned_job(users["alice"]["id"], waiting["id"])["status"] == "cancelled"


def test_only_one_scheduler_can_use_a_database(setup):
    _, store, _ = setup
    one, two = Scheduler(store), Scheduler(store)
    one.start()
    try:
        with pytest.raises(RuntimeError):
            two.start()
    finally:
        one.stop()
    two.start()
    two.stop()


def test_bind_skips_occupied_port_and_releases_socket():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        free = bind_available("127.0.0.1", port, min(port + 50, 65535))
        try:
            assert free.getsockname()[1] > port
        finally:
            free.close()
        with pytest.raises(RuntimeError):
            bind_available("127.0.0.1", port, port)
    with pytest.raises(ValueError):
        bind_available("127.0.0.1", 80, 100)


def test_worker_uses_private_paths_and_logs_without_model_calls(setup, monkeypatch, tmp_path):
    _, store, users = setup
    import core.pipelines.run_until_complete as pipeline
    from web.worker import execute

    jobs = [enqueue(store, users[name]) for name in ("alice", "bobby")]
    for key in ("INKSTONE_DATA_DIR", "INKSTONE_EXPERIMENTS_DIR", "INKSTONE_ERROR_LOG"):
        monkeypatch.setenv(key, "test-before-worker")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "OUTPUT_DIR", tmp_path / "before")
    monkeypatch.setattr(
        server, "_fill_job_from_project", lambda payload, result: payload.update(result)
    )
    seen = []

    async def fake_pipeline(text, **kwargs):
        seen.append((os.getcwd(), os.environ["INKSTONE_DATA_DIR"], kwargs["output_dir"]))
        logging.info("isolated log %s", len(seen))
        kwargs["progress_callback"]("panels", 0.5)
        assert kwargs["cancel_check"]() is False
        return {"panels": []}

    monkeypatch.setattr(pipeline, "run_until_complete", fake_pipeline)
    old_mask = os.umask(0o027)
    old_level = logging.getLogger().level
    try:
        for _ in jobs:
            claim = store.claim()
            execute(store, claim["id"], claim["lease"], threading.Event())
    finally:
        os.umask(old_mask)
        logging.getLogger().setLevel(old_level)
    for index, (name, job) in enumerate(zip(("alice", "bobby"), jobs, strict=True)):
        workspace = str(store.settings.workspace(users[name]["id"]))
        assert seen[index][0] == workspace
        assert seen[index][1].startswith(workspace)
        assert seen[index][2].startswith(workspace)
        result = store.snapshot(store.owned_job(users[name]["id"], job["id"]))
        assert result["status"] == "done"
        assert result["log"] == [f"isolated log {index + 1}"]
