"""Web account/queue settings, separate from image-generation configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    public_url: str = ""
    workers: int = 1
    per_user_running: int = 1
    per_user_queued: int = 5
    session_hours: int = 12
    max_body_bytes: int = 8 * 1024 * 1024
    start_scheduler: bool = True

    @property
    def secure_cookie(self) -> bool:
        return self.public_url.startswith("https://")

    def workspace(self, user_id: str) -> Path:
        # User IDs come exclusively from the account database, never request paths.
        return self.state_dir / "users" / user_id

    @classmethod
    def from_env(cls) -> Settings:
        from core.config import data_dir, ui_max_body_bytes

        def integer(name: str, default: int, maximum: int) -> int:
            value = int(os.environ.get(name, str(default)))
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
            return value

        public_url = os.environ.get("SORVFLOW_PUBLIC_URL", "").strip().rstrip("/")
        if public_url:
            parsed = urlsplit(public_url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "SORVFLOW_PUBLIC_URL must be an origin, e.g. https://comic.example.com"
                )
            if parsed.scheme != "https" and parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("Public account access requires HTTPS in SORVFLOW_PUBLIC_URL")
        return cls(
            state_dir=Path(os.environ.get("SORVFLOW_STATE_DIR", str(data_dir() / "web")))
            .expanduser()
            .resolve(),
            public_url=public_url,
            workers=integer("SORVFLOW_WORKERS", 1, 8),
            per_user_running=integer("SORVFLOW_USER_RUNNING", 1, 8),
            per_user_queued=integer("SORVFLOW_USER_QUEUED", 5, 100),
            session_hours=integer("SORVFLOW_SESSION_HOURS", 12, 168),
            max_body_bytes=ui_max_body_bytes(),
        )
