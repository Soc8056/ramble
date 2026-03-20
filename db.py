"""
db.py — Async SQLite database for Ramble.

Tables:
  users    — GitHub identity + optional Vercel token
  sessions — cookie tokens (UUID) mapped to user IDs
  projects — one row per completed build, linked to a user

Uses aiosqlite so every call is awaitable without blocking the event loop.
DB file defaults to ./ramble.db — mount a Railway volume at /app to persist.
Set DB_PATH env var to override.
"""

import os
import json
import uuid
import aiosqlite
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", "ramble.db")

_CREATE_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    github_id      TEXT    UNIQUE NOT NULL,
    github_login   TEXT    NOT NULL,
    github_avatar  TEXT,
    github_token   TEXT,
    vercel_token   TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id   TEXT    UNIQUE NOT NULL,
    app_name     TEXT,
    deploy_url   TEXT,
    github_repo  TEXT,
    spec_json    TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_projects_user    ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_projects_session ON projects(session_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

def _row_to_user(row) -> dict:
    d = dict(row)
    # Normalise column name so callers use github_avatar_url everywhere
    d["github_avatar_url"] = d.pop("github_avatar", None)
    return d


# ── Init ──────────────────────────────────────────────────────────────────────

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_CREATE_SQL)
        await db.commit()
    print(f"✓ DB initialised at {DB_PATH}")


# ── Users ─────────────────────────────────────────────────────────────────────

async def upsert_user(
    github_id: str | int,
    login: str,
    avatar_url: str | None,
    github_token: str,
) -> dict:
    """Insert or update user by GitHub ID. Returns full user dict."""
    now = _now()
    gid = str(github_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            INSERT INTO users (github_id, github_login, github_avatar, github_token, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(github_id) DO UPDATE SET
                github_login  = excluded.github_login,
                github_avatar = excluded.github_avatar,
                github_token  = excluded.github_token,
                updated_at    = excluded.updated_at
        """, (gid, login, avatar_url, github_token, now, now))
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE github_id = ?", (gid,)) as cur:
            row = await cur.fetchone()
        return _row_to_user(row)


async def get_user_by_id(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_user(row) if row else None


async def save_vercel_token(user_id: int, token: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET vercel_token = ?, updated_at = ? WHERE id = ?",
            (token, _now(), user_id),
        )
        await db.commit()


# ── Sessions ──────────────────────────────────────────────────────────────────

async def create_session(user_id: int) -> str:
    """Create a new session token for user_id. Returns the raw token string."""
    token = uuid.uuid4().hex + uuid.uuid4().hex  # 64-char random token
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, _now(), _expires()),
        )
        await db.commit()
    return token


async def get_user_by_session_token(token: str) -> dict | None:
    """Look up session token → user dict, or None if expired/missing."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.* FROM users u
            JOIN sessions s ON s.user_id = u.id
            WHERE s.token = ? AND s.expires_at > ?
        """, (token, _now())) as cur:
            row = await cur.fetchone()
        return _row_to_user(row) if row else None


async def delete_session(token: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await db.commit()


async def purge_expired_sessions() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
        await db.commit()
    print("✓ Expired sessions purged")


# ── Projects ──────────────────────────────────────────────────────────────────

async def save_project(
    user_id: int,
    session_id: str,
    app_name: str,
    deploy_url: str | None,
    spec: dict,
    github_repo: str | None = None,
) -> dict:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            INSERT OR REPLACE INTO projects
                (user_id, session_id, app_name, deploy_url, github_repo, spec_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, session_id, app_name, deploy_url, github_repo, json.dumps(spec), now, now))
        await db.commit()
        async with db.execute("SELECT * FROM projects WHERE session_id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
        return dict(row)


async def list_user_projects(user_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT session_id, app_name, deploy_url, github_repo, created_at, updated_at
            FROM projects WHERE user_id = ?
            ORDER BY updated_at DESC LIMIT ?
        """, (user_id, limit)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]