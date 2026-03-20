"""
auth.py — Auth helpers for Ramble.

Exports standalone functions that main.py imports directly:
  get_current_user(request)      — FastAPI dependency, raises 401 if not logged in
  set_session_cookie(resp, tok)  — writes HttpOnly cookie
  clear_session_cookie(resp)     — clears cookie
  github_auth_url()              — returns GitHub OAuth redirect URL
  exchange_github_code(code)     — async, returns user dict from GitHub or None
  vercel_auth_url(user_id)       — returns Vercel OAuth redirect URL
  exchange_vercel_code(code)     — async, returns Vercel token string or None
  push_to_github(token, name, files) — async, creates repo + pushes files, returns URL or None
"""

import os
import base64
import httpx
from fastapi import Request, HTTPException

from db import get_user_by_session_token

# ── Config ────────────────────────────────────────────────────────────────────

APP_URL              = os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")
GITHUB_CLIENT_ID     = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
VERCEL_CLIENT_ID     = os.environ.get("VERCEL_CLIENT_ID", "")
VERCEL_CLIENT_SECRET = os.environ.get("VERCEL_CLIENT_SECRET", "")

COOKIE_NAME = "ramble_session"
IS_PROD     = APP_URL.startswith("https://")


# ── Cookie helpers ────────────────────────────────────────────────────────────

def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=IS_PROD,
        max_age=60 * 60 * 24 * 30,  # 30 days
        path="/",
    )

def clear_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency. Reads ramble_session cookie, looks up the user.
    Raises HTTP 401 if the session is missing, expired, or invalid.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await get_user_by_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    return user


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

def github_auth_url() -> str:
    """Return the GitHub OAuth authorization URL."""
    if not GITHUB_CLIENT_ID:
        raise ValueError("GITHUB_CLIENT_ID not set")
    callback = f"{APP_URL}/auth/github/callback"
    return (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={callback}"
        f"&scope=read:user,repo"
    )

async def exchange_github_code(code: str) -> dict | None:
    """
    Exchange a GitHub OAuth code for an access token, then fetch user profile.
    Returns dict with keys: id, login, avatar_url, token — or None on failure.
    """
    callback = f"{APP_URL}/auth/github/callback"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            tok_resp = await client.post(
                "https://github.com/login/oauth/access_token",
                json={
                    "client_id":     GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  callback,
                },
                headers={"Accept": "application/json"},
            )
            token = tok_resp.json().get("access_token")
            if not token:
                print(f"GitHub token exchange failed: {tok_resp.text}")
                return None

            user_resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            gh = user_resp.json()
            if "id" not in gh:
                print(f"GitHub user fetch failed: {gh}")
                return None

            return {
                "id":         gh["id"],
                "login":      gh.get("login", ""),
                "avatar_url": gh.get("avatar_url"),
                "token":      token,
            }
        except Exception as e:
            print(f"exchange_github_code error: {e}")
            return None


# ── Vercel OAuth ──────────────────────────────────────────────────────────────

def vercel_auth_url(user_id: int) -> str:
    """
    Return Vercel OAuth URL. user_id is passed as `state` so the
    callback can identify which user to store the token for, even
    without an active session (Vercel redirects may lose the cookie).
    """
    if not VERCEL_CLIENT_ID:
        raise ValueError("VERCEL_CLIENT_ID not set")
    callback = f"{APP_URL}/auth/vercel/callback"
    return (
        "https://vercel.com/oauth/authorize"
        f"?client_id={VERCEL_CLIENT_ID}"
        f"&redirect_uri={callback}"
        f"&state={user_id}"
    )

async def exchange_vercel_code(code: str) -> str | None:
    """Exchange Vercel OAuth code for an access token. Returns token string or None."""
    callback = f"{APP_URL}/auth/vercel/callback"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                "https://api.vercel.com/v2/oauth/access_token",
                data={
                    "client_id":     VERCEL_CLIENT_ID,
                    "client_secret": VERCEL_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  callback,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = resp.json()
            token = data.get("access_token")
            if not token:
                print(f"Vercel token exchange failed: {data}")
            return token
        except Exception as e:
            print(f"exchange_vercel_code error: {e}")
            return None


# ── GitHub repo push ──────────────────────────────────────────────────────────

async def push_to_github(
    github_token: str,
    repo_name: str,
    file_map: dict[str, str],
) -> str | None:
    """
    Create a public GitHub repo and push all source files via the Contents API.
    Returns the HTML repo URL on success, None on failure.
    No git binary required — uses pure HTTP.
    """
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # 1. Get authenticated user's login (for repo URL)
            me_resp = await client.get("https://api.github.com/user", headers=headers)
            me      = me_resp.json()
            login   = me.get("login", "")
            if not login:
                print("push_to_github: could not fetch user login")
                return None

            # 2. Create repo
            create_resp = await client.post(
                "https://api.github.com/user/repos",
                headers=headers,
                json={
                    "name":        repo_name,
                    "description": "Built with Ramble · ramble.build",
                    "private":     False,
                    "auto_init":   False,
                },
            )
            created = create_resp.json()
            if create_resp.status_code not in (201, 422):  # 422 = already exists
                print(f"push_to_github: create repo failed: {created}")
                return None

            repo_api = f"https://api.github.com/repos/{login}/{repo_name}"
            repo_url = f"https://github.com/{login}/{repo_name}"

            # 3. Push each file via Contents API (works for small files)
            skip_extensions = {".zip", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}
            for file_path, content in file_map.items():
                # Skip binary-ish files and node_modules if somehow present
                if any(file_path.endswith(ext) for ext in skip_extensions):
                    continue
                if "node_modules" in file_path or file_path.startswith("dist/"):
                    continue

                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

                # Check if file exists (to get its sha for update)
                existing = await client.get(f"{repo_api}/contents/{file_path}", headers=headers)
                body: dict = {"message": f"Add {file_path}", "content": encoded}
                if existing.status_code == 200:
                    body["sha"] = existing.json().get("sha", "")

                put_resp = await client.put(
                    f"{repo_api}/contents/{file_path}",
                    headers=headers,
                    json=body,
                )
                if put_resp.status_code not in (200, 201):
                    print(f"push_to_github: failed to push {file_path}: {put_resp.status_code}")
                    # Don't abort — partial push is better than none

            print(f"✓ GitHub repo created: {repo_url}")
            return repo_url

        except Exception as e:
            print(f"push_to_github error: {e}")
            return None