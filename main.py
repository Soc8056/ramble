import os
import json
import uuid
import asyncio
import subprocess
import tempfile
import shutil
import zipfile
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv
import anthropic
from openai import OpenAI

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    # Sync client for advisor + TTS (called once per turn, don't need async)
    anthropic_sync = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # Async client for parallel file generation
    anthropic_async = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    print("✓ API clients initialized")
except Exception as e:
    print(f"ERROR initializing API clients: {e}")
    raise

# Per-user conversation state keyed by session cookie / IP
# For now: single-user dict (Phase 3 adds real auth)
conversation_history = []

# { session_id: { "zip_path": str, "deploy_url": str, "app_name": str } }
generated_sessions = {}

from spec_prompt import ADVISOR_SYSTEM
from orchestrator_prompt import ORCHESTRATOR_SYSTEM, ORCHESTRATOR_USER_TEMPLATE
from file_prompt import FILE_SYSTEM, FILE_USER_TEMPLATE
from integration_prompt import INTEGRATION_SYSTEM, INTEGRATION_USER_TEMPLATE


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.post("/reset")
async def reset():
    conversation_history.clear()
    print("Conversation reset")
    return {"ok": True}


@app.get("/download/{session_id}")
async def download_project(session_id: str):
    if session_id not in generated_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    zip_path = generated_sessions[session_id]["zip_path"]
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Zip file not found")
    app_name = generated_sessions[session_id].get("app_name", "ramble-project")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{app_name}.zip")


@app.post("/chat")
async def chat(audio: UploadFile = File(...)):

    # 1. Save and detect audio format
    try:
        audio_bytes = await audio.read()
        if audio_bytes[:4] == b'fLaC':
            suffix, fname, ftype = ".flac", "audio.flac", "audio/flac"
        elif audio_bytes[4:8] == b'ftyp' or audio_bytes[:4] == b'\x00\x00\x00\x1c':
            suffix, fname, ftype = ".mp4", "audio.mp4", "audio/mp4"
        elif audio_bytes[:4] == b'OggS':
            suffix, fname, ftype = ".ogg", "audio.ogg", "audio/ogg"
        elif audio_bytes[:4] == b'RIFF':
            suffix, fname, ftype = ".wav", "audio.wav", "audio/wav"
        elif audio_bytes[:3] == b'ID3' or audio_bytes[:2] == b'\xff\xfb':
            suffix, fname, ftype = ".mp3", "audio.mp3", "audio/mpeg"
        else:
            suffix, fname, ftype = ".mp4", "audio.mp4", "audio/mp4"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        print(f"Audio saved: {len(audio_bytes)} bytes, type={ftype}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio save error: {e}")

    # 2. Transcribe with Whisper
    try:
        if ftype == "audio/mp4":
            with open(tmp_path, "rb") as f:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.mp4", f, "audio/mp4"),
                )
        else:
            converted_path = tmp_path + ".wav"
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", "-f", "wav", converted_path],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                raise Exception(f"ffmpeg failed: {result.stderr}")
            with open(converted_path, "rb") as f:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.wav", f, "audio/wav"),
                )
            try:
                os.unlink(converted_path)
            except Exception:
                pass

        user_text = transcript.text.strip()
        print(f"TRANSCRIPT: {user_text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Whisper error: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if not user_text:
        raise HTTPException(status_code=400, detail="Empty transcript")

    # 3. Claude advisor
    conversation_history.append({"role": "user", "content": user_text})

    try:
        claude_response = anthropic_sync.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=ADVISOR_SYSTEM,
            messages=conversation_history,
        )
        raw_text = claude_response.content[0].text.strip()
        print(f"CLAUDE RAW: {raw_text}")
    except Exception as e:
        conversation_history.pop()
        raise HTTPException(status_code=500, detail=f"Claude error: {e}")

    # 4. Parse advisor JSON
    try:
        clean = raw_text
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        conversation_history.pop()
        raise HTTPException(status_code=500, detail=f"Claude returned invalid JSON: {e}")

    spoken_message = data.get("message", "Sorry, can you say that again?")
    is_complete = data.get("complete", False)
    spec = data.get("spec", None)

    conversation_history.append({"role": "assistant", "content": raw_text})

    # 5. TTS
    try:
        tts_response = openai_client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=spoken_message,
        )
        audio_filename = f"response_{uuid.uuid4().hex}.mp3"
        audio_out_path = f"/tmp/{audio_filename}"
        tts_response.stream_to_file(audio_out_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS error: {e}")

    # 6. Generate and deploy if spec is complete
    deploy_url = None
    session_id = None
    if is_complete and spec:
        print("SPEC COMPLETE — starting multi-stage generation pipeline...")
        session_id, deploy_url = await generate_and_deploy(spec)

    return JSONResponse({
        "transcript": user_text,
        "message": spoken_message,
        "audio_url": f"/audio/{audio_filename}",
        "complete": is_complete,
        "spec": spec,
        "deploy_url": deploy_url,
        "session_id": session_id,
    })


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    path = f"/tmp/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(path, media_type="audio/mpeg")


# ─────────────────────────────────────────────
# STAGE 1: ORCHESTRATOR
# ─────────────────────────────────────────────

async def run_orchestrator(spec: dict) -> dict | None:
    """
    Single call that returns the complete file plan and design tokens.
    No code generated here — just the blueprint.
    """
    prompt = ORCHESTRATOR_USER_TEMPLATE.format(
        spec_json=json.dumps(spec, indent=2)
    )

    print("  [orchestrator] calling Claude...")
    try:
        resp = await anthropic_async.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=ORCHESTRATOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        plan = json.loads(raw)
        files = plan.get("files", [])
        tokens = plan.get("design_tokens", {})
        print(f"  [orchestrator] plan: {len(files)} files, app_name={tokens.get('app_name')}")
        return plan
    except Exception as e:
        print(f"  [orchestrator] ERROR: {e}")
        return None


# ─────────────────────────────────────────────
# STAGE 2: PER-FILE GENERATION
# ─────────────────────────────────────────────

def topological_batches(files: list[dict]) -> list[list[dict]]:
    """
    Group files into batches where each batch can run in parallel.
    A file is ready when all its depends_on files are already generated.
    """
    completed: set[str] = set()
    remaining = list(files)
    batches = []

    while remaining:
        ready = []
        not_ready = []
        for f in remaining:
            deps = f.get("depends_on", [])
            if all(d in completed for d in deps):
                ready.append(f)
            else:
                not_ready.append(f)

        if not ready:
            # Circular dep or bad plan — force all remaining into one batch
            print(f"  [topo] WARNING: circular deps detected, forcing {len(remaining)} files into one batch")
            batches.append(remaining)
            break

        batches.append(ready)
        for f in ready:
            completed.add(f["path"])
        remaining = not_ready

    return batches


def format_dependency_contents(file_info: dict, generated: dict[str, str]) -> str:
    """
    Build the dependency_contents block for a file generation prompt.
    Shows the actual content of each file this file imports from.
    """
    deps = file_info.get("depends_on", [])
    if not deps:
        return "(no project file dependencies)"

    blocks = []
    for dep_path in deps:
        content = generated.get(dep_path)
        if content:
            blocks.append(f"=== {dep_path} ===\n{content}\n")
        else:
            blocks.append(f"=== {dep_path} ===\n(not yet generated — do not import)\n")
    return "\n".join(blocks)


async def generate_single_file(
    file_info: dict,
    spec: dict,
    plan: dict,
    generated: dict[str, str],
) -> tuple[str, str]:
    """
    Generate one file. Returns (path, content).
    Retries once if output looks clearly incomplete.
    """
    path = file_info["path"]
    all_files_list = "\n".join(f["path"] for f in plan["files"])
    dep_contents = format_dependency_contents(file_info, generated)

    prompt = FILE_USER_TEMPLATE.format(
        file_path=path,
        file_description=file_info.get("description", ""),
        file_complexity=file_info.get("complexity", "medium"),
        spec_json=json.dumps(spec, indent=2),
        design_tokens_json=json.dumps(plan.get("design_tokens", {}), indent=2),
        all_files_list=all_files_list,
        dependency_contents=dep_contents,
        platform=spec.get("platform", "both"),
    )

    # Token budget by complexity
    max_tokens_map = {"low": 2000, "medium": 4000, "high": 8000}
    max_tokens = max_tokens_map.get(file_info.get("complexity", "medium"), 4000)

    for attempt in range(2):
        try:
            resp = await anthropic_async.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=max_tokens,
                system=FILE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            content = resp.content[0].text.strip()

            # Sanity check: content shouldn't be suspiciously short for complex files
            if file_info.get("complexity") == "high" and len(content) < 500:
                print(f"  [file] WARNING: {path} returned only {len(content)} chars on attempt {attempt+1}")
                if attempt == 0:
                    continue

            # Strip accidental markdown fences
            if content.startswith("```"):
                lines = content.split("\n")
                # Remove first line (```jsx or similar) and last ``` if present
                content = "\n".join(lines[1:])
                if content.endswith("```"):
                    content = content[:-3].rstrip()

            print(f"  [file] ✓ {path} ({len(content)} chars)")
            return path, content

        except Exception as e:
            print(f"  [file] ERROR generating {path} (attempt {attempt+1}): {e}")
            if attempt == 1:
                # Return a minimal stub so the build can at least attempt
                stub = _make_stub(path, file_info)
                return path, stub

    stub = _make_stub(path, file_info)
    return path, stub


def _make_stub(path: str, file_info: dict) -> str:
    """Last-resort stub when generation fails completely."""
    name = Path(path).stem
    if path.endswith(".json"):
        return '{"name":"ramble-app","private":true,"version":"0.1.0","type":"module","scripts":{"dev":"vite","build":"vite build"},"dependencies":{"react":"^18.2.0","react-dom":"^18.2.0","react-router-dom":"^6.8.0","lucide-react":"^0.263.1"},"devDependencies":{"@vitejs/plugin-react":"^4.0.0","vite":"^4.4.0"}}'
    if path.endswith(".jsx") or path.endswith(".js"):
        return f"export default function {name}() {{ return <div style={{{{padding:'2rem', color:'#888'}}}}>Failed to generate {name}</div>; }}\n"
    return f"/* failed to generate {path} */\n"


async def generate_all_files(
    spec: dict,
    plan: dict,
) -> dict[str, str]:
    """
    Run the full parallel generation pipeline.
    Files are batched by dependency order; each batch runs in parallel.
    """
    files = plan.get("files", [])
    batches = topological_batches(files)

    print(f"  [pipeline] {len(files)} files across {len(batches)} batches")
    for i, batch in enumerate(batches):
        paths = [f["path"] for f in batch]
        print(f"  [pipeline] batch {i+1}/{len(batches)}: {paths}")

    generated: dict[str, str] = {}

    for batch_idx, batch in enumerate(batches):
        print(f"  [pipeline] starting batch {batch_idx+1} ({len(batch)} files in parallel)...")
        tasks = [generate_single_file(f, spec, plan, generated) for f in batch]
        results = await asyncio.gather(*tasks)
        for path, content in results:
            generated[path] = content

    return generated


# ─────────────────────────────────────────────
# STAGE 3: INTEGRATION PASS
# ─────────────────────────────────────────────

async def run_integration_pass(spec: dict, file_map: dict[str, str]) -> dict[str, str]:
    """
    Single pass that reviews all generated files together and fixes
    cross-file bugs: broken imports, prop mismatches, missing exports, bad routes.
    Only touches files that actually need fixing.
    """
    # Cap total input size — send only JSX/JS files (config files rarely have cross-file bugs)
    js_files = {k: v for k, v in file_map.items() if k.endswith(('.jsx', '.js', '.ts', '.tsx'))}

    files_payload = json.dumps(js_files, indent=2)

    # If the payload is huge, trim file contents to first 3000 chars each for the review
    if len(files_payload) > 80000:
        trimmed = {k: v[:3000] + "\n// ... (truncated for review)" if len(v) > 3000 else v
                   for k, v in js_files.items()}
        files_payload = json.dumps(trimmed, indent=2)
        print("  [integration] payload trimmed for review pass")

    prompt = INTEGRATION_USER_TEMPLATE.format(files_json=files_payload)

    print("  [integration] running cross-file review...")
    try:
        resp = await anthropic_async.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=12000,
            system=INTEGRATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)
        fixes = result.get("fixes", [])

        if not fixes:
            print("  [integration] no fixes needed")
            return file_map

        print(f"  [integration] applying {len(fixes)} fix(es): {[f['path'] for f in fixes]}")
        updated = dict(file_map)
        for fix in fixes:
            path = fix.get("path")
            content = fix.get("content")
            if path and content and path in updated:
                updated[path] = content
                print(f"  [integration] ✓ fixed {path}")
        return updated

    except Exception as e:
        print(f"  [integration] ERROR (skipping): {e}")
        return file_map  # Return unmodified if integration pass fails


# ─────────────────────────────────────────────
# STAGE 4: BUILD + DEPLOY
# ─────────────────────────────────────────────

async def run_subprocess(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Run a subprocess without blocking the event loop."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"Command {cmd[0]} timed out after {timeout}s")


async def build_and_deploy(
    file_map: dict[str, str],
    app_name: str,
    session_id: str,
) -> str | None:
    """Write files, npm install, vite build, deploy to Vercel. Returns deploy URL or None."""

    project_dir = tempfile.mkdtemp()

    try:
        # Write all files
        for file_path, content in file_map.items():
            full_path = os.path.join(project_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
        print(f"  [build] wrote {len(file_map)} files to {project_dir}")

        # npm install
        print("  [build] npm install...")
        code, stdout, stderr = await run_subprocess(
            ["npm", "install"], project_dir, timeout=180
        )
        if code != 0:
            print(f"  [build] npm install failed:\n{stderr[-1000:]}")
            return None
        print("  [build] npm install ✓")

        # vite build
        print("  [build] vite build...")
        code, stdout, stderr = await run_subprocess(
            ["npm", "run", "build"], project_dir, timeout=120
        )
        if code != 0:
            print(f"  [build] vite build failed:\nSTDOUT:{stdout[-500:]}\nSTDERR:{stderr[-500:]}")
            return None
        print("  [build] vite build ✓")

        # Zip source for download
        zip_path = f"/tmp/ramble_{session_id}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in file_map.keys():
                    full_path = os.path.join(project_dir, file_path)
                    if os.path.exists(full_path):
                        zf.write(full_path, file_path)
            generated_sessions[session_id] = {
                "zip_path": zip_path,
                "app_name": app_name,
                "deploy_url": None,
            }
            print(f"  [build] source zip created: {zip_path}")
        except Exception as e:
            print(f"  [build] zip error (non-fatal): {e}")

        # Deploy to Vercel
        dist_dir = os.path.join(project_dir, "dist")
        if not os.path.exists(dist_dir):
            print("  [build] dist/ not found after build")
            return None

        project_name = f"ramble-{uuid.uuid4().hex[:8]}"
        print(f"  [deploy] deploying to Vercel as {project_name}...")

        code, stdout, stderr = await run_subprocess(
            [
                "vercel", "--yes",
                "--name", project_name,
                "--prod",
                "--token", os.environ.get("VERCEL_TOKEN", ""),
            ],
            dist_dir,
            timeout=300,
        )

        print(f"  [deploy] VERCEL STDOUT:\n{stdout}")
        if stderr:
            print(f"  [deploy] VERCEL STDERR:\n{stderr}")

        deploy_url = None
        for line in reversed(stdout.strip().split("\n")):
            line = line.strip()
            if line.startswith("https://"):
                deploy_url = line
                break

        if deploy_url:
            print(f"  [deploy] ✓ deployed: {deploy_url}")
            if session_id in generated_sessions:
                generated_sessions[session_id]["deploy_url"] = deploy_url
        else:
            print("  [deploy] WARNING: could not extract deploy URL")

        return deploy_url

    except TimeoutError as e:
        print(f"  [build] TIMEOUT: {e}")
        return None
    except Exception as e:
        print(f"  [build] ERROR: {e}")
        return None
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

async def generate_and_deploy(spec: dict) -> tuple[str | None, str | None]:
    """
    Full 4-stage pipeline:
    1. Orchestrator  → file plan + design tokens
    2. File gen      → parallel per-file Claude calls (batched by dependency)
    3. Integration   → cross-file bug fixes
    4. Build+deploy  → npm install → vite build → vercel deploy
    """
    session_id = uuid.uuid4().hex
    print(f"\n{'='*50}")
    print(f"GENERATION PIPELINE — session {session_id}")
    print(f"{'='*50}")

    # Stage 1: Orchestrate
    print("\n[STAGE 1] Orchestrating...")
    plan = await run_orchestrator(spec)
    if not plan:
        print("[STAGE 1] FAILED — aborting")
        return session_id, None

    design_tokens = plan.get("design_tokens", {})
    app_name = design_tokens.get("app_name", "ramble-app")
    print(f"[STAGE 1] ✓ app_name={app_name}, {len(plan['files'])} files planned")

    # Stage 2: Generate files
    print("\n[STAGE 2] Generating files...")
    file_map = await generate_all_files(spec, plan)
    print(f"[STAGE 2] ✓ generated {len(file_map)} files")

    # Stage 3: Integration pass
    print("\n[STAGE 3] Integration review...")
    file_map = await run_integration_pass(spec, file_map)
    print("[STAGE 3] ✓ integration complete")

    # Stage 4: Build and deploy
    print("\n[STAGE 4] Build + deploy...")
    deploy_url = await build_and_deploy(file_map, app_name, session_id)

    print(f"\n{'='*50}")
    if deploy_url:
        print(f"PIPELINE COMPLETE ✓ → {deploy_url}")
    else:
        print("PIPELINE COMPLETE — deploy failed (check logs above)")
    print(f"{'='*50}\n")

    return session_id, deploy_url