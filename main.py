import os
import json
import uuid
import asyncio
import subprocess
import tempfile
import shutil
import zipfile
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from dotenv import load_dotenv
import anthropic
from openai import OpenAI

from spec_prompt         import ADVISOR_SYSTEM
from orchestrator_prompt import ORCHESTRATOR_SYSTEM, ORCHESTRATOR_USER_TEMPLATE
from file_prompt         import FILE_SYSTEM, FILE_USER_TEMPLATE
from integration_prompt  import INTEGRATION_SYSTEM, INTEGRATION_USER_TEMPLATE
from edit_prompt         import EDITOR_SYSTEM, EDITOR_USER_TEMPLATE
from session_store       import save_session, load_session, list_sessions
from db                  import (
    init_db, save_project, list_user_projects,
    save_vercel_token, purge_expired_sessions,
    upsert_user, create_session, delete_session as db_delete_session,
)
from auth import (
    get_current_user, set_session_cookie, clear_session_cookie,
    github_auth_url, exchange_github_code,
    vercel_auth_url, exchange_vercel_code,
    push_to_github,
)

load_dotenv()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

try:
    anthropic_sync  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    anthropic_async = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    openai_client   = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    print("\u2713 API clients initialized")
except Exception as e:
    print(f"ERROR: {e}"); raise

@app.on_event("startup")
async def startup():
    await init_db()
    await purge_expired_sessions()

# Per-user conversation history
_conversations: dict[int, list[dict]] = {}
def get_history(user_id: int) -> list[dict]:
    if user_id not in _conversations: _conversations[user_id] = []
    return _conversations[user_id]
def clear_history(user_id: int) -> None:
    _conversations[user_id] = []

# SSE
_sse_queues: dict[str, asyncio.Queue] = {}
def _get_or_create_queue(sid: str) -> asyncio.Queue:
    if sid not in _sse_queues: _sse_queues[sid] = asyncio.Queue()
    return _sse_queues[sid]
async def _push(sid: str, event: dict) -> None:
    await _get_or_create_queue(sid).put(event)
    print(f"[{sid[:8]}] {event.get('msg') or event.get('type')}")
async def _sse_generator(sid: str) -> AsyncGenerator[str, None]:
    _get_or_create_queue(sid)
    while True:
        try:
            event = await asyncio.wait_for(_sse_queues[sid].get(), timeout=120)
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"): break
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"

# Fixed templates
VITE_CONFIG = "import { defineConfig } from 'vite'\nimport react from '@vitejs/plugin-react'\nexport default defineConfig({ plugins: [react()] })\n"
MAIN_JSX    = "import React from 'react'\nimport ReactDOM from 'react-dom/client'\nimport App from './App'\n\nReactDOM.createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)\n"

def make_package_json(npm_name: str) -> str:
    return json.dumps({"name": npm_name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
        "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0", "react-router-dom": "^6.8.0", "lucide-react": "^0.263.1"},
        "devDependencies": {"@vitejs/plugin-react": "^4.0.0", "vite": "^4.4.0"}}, indent=2)

def make_index_html(app_name: str, tokens: dict) -> str:
    fu = tokens.get("font_url", "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap")
    ff = tokens.get("font_family", "Inter")
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\"/>"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1.0\"/>"
            f"<title>{app_name}</title><link href=\"{fu}\" rel=\"stylesheet\"/>"
            f"<script src=\"https://cdn.tailwindcss.com\"></script>"
            f"<style>:root{{--bg:{tokens.get('bg','#0f172a')};--accent:{tokens.get('accent','#6366f1')};--text:{tokens.get('text','#f1f5f9')}}}"
            f"*{{box-sizing:border-box}}html,body,#root{{height:100%;margin:0}}"
            f"body{{background:var(--bg);color:var(--text);font-family:'{ff}',sans-serif}}</style>"
            f"</head><body><div id=\"root\"></div><script type=\"module\" src=\"/src/main.jsx\"></script></body></html>")

# Generation helpers
def build_waves(files: list[dict]) -> list[list[dict]]:
    remaining, completed, waves = list(files), set(), []
    while remaining:
        wave = [f for f in remaining if all(d in completed for d in f.get("depends_on", []))]
        if not wave: wave = list(remaining)
        waves.append(wave)
        for f in wave: completed.add(f["path"])
        remaining = [f for f in remaining if f not in wave]
    return waves

def format_dep_contents(deps: list[str], generated: dict[str, str]) -> str:
    if not deps: return "(no local dependencies)"
    parts = []
    for dep in deps:
        c = generated.get(dep, "(not yet generated)")
        if len(c) > 3000: c = c[:3000] + "\n// ... (truncated)"
        parts.append(f"=== {dep} ===\n{c}")
    return "\n\n".join(parts)

def make_fallback_stub(path: str) -> str:
    name = Path(path).stem
    if path.endswith(".jsx"):
        return f"import React from 'react';\nexport default function {name}() {{\n  return <div style={{{{padding:'2rem',color:'#888',textAlign:'center'}}}}>{name} failed to generate.</div>;\n}}\n"
    return f"// {path} — generation failed\n"

async def run_orchestrator(spec: dict, platform: str) -> dict | None:
    prompt = ORCHESTRATOR_USER_TEMPLATE.format(spec_json=json.dumps(spec, indent=2), platform=platform)
    try:
        resp = await anthropic_async.messages.create(model="claude-sonnet-4-20250514", max_tokens=4000,
            system=ORCHESTRATOR_SYSTEM, messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"): raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
        print(f"\u2713 Orchestrator: {plan.get('app_name')} ({len(plan.get('files',[]))} files)")
        return plan
    except Exception as e:
        print(f"ERROR orchestrator: {e}"); return None

async def generate_one_file(fi: dict, plan: dict, spec: dict, generated: dict[str, str], platform: str) -> str:
    complexity = fi.get("complexity", "medium")
    max_tok    = {"low": 2000, "medium": 4000, "high": 8000}.get(complexity, 4000)
    prompt = FILE_USER_TEMPLATE.format(
        file_path=fi["path"], file_description=fi["description"], file_complexity=complexity,
        spec_json=json.dumps(spec, indent=2), design_tokens_json=json.dumps(plan.get("design_tokens", {}), indent=2),
        all_files_list="\n".join(f"  {f['path']}" for f in plan["files"]),
        dependency_contents=format_dep_contents(fi.get("depends_on", []), generated), platform=platform)
    resp = await anthropic_async.messages.create(model="claude-sonnet-4-20250514", max_tokens=max_tok,
        system=FILE_SYSTEM, messages=[{"role": "user", "content": prompt}])
    content = resp.content[0].text.strip()
    if content.startswith("```"): content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    print(f"  \u2713 {fi['path']} ({len(content):,} chars)")
    return content

async def run_integration_pass(plan: dict, spec: dict, generated: dict[str, str]) -> dict[str, str]:
    source = {k: v for k, v in generated.items() if k.endswith((".jsx", ".js")) and k != "vite.config.js"}
    files_block = "\n\n".join(f"=== {p} ===\n{c}" for p, c in source.items())
    prompt = INTEGRATION_USER_TEMPLATE.format(app_name=plan.get("app_name",""), platform=spec.get("platform","both"),
        core_function=spec.get("core_function",""), key_screens=spec.get("key_screens",""),
        core_features=spec.get("core_features",""), files_json=files_block)
    try:
        resp = await anthropic_async.messages.create(model="claude-sonnet-4-20250514", max_tokens=10000,
            system=INTEGRATION_SYSTEM, messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"): raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if not raw or raw in ("{}", '{"fixes":[]}'):  return {}
        result = json.loads(raw)
        if "fixes" in result:
            return {f["path"]: f["content"] for f in result["fixes"] if f.get("path") and f.get("content")}
        return result
    except Exception as e:
        print(f"WARNING: integration pass failed ({e})"); return {}

async def build_and_deploy(
    spec: dict, session_id: str, user_id: int = 0,
    vercel_token: str | None = None, github_token: str | None = None,
    file_map_override: dict[str, str] | None = None,
    files_to_regen: list[str] | None = None,
) -> str | None:
    platform = spec.get("platform", "both")
    is_edit  = file_map_override is not None
    await _push(session_id, {"type": "progress", "msg": "Starting build pipeline..."})

    if is_edit:
        plan = None; generated = dict(file_map_override)
        sess = load_session(session_id)
        app_name = sess.get("app_name", "App") if sess else "App"
        npm_name = app_name.lower().replace(" ", "-")
    else:
        await _push(session_id, {"type": "progress", "msg": "Planning file structure..."})
        plan = await run_orchestrator(spec, platform)
        if not plan:
            await _push(session_id, {"type": "error", "msg": "Orchestrator failed."}); return None
        app_name = plan.get("app_name", "Ramble App")
        npm_name = plan.get("npm_name", app_name.lower().replace(" ", "-"))
        tokens   = plan.get("design_tokens", {})
        await _push(session_id, {"type": "progress", "msg": f"Plan ready: {app_name} · {len(plan['files'])} files"})
        generated = {"package.json": make_package_json(npm_name), "vite.config.js": VITE_CONFIG,
                     "index.html": make_index_html(app_name, tokens), "src/main.jsx": MAIN_JSX}

    if is_edit:
        sess_data = load_session(session_id)
        edit_plan = sess_data.get("plan", {}) if sess_data else {}
        for path in (files_to_regen or []):
            fi = next((f for f in edit_plan.get("files", []) if f["path"] == path),
                      {"path": path, "description": "Updated by user request", "complexity": "medium", "depends_on": []})
            await _push(session_id, {"type": "progress", "msg": f"Updating {path}..."})
            try: generated[path] = await generate_one_file(fi, edit_plan or {"design_tokens":{}, "files":[]}, spec, generated, platform)
            except Exception: await _push(session_id, {"type": "progress", "msg": f"  \u2717 {path} — keeping original"})
    else:
        waves = build_waves(plan["files"])
        await _push(session_id, {"type": "progress", "msg": f"Generating in {len(waves)} wave(s)..."})
        for wi, wave in enumerate(waves):
            names = ", ".join(f["path"].split("/")[-1] for f in wave)
            await _push(session_id, {"type": "progress", "msg": f"Wave {wi+1}/{len(waves)}: {names}"})
            results = await asyncio.gather(*[generate_one_file(f, plan, spec, generated, platform) for f in wave], return_exceptions=True)
            for fi, result in zip(wave, results):
                if isinstance(result, Exception):
                    await _push(session_id, {"type": "progress", "msg": f"  \u2717 {fi['path']} — stub"})
                    generated[fi["path"]] = make_fallback_stub(fi["path"])
                else: generated[fi["path"]] = result

    await _push(session_id, {"type": "progress", "msg": "Running integration review..."})
    fixes = await run_integration_pass(plan or {}, spec, generated)
    if fixes:
        generated.update(fixes)
        await _push(session_id, {"type": "progress", "msg": f"Fixed {len(fixes)} file(s)"})

    project_dir = tempfile.mkdtemp()
    try:
        for fp, content in generated.items():
            full = os.path.join(project_dir, fp)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f: f.write(content)

        await _push(session_id, {"type": "progress", "msg": "Installing dependencies..."})
        npm = subprocess.run(["npm", "install"], cwd=project_dir, capture_output=True, text=True, timeout=180)
        if npm.returncode != 0:
            await _push(session_id, {"type": "error", "msg": f"npm install failed:\n{npm.stderr[-400:]}"}); return None

        await _push(session_id, {"type": "progress", "msg": "Building production bundle..."})
        build = subprocess.run(["npm", "run", "build"], cwd=project_dir, capture_output=True, text=True, timeout=120)
        if build.returncode != 0:
            await _push(session_id, {"type": "error", "msg": f"vite build failed:\n{build.stdout[-300:]}\n{build.stderr[-300:]}"}); return None
        await _push(session_id, {"type": "progress", "msg": "Build complete \u2713"})

        zip_path = f"/tmp/ramble_{session_id}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fp in generated:
                    full = os.path.join(project_dir, fp)
                    if os.path.exists(full): zf.write(full, fp)
        except Exception as e: print(f"WARNING: zip failed: {e}")

        github_repo_url = None
        if github_token and not is_edit:
            await _push(session_id, {"type": "progress", "msg": "Pushing source to GitHub..."})
            repo_name = f"{npm_name}-{session_id[:6]}"
            github_repo_url = await push_to_github(github_token, repo_name, generated)
            if github_repo_url:
                await _push(session_id, {"type": "progress", "msg": f"\u2713 GitHub: {github_repo_url}"})

        dist_dir = os.path.join(project_dir, "dist")
        if not os.path.exists(dist_dir):
            await _push(session_id, {"type": "error", "msg": "dist/ not found after build"}); return None

        eff_token   = vercel_token or os.environ.get("VERCEL_TOKEN", "")
        owner_label = "your Vercel account" if vercel_token else "Ramble's Vercel"
        proj_name   = f"ramble-{uuid.uuid4().hex[:8]}"
        await _push(session_id, {"type": "progress", "msg": f"Deploying to {owner_label}..."})

        vercel = subprocess.run(
            ["vercel", "--yes", "--name", proj_name, "--prod", "--token", eff_token],
            cwd=dist_dir, capture_output=True, text=True, timeout=300)
        print(f"VERCEL STDOUT:\n{vercel.stdout}")

        deploy_url = None
        for line in reversed(vercel.stdout.strip().split("\n")):
            if line.strip().startswith("https://"): deploy_url = line.strip(); break

        if not deploy_url:
            await _push(session_id, {"type": "error", "msg": "Could not extract Vercel URL"}); return None

        await _push(session_id, {"type": "progress", "msg": f"\u2713 Deployed: {deploy_url}"})

        save_session(session_id, {"spec": spec, "file_map": generated, "deploy_url": deploy_url,
            "app_name": app_name, "npm_name": npm_name, "plan": plan,
            "zip_path": zip_path, "user_id": user_id, "github_repo_url": github_repo_url})

        if user_id and not is_edit:
            await save_project(user_id, session_id, app_name, deploy_url, spec)

        await _push(session_id, {"type": "done", "deploy_url": deploy_url, "session_id": session_id,
            "github_repo_url": github_repo_url, "owned_deploy": bool(vercel_token)})
        return deploy_url

    except subprocess.TimeoutExpired as e:
        await _push(session_id, {"type": "error", "msg": f"Timed out: {e}"}); return None
    except Exception as e:
        await _push(session_id, {"type": "error", "msg": f"Build error: {e}"}); return None
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)

# Auth routes
@app.get("/auth/github")
async def auth_github():
    return RedirectResponse(github_auth_url())

@app.get("/auth/github/callback")
async def auth_github_callback(code: str = "", error: str = ""):
    if error or not code: return RedirectResponse("/?auth_error=github_denied")
    gu = await exchange_github_code(code)
    if not gu: return RedirectResponse("/?auth_error=exchange_failed")
    user  = await upsert_user(gu["id"], gu["login"], gu["avatar_url"], gu["token"])
    token = await create_session(user["id"])
    resp  = RedirectResponse("/")
    set_session_cookie(resp, token)
    return resp

@app.get("/auth/vercel")
async def auth_vercel(user: dict = Depends(get_current_user)):
    return RedirectResponse(vercel_auth_url(user["id"]))

@app.get("/auth/vercel/callback")
async def auth_vercel_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code: return RedirectResponse("/?vercel_error=denied")
    vt = await exchange_vercel_code(code)
    if not vt: return RedirectResponse("/?vercel_error=exchange_failed")
    try: await save_vercel_token(int(state), vt)
    except Exception as e: print(f"Vercel callback save error: {e}"); return RedirectResponse("/?vercel_error=save_failed")
    return RedirectResponse("/?vercel_connected=1")

@app.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get("ramble_session")
    if token: await db_delete_session(token)
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return resp

@app.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return JSONResponse({"id": user["id"], "login": user["github_login"],
        "avatar_url": user["github_avatar_url"], "vercel_connected": bool(user.get("vercel_token"))})

# App routes
@app.get("/")
async def serve_index(): return FileResponse("index.html")

@app.get("/manifest.json")
async def serve_manifest(): return FileResponse("manifest.json", media_type="application/manifest+json")

@app.post("/reset")
async def reset(user: dict = Depends(get_current_user)):
    clear_history(user["id"]); return {"ok": True}

@app.get("/projects")
async def get_projects(user: dict = Depends(get_current_user)):
    return JSONResponse(await list_user_projects(user["id"]))

@app.post("/regenerate/{session_id}")
async def regenerate(session_id: str,
                     background_tasks: BackgroundTasks = BackgroundTasks(),
                     user: dict = Depends(get_current_user)):
    """
    Re-run the full build pipeline for an existing session using the same spec.
    Returns a new session_id immediately; client connects SSE to track progress.
    """
    data = load_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    if data.get("user_id") and data["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your session")

    spec = data.get("spec")
    if not spec:
        raise HTTPException(status_code=400, detail="No spec stored for this session")

    new_sid = uuid.uuid4().hex
    _get_or_create_queue(new_sid)
    background_tasks.add_task(build_and_deploy, spec, new_sid,
        user["id"], user.get("vercel_token"), user.get("github_token"))

    return JSONResponse({"session_id": new_sid})


@app.get("/sessions/{session_id}")
async def get_session_detail(session_id: str, user: dict = Depends(get_current_user)):
    data = load_session(session_id)
    if not data: raise HTTPException(status_code=404, detail="Session not found")
    if data.get("user_id") and data["user_id"] != user["id"]: raise HTTPException(status_code=403, detail="Not your session")
    return JSONResponse({k: v for k, v in data.items() if k != "file_map"})

@app.get("/download/{session_id}")
async def download_project(session_id: str, user: dict = Depends(get_current_user)):
    data = load_session(session_id)
    if not data: raise HTTPException(status_code=404, detail="Session not found")
    if data.get("user_id") and data["user_id"] != user["id"]: raise HTTPException(status_code=403, detail="Not your session")
    zip_path = data.get("zip_path")
    if not zip_path or not os.path.exists(zip_path): raise HTTPException(status_code=404, detail="Source zip not found")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{data.get('app_name','app')}.zip")

@app.get("/progress/{session_id}")
async def stream_progress(session_id: str):
    _get_or_create_queue(session_id)
    return StreamingResponse(_sse_generator(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/chat")
async def chat(audio: UploadFile = File(...),
               background_tasks: BackgroundTasks = BackgroundTasks(),
               user: dict = Depends(get_current_user)):
    history = get_history(user["id"])

    try:
        audio_bytes = await audio.read()
        if   audio_bytes[:4]  == b'fLaC':                                          suffix = ".flac"; ftype = "audio/flac"
        elif audio_bytes[4:8] == b'ftyp' or audio_bytes[:4] == b'\x00\x00\x00\x1c': suffix = ".mp4";  ftype = "audio/mp4"
        elif audio_bytes[:4]  == b'OggS':                                          suffix = ".ogg";  ftype = "audio/ogg"
        elif audio_bytes[:4]  == b'RIFF':                                          suffix = ".wav";  ftype = "audio/wav"
        elif audio_bytes[:3]  == b'ID3' or audio_bytes[:2] == b'\xff\xfb':        suffix = ".mp3";  ftype = "audio/mpeg"
        else:                                                                         suffix = ".mp4";  ftype = "audio/mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes); tmp_path = tmp.name
    except Exception as e: raise HTTPException(status_code=500, detail=f"Audio save error: {e}")

    try:
        if ftype == "audio/mp4":
            with open(tmp_path, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.mp4", f, "audio/mp4"))
        else:
            wav = tmp_path + ".wav"
            r = subprocess.run(["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav], capture_output=True, text=True)
            if r.returncode != 0: raise Exception(f"ffmpeg: {r.stderr}")
            with open(wav, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.wav", f, "audio/wav"))
            try: os.unlink(wav)
            except: pass
        user_text = tx.text.strip()
        print(f"TRANSCRIPT [{user['github_login']}]: {user_text}")
    except Exception as e: raise HTTPException(status_code=500, detail=f"Whisper error: {e}")
    finally:
        try: os.unlink(tmp_path)
        except: pass

    if not user_text: raise HTTPException(status_code=400, detail="Empty transcript")

    history.append({"role": "user", "content": user_text})
    try:
        resp = anthropic_sync.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000,
            system=ADVISOR_SYSTEM, messages=history)
        raw = resp.content[0].text.strip()
    except Exception as e:
        history.pop(); raise HTTPException(status_code=500, detail=f"Claude error: {e}")

    try:
        clean = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip() if raw.startswith("```") else raw
        data  = json.loads(clean)
    except Exception as e:
        history.pop(); raise HTTPException(status_code=500, detail=f"Advisor JSON error: {e}")

    spoken = data.get("message", "Sorry, can you say that again?")
    stage  = data.get("stage", "gathering")   # gathering | confirming | confirmed
    spec   = data.get("spec", None)
    # "complete" = user explicitly confirmed the spec — build starts now
    complete = (stage == "confirmed") and bool(spec)
    history.append({"role": "assistant", "content": raw})

    try:
        tts = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=spoken)
        audio_filename = f"resp_{uuid.uuid4().hex}.mp3"
        tts.stream_to_file(f"/tmp/{audio_filename}")
    except Exception as e: raise HTTPException(status_code=500, detail=f"TTS error: {e}")

    session_id = None
    if complete:
        session_id = uuid.uuid4().hex
        _get_or_create_queue(session_id)
        background_tasks.add_task(build_and_deploy, spec, session_id,
            user["id"], user.get("vercel_token"), user.get("github_token"))

    return JSONResponse({"transcript": user_text, "message": spoken,
        "audio_url": f"/audio/{audio_filename}", "stage": stage,
        "complete": complete, "spec": spec, "session_id": session_id})


@app.post("/edit")
async def edit(audio: UploadFile = File(...), session_id: str = "",
               background_tasks: BackgroundTasks = BackgroundTasks(),
               user: dict = Depends(get_current_user)):
    if not session_id: raise HTTPException(status_code=400, detail="session_id required")
    sess = load_session(session_id)
    if not sess: raise HTTPException(status_code=404, detail="Session not found")
    if sess.get("user_id") and sess["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your session")

    spec, file_map = sess.get("spec", {}), sess.get("file_map", {})

    try:
        audio_bytes = await audio.read()
        suffix = ".mp4" if audio_bytes[4:8] == b'ftyp' else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes); tmp_path = tmp.name
        if suffix == ".mp4":
            with open(tmp_path, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.mp4", f, "audio/mp4"))
        else:
            wav = tmp_path + ".wav"
            subprocess.run(["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav], capture_output=True)
            with open(wav, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.wav", f, "audio/wav"))
            try: os.unlink(wav)
            except: pass
        user_text = tx.text.strip()
    except Exception as e: raise HTTPException(status_code=500, detail=f"Transcription error: {e}")
    finally:
        try: os.unlink(tmp_path)
        except: pass

    file_list = "\n".join(f"  {p}" for p in file_map.keys())
    prompt = EDITOR_USER_TEMPLATE.format(spec_json=json.dumps(spec, indent=2),
        user_request=user_text, file_list=file_list)
    try:
        resp = anthropic_sync.messages.create(model="claude-sonnet-4-20250514", max_tokens=500,
            system=EDITOR_SYSTEM, messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        edit_data = json.loads(raw.split("\n", 1)[1].rsplit("```", 1)[0].strip() if raw.startswith("```") else raw)
    except Exception as e: raise HTTPException(status_code=500, detail=f"Editor error: {e}")

    spoken = edit_data.get("message", "Making that change now.")
    ready  = edit_data.get("ready", False)
    ftc    = edit_data.get("files_to_change", [])

    try:
        tts = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=spoken)
        audio_filename = f"resp_{uuid.uuid4().hex}.mp3"
        tts.stream_to_file(f"/tmp/{audio_filename}")
    except Exception as e: raise HTTPException(status_code=500, detail=f"TTS error: {e}")

    new_sid = None
    if ready and ftc:
        new_sid = uuid.uuid4().hex
        _get_or_create_queue(new_sid)
        background_tasks.add_task(build_and_deploy, spec, new_sid,
            user["id"], user.get("vercel_token"), user.get("github_token"), file_map, ftc)

    return JSONResponse({"transcript": user_text, "message": spoken,
        "audio_url": f"/audio/{audio_filename}", "ready": ready,
        "needs_clarify": not ready, "session_id": new_sid})


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    path = f"/tmp/{filename}"
    if not os.path.exists(path): raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/mpeg")