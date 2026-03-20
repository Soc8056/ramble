"""
session_store.py — Disk-backed session persistence.

Each session is a JSON file at sessions/{session_id}.json containing:
  - spec: the product spec dict
  - file_map: all generated source files {path: content}
  - deploy_url: the Vercel URL
  - app_name: human-readable app name
  - conversation_history: full advisor conversation
  - created_at: ISO timestamp
  - updated_at: ISO timestamp

Also maintains an in-memory cache so hot reads don't hit disk.
Sessions directory is created on first use.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path("sessions")

# In-memory cache: session_id → session dict
_cache: dict[str, dict] = {}


def _sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR


def _path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_session(session_id: str, data: dict) -> None:
    """Save or update a session. Merges with existing data if session already exists."""
    existing = _cache.get(session_id, {})
    existing.update(data)
    existing["updated_at"] = _now()
    if "created_at" not in existing:
        existing["created_at"] = _now()
    existing["session_id"] = session_id

    _cache[session_id] = existing

    try:
        with open(_path(session_id), "w", encoding="utf-8") as f:
            # file_map can be large — write it but don't pretty-print
            json.dump(existing, f, separators=(",", ":"))
    except Exception as e:
        print(f"[session_store] WARNING: could not write {session_id} to disk: {e}")


def load_session(session_id: str) -> dict | None:
    """Load a session from cache or disk. Returns None if not found."""
    if session_id in _cache:
        return _cache[session_id]

    p = _path(session_id)
    if not p.exists():
        return None

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache[session_id] = data
        return data
    except Exception as e:
        print(f"[session_store] WARNING: could not read {session_id}: {e}")
        return None


def list_sessions(limit: int = 20) -> list[dict]:
    """
    Return the most recent sessions (metadata only, no file_map).
    Sorted newest-first.
    """
    sessions = []
    try:
        for p in sorted(_sessions_dir().glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id":  data.get("session_id", p.stem),
                    "app_name":    data.get("app_name", "Unknown"),
                    "deploy_url":  data.get("deploy_url"),
                    "created_at":  data.get("created_at"),
                    "updated_at":  data.get("updated_at"),
                })
            except Exception:
                continue
    except Exception:
        pass
    return sessions


def delete_session(session_id: str) -> bool:
    _cache.pop(session_id, None)
    p = _path(session_id)
    if p.exists():
        p.unlink()
        return True
    return False