import os
import json
import uuid
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
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    print("✓ API clients initialized")
except Exception as e:
    print(f"ERROR initializing API clients: {e}")
    raise

# Single-user in-memory conversation state
conversation_history = []

# Store generated session data for download
# { session_id: { "zip_path": str, "deploy_url": str, "app_name": str } }
generated_sessions = {}


from spec_prompt import ADVISOR_SYSTEM
from builder_prompt import BUILDER_SYSTEM, BUILDER_USER_TEMPLATE


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
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{app_name}.zip"
    )


@app.post("/chat")
async def chat(audio: UploadFile = File(...)):

    # 1. Save audio to temp file
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
        print(f"Audio saved: {len(audio_bytes)} bytes, type={ftype} → {tmp_path}")
    except Exception as e:
        print(f"ERROR saving audio: {e}")
        raise HTTPException(status_code=500, detail=f"Audio save error: {e}")

    # 2. Transcribe with Whisper
    try:
        if ftype in ("audio/mp4",):
            with open(tmp_path, "rb") as f:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=("audio.mp4", f, "audio/mp4"),
                )
        else:
            converted_path = tmp_path + ".wav"
            ffmpeg_result = subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1", "-f", "wav", converted_path],
                capture_output=True, text=True
            )
            if ffmpeg_result.returncode != 0:
                print(f"ffmpeg stderr: {ffmpeg_result.stderr}")
                raise Exception(f"ffmpeg conversion failed: {ffmpeg_result.stderr}")
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
        print(f"ERROR in Whisper: {e}")
        raise HTTPException(status_code=500, detail=f"Whisper error: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if not user_text:
        raise HTTPException(status_code=400, detail="Empty transcript — no speech detected")

    # 3. Call Claude advisor
    conversation_history.append({"role": "user", "content": user_text})

    try:
        claude_response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=ADVISOR_SYSTEM,
            messages=conversation_history,
        )
        raw_text = claude_response.content[0].text.strip()
        print(f"CLAUDE RAW: {raw_text}")
    except Exception as e:
        print(f"ERROR calling Claude: {e}")
        conversation_history.pop()
        raise HTTPException(status_code=500, detail=f"Claude error: {e}")

    # 4. Parse advisor JSON
    try:
        clean = raw_text
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"ERROR parsing Claude JSON: {e}\nRaw was: {raw_text}")
        conversation_history.pop()
        raise HTTPException(status_code=500, detail=f"Claude returned invalid JSON: {e}")

    spoken_message = data.get("message", "Sorry, I lost my train of thought. Can you say that again?")
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
        print(f"TTS written: {audio_out_path}")
    except Exception as e:
        print(f"ERROR in TTS: {e}")
        raise HTTPException(status_code=500, detail=f"TTS error: {e}")

    # 6. Generate and deploy if complete
    deploy_url = None
    session_id = None
    if is_complete and spec:
        print("SPEC COMPLETE — generating React app and deploying...")
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
# GENERATE AND DEPLOY
# ─────────────────────────────────────────────

async def generate_and_deploy(spec: dict) -> tuple[str | None, str | None]:
    session_id = uuid.uuid4().hex
    platform = spec.get("platform", "both")

    # 1. Generate full React project as JSON file map
    try:
        spec_json = json.dumps(spec, indent=2)
        prompt = BUILDER_USER_TEMPLATE.format(
            spec_json=spec_json,
            platform=platform
        )

        print("Calling Claude builder...")
        gen_response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8096,
            system=BUILDER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_output = gen_response.content[0].text.strip()

        if raw_output.startswith("```"):
            raw_output = raw_output.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        print(f"Builder raw output: {len(raw_output)} chars")
    except Exception as e:
        print(f"ERROR calling Claude builder: {e}")
        return None, None

    # 2. Parse file map
    try:
        file_map = json.loads(raw_output)
        print(f"Generated {len(file_map)} files: {list(file_map.keys())}")
    except json.JSONDecodeError as e:
        print(f"ERROR parsing builder JSON: {e}")
        print(f"Raw output preview: {raw_output[:500]}")
        return None, None

    # 3. Get app name from package.json
    app_name = "ramble-app"
    try:
        if "package.json" in file_map:
            pkg = json.loads(file_map["package.json"])
            app_name = pkg.get("name", "ramble-app")
    except Exception:
        pass

    # 4. Write all files to temp directory
    project_dir = tempfile.mkdtemp()
    try:
        for file_path, content in file_map.items():
            full_path = os.path.join(project_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
        print(f"Files written to: {project_dir}")
    except Exception as e:
        print(f"ERROR writing project files: {e}")
        shutil.rmtree(project_dir, ignore_errors=True)
        return None, None

    # 5. npm install
    try:
        print("Running npm install...")
        npm_result = subprocess.run(
            ["npm", "install"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if npm_result.returncode != 0:
            print(f"npm install failed:\n{npm_result.stderr[-1000:]}")
            raise Exception("npm install failed")
        print("npm install complete")
    except subprocess.TimeoutExpired:
        print("ERROR: npm install timed out")
        shutil.rmtree(project_dir, ignore_errors=True)
        return None, None
    except Exception as e:
        print(f"ERROR in npm install: {e}")
        shutil.rmtree(project_dir, ignore_errors=True)
        return None, None

    # 6. vite build
    try:
        print("Running vite build...")
        build_result = subprocess.run(
            ["npm", "run", "build"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if build_result.returncode != 0:
            print(f"vite build failed:\nSTDOUT: {build_result.stdout[-500:]}\nSTDERR: {build_result.stderr[-500:]}")
            raise Exception("vite build failed")
        print("vite build complete")
    except subprocess.TimeoutExpired:
        print("ERROR: vite build timed out")
        shutil.rmtree(project_dir, ignore_errors=True)
        return None, None
    except Exception as e:
        print(f"ERROR in vite build: {e}")
        shutil.rmtree(project_dir, ignore_errors=True)
        return None, None

    # 7. Zip source files for download
    zip_path = f"/tmp/ramble_{session_id}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in file_map.keys():
                full_path = os.path.join(project_dir, file_path)
                if os.path.exists(full_path):
                    zf.write(full_path, file_path)
        print(f"Source zip created: {zip_path}")
        generated_sessions[session_id] = {
            "zip_path": zip_path,
            "app_name": app_name,
            "deploy_url": None,
        }
    except Exception as e:
        print(f"ERROR creating zip: {e}")

    # 8. Deploy dist/ to Vercel
    deploy_url = None
    try:
        dist_dir = os.path.join(project_dir, "dist")
        if not os.path.exists(dist_dir):
            print("ERROR: dist/ not found after build")
            shutil.rmtree(project_dir, ignore_errors=True)
            return session_id, None

        project_name = f"ramble-{uuid.uuid4().hex[:8]}"
        print(f"Deploying dist/ to Vercel as: {project_name}")

        result = subprocess.run(
            [
                "vercel", "--yes",
                "--name", project_name,
                "--prod",
                "--token", os.environ.get("VERCEL_TOKEN", ""),
            ],
            cwd=dist_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )

        print(f"VERCEL STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"VERCEL STDERR:\n{result.stderr}")

        for line in reversed(result.stdout.strip().split("\n")):
            line = line.strip()
            if line.startswith("https://"):
                deploy_url = line
                break

        if deploy_url:
            print(f"✓ DEPLOYED: {deploy_url}")
            if session_id in generated_sessions:
                generated_sessions[session_id]["deploy_url"] = deploy_url
        else:
            print("WARNING: Could not extract deploy URL from Vercel output")

    except subprocess.TimeoutExpired:
        print("ERROR: Vercel deploy timed out after 300s")
    except FileNotFoundError:
        print("ERROR: vercel CLI not found")
    except Exception as e:
        print(f"ERROR deploying to Vercel: {e}")
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)

    return session_id, deploy_url