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

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from dotenv import load_dotenv
import anthropic
from openai import OpenAI

from spec_prompt        import ADVISOR_SYSTEM
from orchestrator_prompt import ORCHESTRATOR_SYSTEM, ORCHESTRATOR_USER_TEMPLATE
from file_prompt         import FILE_SYSTEM, FILE_USER_TEMPLATE
from integration_prompt  import INTEGRATION_SYSTEM, INTEGRATION_USER_TEMPLATE
from edit_prompt         import EDITOR_SYSTEM, EDITOR_USER_TEMPLATE
from session_store       import save_session, load_session, list_sessions

load_dotenv()

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

try:
    anthropic_sync  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    anthropic_async = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    openai_client   = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    print("✓ API clients initialized")
except Exception as e:
    print(f"ERROR initializing clients: {e}")
    raise

# ── In-memory state ──────────────────────────────────────────────────────────
# Single-user conversation history (Phase 3 replaces with per-user auth)
conversation_history: list[dict] = []

# SSE queues: session_id → asyncio.Queue of event dicts
# Events: {"type": "progress", "msg": "..."} | {"type": "done", "deploy_url": "...", "session_id": "..."}
_sse_queues: dict[str, asyncio.Queue] = {}


# ── Fixed templates (no Claude call needed) ──────────────────────────────────

VITE_CONFIG = (
    "import { defineConfig } from 'vite'\n"
    "import react from '@vitejs/plugin-react'\n"
    "export default defineConfig({ plugins: [react()] })\n"
)

MAIN_JSX = (
    "import React from 'react'\n"
    "import ReactDOM from 'react-dom/client'\n"
    "import App from './App'\n\n"
    "ReactDOM.createRoot(document.getElementById('root')).render(\n"
    "  <React.StrictMode><App /></React.StrictMode>\n"
    ")\n"
)


def make_package_json(npm_name: str) -> str:
    return json.dumps({
        "name": npm_name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
        "dependencies": {
            "react": "^18.2.0", "react-dom": "^18.2.0",
            "react-router-dom": "^6.8.0", "lucide-react": "^0.263.1",
        },
        "devDependencies": {"@vitejs/plugin-react": "^4.0.0", "vite": "^4.4.0"},
    }, indent=2)


def make_index_html(app_name: str, tokens: dict) -> str:
    font_url    = tokens.get("font_url", "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap")
    font_family = tokens.get("font_family", "Inter")
    bg          = tokens.get("bg", "#0f172a")
    accent      = tokens.get("accent", "#6366f1")
    accent_lo   = tokens.get("accent_lo", "rgba(99,102,241,0.1)")
    bg2         = tokens.get("bg2", "#1e293b")
    bg3         = tokens.get("bg3", "#334155")
    border      = tokens.get("border", "#1e293b")
    border2     = tokens.get("border2", "#334155")
    text        = tokens.get("text", "#f1f5f9")
    muted       = tokens.get("muted", "#94a3b8")
    radius      = tokens.get("radius", "8px")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <title>{app_name}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="{font_url}" rel="stylesheet" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      theme: {{ extend: {{ fontFamily: {{ sans: ['{font_family}', 'sans-serif'] }} }} }}
    }}
  </script>
  <style>
    :root {{
      --bg: {bg}; --bg2: {bg2}; --bg3: {bg3};
      --border: {border}; --border2: {border2};
      --text: {text}; --muted: {muted};
      --accent: {accent}; --accent-lo: {accent_lo};
      --radius: {radius};
    }}
    *, *::before, *::after {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
    html, body, #root {{ height: 100%; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text); font-family: '{font_family}', sans-serif; }}
  </style>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/src/main.jsx"></script>
</body>
</html>"""


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _get_or_create_queue(session_id: str) -> asyncio.Queue:
    if session_id not in _sse_queues:
        _sse_queues[session_id] = asyncio.Queue()
    return _sse_queues[session_id]


async def _push(session_id: str, event: dict) -> None:
    q = _get_or_create_queue(session_id)
    await q.put(event)
    print(f"[{session_id[:8]}] {event.get('msg') or event.get('type')}")


def _push_sync(session_id: str, event: dict) -> None:
    """Thread-safe push from sync context (runs in the event loop)."""
    try:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(_sse_queues[session_id].put_nowait, event)
    except Exception:
        pass


async def _sse_generator(session_id: str) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted strings from the session's queue.
    Closes automatically when it receives a 'done' or 'error' event.
    """
    _get_or_create_queue(session_id)  # ensure it exists

    while True:
        try:
            event = await asyncio.wait_for(_sse_queues[session_id].get(), timeout=120)
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                break
        except asyncio.TimeoutError:
            # Send a keepalive comment so the connection doesn't drop
            yield ": keepalive\n\n"


# ── Topological batching ──────────────────────────────────────────────────────

def build_waves(files: list[dict]) -> list[list[dict]]:
    remaining = list(files)
    completed: set[str] = set()
    waves: list[list[dict]] = []

    while remaining:
        wave = [f for f in remaining if all(d in completed for d in f.get("depends_on", []))]
        if not wave:
            print(f"WARNING: circular deps, forcing remaining {len(remaining)} files")
            wave = list(remaining)
        waves.append(wave)
        for f in wave:
            completed.add(f["path"])
        remaining = [f for f in remaining if f not in wave]

    return waves


# ── File generation helpers ──────────────────────────────────────────────────

def format_dep_contents(dep_paths: list[str], generated: dict[str, str]) -> str:
    if not dep_paths:
        return "(no local dependencies)"
    parts = []
    for dep in dep_paths:
        content = generated.get(dep, "(not yet generated)")
        if len(content) > 3000:
            content = content[:3000] + "\n// ... (truncated)"
        parts.append(f"=== {dep} ===\n{content}")
    return "\n\n".join(parts)


def make_fallback_stub(path: str) -> str:
    name = Path(path).stem
    if path.endswith(".jsx"):
        return (
            f"import React from 'react';\n"
            f"export default function {name}() {{\n"
            f"  return <div style={{{{padding:'2rem',color:'#888',textAlign:'center'}}}}>"
            f"{name} failed to generate.</div>;\n"
            f"}}\n"
        )
    if path == "src/store.js":
        return "// store.js — generation failed\nexport function getData() { return []; }\n"
    return f"// {path} — generation failed\n"


async def run_orchestrator(spec: dict, platform: str) -> dict | None:
    prompt = ORCHESTRATOR_USER_TEMPLATE.format(
        spec_json=json.dumps(spec, indent=2),
        platform=platform,
    )
    try:
        resp = await anthropic_async.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=4000,
            system=ORCHESTRATOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
        print(f"✓ Orchestrator: {plan.get('app_name')} ({len(plan.get('files',[]))} files)")
        return plan
    except Exception as e:
        print(f"ERROR orchestrator: {e}")
        return None


async def generate_one_file(
    file_info: dict, plan: dict, spec: dict,
    generated: dict[str, str], platform: str,
) -> str:
    all_files_list = "\n".join(f"  {f['path']}" for f in plan["files"])
    dep_contents   = format_dep_contents(file_info.get("depends_on", []), generated)
    complexity     = file_info.get("complexity", "medium")
    max_tok        = {"low": 2000, "medium": 4000, "high": 8000}.get(complexity, 4000)

    prompt = FILE_USER_TEMPLATE.format(
        file_path           = file_info["path"],
        file_description    = file_info["description"],
        file_complexity     = complexity,
        spec_json           = json.dumps(spec, indent=2),
        design_tokens_json  = json.dumps(plan.get("design_tokens", {}), indent=2),
        all_files_list      = all_files_list,
        dependency_contents = dep_contents,
        platform            = platform,
    )
    resp = await anthropic_async.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=max_tok,
        system=FILE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    content = resp.content[0].text.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    print(f"  ✓ {file_info['path']} ({len(content):,} chars)")
    return content


async def run_integration_pass(plan: dict, spec: dict, generated: dict[str, str]) -> dict[str, str]:
    source = {k: v for k, v in generated.items() if k.endswith((".jsx", ".js")) and k != "vite.config.js"}
    files_block = "\n\n".join(f"=== {p} ===\n{c}" for p, c in source.items())

    prompt = INTEGRATION_USER_TEMPLATE.format(
        app_name=plan.get("app_name", ""),
        platform=spec.get("platform", "both"),
        core_function=spec.get("core_function", ""),
        key_screens=spec.get("key_screens", ""),
        core_features=spec.get("core_features", ""),
        files_json=files_block,
    )
    try:
        resp = await anthropic_async.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=10000,
            system=INTEGRATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if not raw or raw in ("{}", '{"fixes":[]}'):
            print("✓ Integration: nothing to fix")
            return {}
        result = json.loads(raw)
        # Support both {"path": content} and {"fixes": [{path, content}]} shapes
        if "fixes" in result:
            return {f["path"]: f["content"] for f in result["fixes"] if f.get("path") and f.get("content")}
        return result
    except Exception as e:
        print(f"WARNING: integration pass failed ({e}), skipping")
        return {}


# ── Build pipeline ────────────────────────────────────────────────────────────

async def build_and_deploy(
    spec: dict,
    session_id: str,
    file_map_override: dict[str, str] | None = None,  # for targeted edits
    files_to_regen: list[str] | None = None,          # for targeted edits
) -> str | None:
    """
    Full pipeline: orchestrate → generate → integrate → npm install → vite build → Vercel.
    For edits: pass file_map_override (existing files) + files_to_regen (paths to regenerate).
    Returns deploy_url or None.
    """
    platform = spec.get("platform", "both")
    is_edit  = file_map_override is not None

    await _push(session_id, {"type": "progress", "msg": "Starting build pipeline..."})

    # ── Stage 1: Orchestrate (skip for edits, reuse existing plan) ────────────
    if is_edit:
        plan = None  # edits use a lightweight re-gen, not a full orchestration
        generated = dict(file_map_override)
        app_name  = load_session(session_id).get("app_name", "App") if load_session(session_id) else "App"
        npm_name  = app_name.lower().replace(" ", "-")
        tokens    = {}
    else:
        await _push(session_id, {"type": "progress", "msg": "Planning file structure..."})
        plan = await run_orchestrator(spec, platform)
        if not plan:
            await _push(session_id, {"type": "error", "msg": "Orchestrator failed — please try again."})
            return None

        app_name = plan.get("app_name", "Ramble App")
        npm_name = plan.get("npm_name", app_name.lower().replace(" ", "-"))
        tokens   = plan.get("design_tokens", {})
        await _push(session_id, {"type": "progress", "msg": f"Planning done: {app_name} · {len(plan['files'])} files"})

        # Seed fixed templates
        generated: dict[str, str] = {
            "package.json":   make_package_json(npm_name),
            "vite.config.js": VITE_CONFIG,
            "index.html":     make_index_html(app_name, tokens),
            "src/main.jsx":   MAIN_JSX,
        }

    # ── Stage 2: Generate files ───────────────────────────────────────────────
    if is_edit:
        # Only regenerate specified files using targeted prompts
        await _push(session_id, {"type": "progress", "msg": f"Regenerating {len(files_to_regen)} file(s)..."})
        session_data = load_session(session_id)
        edit_plan = session_data.get("plan", {}) if session_data else {}

        for path in (files_to_regen or []):
            file_info = next(
                (f for f in edit_plan.get("files", []) if f["path"] == path),
                {"path": path, "description": "Updated by user request", "complexity": "medium", "depends_on": []},
            )
            await _push(session_id, {"type": "progress", "msg": f"Updating {path}..."})
            try:
                content = await generate_one_file(file_info, edit_plan or {"design_tokens": {}, "files": []}, spec, generated, platform)
                generated[path] = content
            except Exception as e:
                print(f"Edit regen failed for {path}: {e}")
                await _push(session_id, {"type": "progress", "msg": f"  ✗ {path} failed — keeping original"})
    else:
        waves = build_waves(plan["files"])
        await _push(session_id, {"type": "progress", "msg": f"Generating in {len(waves)} wave(s)..."})

        for wave_idx, wave in enumerate(waves):
            wave_names = ", ".join(f["path"].split("/")[-1] for f in wave)
            await _push(session_id, {"type": "progress", "msg": f"Wave {wave_idx+1}/{len(waves)}: {wave_names}"})

            tasks   = [generate_one_file(f, plan, spec, generated, platform) for f in wave]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for file_info, result in zip(wave, results):
                if isinstance(result, Exception):
                    await _push(session_id, {"type": "progress", "msg": f"  ✗ {file_info['path']} failed — using stub"})
                    generated[file_info["path"]] = make_fallback_stub(file_info["path"])
                else:
                    generated[file_info["path"]] = result

    # ── Stage 3: Integration pass ─────────────────────────────────────────────
    await _push(session_id, {"type": "progress", "msg": "Running integration review..."})
    fixes = await run_integration_pass(plan or {}, spec, generated)
    if fixes:
        generated.update(fixes)
        await _push(session_id, {"type": "progress", "msg": f"Fixed {len(fixes)} file(s): {', '.join(fixes)}"})
    else:
        await _push(session_id, {"type": "progress", "msg": "Integration: no fixes needed"})

    # ── Stage 4: Write to disk ────────────────────────────────────────────────
    project_dir = tempfile.mkdtemp()
    try:
        for fp, content in generated.items():
            full = os.path.join(project_dir, fp)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
        await _push(session_id, {"type": "progress", "msg": f"Wrote {len(generated)} files"})

        # ── Stage 5: npm install ──────────────────────────────────────────────
        await _push(session_id, {"type": "progress", "msg": "Installing dependencies..."})
        npm = subprocess.run(
            ["npm", "install"], cwd=project_dir,
            capture_output=True, text=True, timeout=180,
        )
        if npm.returncode != 0:
            await _push(session_id, {"type": "error", "msg": f"npm install failed:\n{npm.stderr[-400:]}"})
            return None
        await _push(session_id, {"type": "progress", "msg": "Dependencies installed"})

        # ── Stage 6: vite build ───────────────────────────────────────────────
        await _push(session_id, {"type": "progress", "msg": "Building production bundle..."})
        build = subprocess.run(
            ["npm", "run", "build"], cwd=project_dir,
            capture_output=True, text=True, timeout=120,
        )
        if build.returncode != 0:
            await _push(session_id, {"type": "error", "msg": f"vite build failed:\n{build.stdout[-300:]}\n{build.stderr[-300:]}"})
            return None
        await _push(session_id, {"type": "progress", "msg": "Build complete ✓"})

        # ── Stage 7: Zip source for download ──────────────────────────────────
        zip_path = f"/tmp/ramble_{session_id}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fp in generated:
                    full = os.path.join(project_dir, fp)
                    if os.path.exists(full):
                        zf.write(full, fp)
        except Exception as e:
            print(f"WARNING: zip failed: {e}")

        # ── Stage 8: Vercel deploy ────────────────────────────────────────────
        dist_dir = os.path.join(project_dir, "dist")
        if not os.path.exists(dist_dir):
            await _push(session_id, {"type": "error", "msg": "dist/ not found after build"})
            return None

        project_name = f"ramble-{uuid.uuid4().hex[:8]}"
        await _push(session_id, {"type": "progress", "msg": f"Deploying to Vercel as {project_name}..."})

        vercel = subprocess.run(
            ["vercel", "--yes", "--name", project_name, "--prod",
             "--token", os.environ.get("VERCEL_TOKEN", "")],
            cwd=dist_dir, capture_output=True, text=True, timeout=300,
        )
        print(f"VERCEL STDOUT:\n{vercel.stdout}")

        deploy_url = None
        for line in reversed(vercel.stdout.strip().split("\n")):
            line = line.strip()
            if line.startswith("https://"):
                deploy_url = line
                break

        if deploy_url:
            await _push(session_id, {"type": "progress", "msg": f"✓ Deployed: {deploy_url}"})
        else:
            await _push(session_id, {"type": "error", "msg": "Could not extract Vercel URL"})
            return None

        # ── Save session ──────────────────────────────────────────────────────
        save_session(session_id, {
            "spec":         spec,
            "file_map":     generated,
            "deploy_url":   deploy_url,
            "app_name":     app_name if not is_edit else load_session(session_id).get("app_name"),
            "npm_name":     npm_name,
            "plan":         plan,
            "zip_path":     zip_path,
            "conversation": conversation_history,
        })

        await _push(session_id, {"type": "done", "deploy_url": deploy_url, "session_id": session_id})
        return deploy_url

    except subprocess.TimeoutExpired as e:
        await _push(session_id, {"type": "error", "msg": f"Timed out: {e}"})
        return None
    except Exception as e:
        await _push(session_id, {"type": "error", "msg": f"Build error: {e}"})
        return None
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.post("/reset")
async def reset():
    conversation_history.clear()
    return {"ok": True}


@app.get("/sessions")
async def get_sessions():
    """List recent sessions for history panel."""
    return JSONResponse(list_sessions())


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Restore a session by ID (for Safari reload recovery)."""
    data = load_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    # Return everything except the large file_map
    return JSONResponse({k: v for k, v in data.items() if k != "file_map"})


@app.get("/download/{session_id}")
async def download_project(session_id: str):
    data = load_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    zip_path = data.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Source zip not found (may have expired)")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{data.get('app_name','app')}.zip")


@app.get("/progress/{session_id}")
async def stream_progress(session_id: str):
    """
    SSE endpoint. The frontend connects here after receiving session_id from /chat.
    Streams build progress events until a 'done' or 'error' event is sent.
    """
    _get_or_create_queue(session_id)  # pre-create so build task can push immediately

    return StreamingResponse(
        _sse_generator(session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@app.post("/chat")
async def chat(audio: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    # ── 1. Save audio ─────────────────────────────────────────────────────────
    try:
        audio_bytes = await audio.read()
        if   audio_bytes[:4]  == b'fLaC':                              suffix = ".flac"; ftype = "audio/flac"
        elif audio_bytes[4:8] == b'ftyp' or audio_bytes[:4] == b'\x00\x00\x00\x1c': suffix = ".mp4";  ftype = "audio/mp4"
        elif audio_bytes[:4]  == b'OggS':                              suffix = ".ogg";  ftype = "audio/ogg"
        elif audio_bytes[:4]  == b'RIFF':                              suffix = ".wav";  ftype = "audio/wav"
        elif audio_bytes[:3]  == b'ID3' or audio_bytes[:2] == b'\xff\xfb': suffix = ".mp3"; ftype = "audio/mpeg"
        else:                                                           suffix = ".mp4";  ftype = "audio/mp4"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio save error: {e}")

    # ── 2. Whisper ────────────────────────────────────────────────────────────
    try:
        if ftype == "audio/mp4":
            with open(tmp_path, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.mp4", f, "audio/mp4"))
        else:
            wav = tmp_path + ".wav"
            r = subprocess.run(["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav],
                                capture_output=True, text=True)
            if r.returncode != 0:
                raise Exception(f"ffmpeg: {r.stderr}")
            with open(wav, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.wav", f, "audio/wav"))
            try: os.unlink(wav)
            except: pass
        user_text = tx.text.strip()
        print(f"TRANSCRIPT: {user_text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Whisper error: {e}")
    finally:
        try: os.unlink(tmp_path)
        except: pass

    if not user_text:
        raise HTTPException(status_code=400, detail="Empty transcript")

    # ── 3. Claude advisor ─────────────────────────────────────────────────────
    conversation_history.append({"role": "user", "content": user_text})
    try:
        resp = anthropic_sync.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=1000,
            system=ADVISOR_SYSTEM, messages=conversation_history,
        )
        raw = resp.content[0].text.strip()
    except Exception as e:
        conversation_history.pop()
        raise HTTPException(status_code=500, detail=f"Claude error: {e}")

    try:
        clean = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip() if raw.startswith("```") else raw
        data  = json.loads(clean)
    except Exception as e:
        conversation_history.pop()
        raise HTTPException(status_code=500, detail=f"Advisor JSON parse error: {e}")

    spoken  = data.get("message", "Sorry, can you say that again?")
    complete = data.get("complete", False)
    spec    = data.get("spec", None)
    conversation_history.append({"role": "assistant", "content": raw})

    # ── 4. TTS ────────────────────────────────────────────────────────────────
    try:
        tts = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=spoken)
        audio_filename = f"resp_{uuid.uuid4().hex}.mp3"
        tts.stream_to_file(f"/tmp/{audio_filename}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS error: {e}")

    # ── 5. If spec complete: kick off build as background task ────────────────
    session_id = None
    if complete and spec:
        session_id = uuid.uuid4().hex
        _get_or_create_queue(session_id)  # pre-create queue BEFORE returning session_id
        background_tasks.add_task(build_and_deploy, spec, session_id)

    return JSONResponse({
        "transcript": user_text,
        "message":    spoken,
        "audio_url":  f"/audio/{audio_filename}",
        "complete":   complete,
        "spec":       spec,
        "session_id": session_id,  # frontend uses this to connect SSE
    })


@app.post("/edit")
async def edit(audio: UploadFile = File(...), session_id: str = "", background_tasks: BackgroundTasks = BackgroundTasks()):
    """
    Post-deploy iteration endpoint. User describes a change to their deployed app.
    Returns immediately; streams updated deploy URL via /progress/{new_session_id}.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    session_data = load_session(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail="Session not found — please start over")

    spec     = session_data.get("spec", {})
    file_map = session_data.get("file_map", {})

    # ── Transcribe ────────────────────────────────────────────────────────────
    try:
        audio_bytes = await audio.read()
        suffix = ".mp4" if audio_bytes[4:8] == b'ftyp' else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        if suffix == ".mp4":
            with open(tmp_path, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.mp4", f, "audio/mp4"))
        else:
            wav = tmp_path + ".wav"
            subprocess.run(["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav],
                           capture_output=True)
            with open(wav, "rb") as f:
                tx = openai_client.audio.transcriptions.create(model="whisper-1", file=("audio.wav", f, "audio/wav"))
            try: os.unlink(wav)
            except: pass
        user_text = tx.text.strip()
        print(f"EDIT TRANSCRIPT: {user_text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription error: {e}")
    finally:
        try: os.unlink(tmp_path)
        except: pass

    # ── Editor advisor ────────────────────────────────────────────────────────
    file_list = "\n".join(f"  {p}" for p in file_map.keys())
    prompt = EDITOR_USER_TEMPLATE.format(
        spec_json    = json.dumps(spec, indent=2),
        user_request = user_text,
        file_list    = file_list,
    )
    try:
        resp = anthropic_sync.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=500,
            system=EDITOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw  = resp.content[0].text.strip()
        edit_data = json.loads(raw.split("\n", 1)[1].rsplit("```", 1)[0].strip() if raw.startswith("```") else raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Editor error: {e}")

    spoken          = edit_data.get("message", "Making that change now.")
    ready           = edit_data.get("ready", False)
    files_to_change = edit_data.get("files_to_change", [])
    change_desc     = edit_data.get("change_description", "")

    # ── TTS ───────────────────────────────────────────────────────────────────
    try:
        tts = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=spoken)
        audio_filename = f"resp_{uuid.uuid4().hex}.mp3"
        tts.stream_to_file(f"/tmp/{audio_filename}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS error: {e}")

    # ── If ready: kick off targeted regeneration as background task ───────────
    new_session_id = None
    if ready and files_to_change:
        new_session_id = uuid.uuid4().hex
        _get_or_create_queue(new_session_id)

        # Enrich file descriptions with the specific change needed
        enriched_session = dict(session_data)
        if enriched_session.get("plan") and enriched_session["plan"].get("files"):
            for f in enriched_session["plan"]["files"]:
                if f["path"] in files_to_change:
                    f["description"] = f"{f['description']}\n\nCHANGE REQUESTED: {change_desc}"

        background_tasks.add_task(
            build_and_deploy, spec, new_session_id,
            file_map_override=file_map,
            files_to_regen=files_to_change,
        )

    return JSONResponse({
        "transcript":    user_text,
        "message":       spoken,
        "audio_url":     f"/audio/{audio_filename}",
        "ready":         ready,
        "needs_clarify": not ready,
        "session_id":    new_session_id,  # new session for the updated deploy
    })


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    path = f"/tmp/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/mpeg")