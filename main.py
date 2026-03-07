import os
import json
import uuid
import subprocess
import tempfile
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

ADVISOR_SYSTEM = """You are a senior software architect doing a voice interview to fully understand someone's software idea before building it. You are concise because this is audio — keep every response to 2-3 short sentences maximum.

Your goal is to extract a complete, buildable picture of the product through natural conversation. You need ALL of these before you can mark the spec complete:

1. Core function — what does this app actually do? What is the one thing it accomplishes?
2. The user — who is using it, what are they doing right before they open this app, what do they need to walk away with?
3. Key screens or views — what does the user actually see and interact with? Walk through it like you're describing a demo.
4. Core features — what are the must-have features for this to feel complete? What can be cut?
5. Data and state — what information does the app need to store, display, or manipulate?
6. Interactions and flow — how does a user move through the app? What triggers what?
7. Visual direction — any strong opinions on how it should look or feel? Minimal, dense, playful, serious?

Rules:
- Ask exactly ONE question per turn. Never stack questions.
- If an answer is vague, push back ONCE with a more specific question before moving on.
- You are building this — ask like an engineer who needs to write the code, not a consultant filling out a form.
- Keep a mental model of what you already know and only ask what you still need.
- Be direct and collaborative. This is a working session, not an interview.
- Do NOT mark complete until you have a clear, specific answer for all 7 items above.

YOU MUST RESPOND WITH RAW JSON ONLY. No markdown, no backticks, no explanation. Just the JSON object.

While gathering info:
{"message": "your 2-3 sentence spoken response with the next question", "complete": false, "spec": null}

Only when ALL 7 items are gathered with specific, buildable answers:
{"message": "Got it. I have a clear picture of what you want. Give me a moment to build it and get it live.", "complete": true, "spec": {"core_function": "...", "target_user": "...", "key_screens": "...", "core_features": "...", "data_and_state": "...", "interactions_and_flow": "...", "visual_direction": "..."}}"""


BUILDER_SYSTEM = """You are an expert frontend engineer who builds beautiful, functional single-file web apps. You write clean, modern HTML/CSS/JS. You have strong opinions about design and you execute them precisely."""

BUILDER_USER_TEMPLATE = """Build a complete, working single-file web app based on this product spec. This should be a real, functional implementation — not a landing page, not a mockup. The core feature must actually work in the browser using JavaScript.

Product Spec:
{spec_json}

Requirements:
1. Invent a great product name that fits the idea
2. The core feature described in the spec must be fully functional — real interactivity, real state management, real user flows
3. If the spec requires data persistence, use localStorage
4. If the spec requires an API the browser can't call, simulate it convincingly with realistic fake data and setTimeout loading states
5. Navigation between screens/views must work
6. Use Tailwind CSS from CDN for styling: <script src="https://cdn.tailwindcss.com"></script>
7. Typography: import one distinctive Google Font that fits the visual direction, use it as the primary font
8. The design must match the visual direction in the spec — if they said minimal, be minimal; if they said dense or data-heavy, build that
9. Mobile responsive
10. A small footer: "[Product Name] · Built with Ramble"

Return ONLY the raw HTML. Start with <!DOCTYPE html>. Zero explanation. Zero markdown."""


@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.post("/reset")
async def reset():
    conversation_history.clear()
    print("Conversation reset")
    return {"ok": True}


@app.post("/chat")
async def chat(audio: UploadFile = File(...)):
    # 1. Save audio to temp file
    try:
        audio_bytes = await audio.read()
        suffix = ".webm"
        if audio.content_type and "mp4" in audio.content_type:
            suffix = ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        print(f"Audio saved: {len(audio_bytes)} bytes → {tmp_path}")
    except Exception as e:
        print(f"ERROR saving audio: {e}")
        raise HTTPException(status_code=500, detail=f"Audio save error: {e}")

    # 2. Transcribe with Whisper
    try:
        with open(tmp_path, "rb") as f:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=("audio.webm", f, "audio/webm"),
            )
            user_text = transcript.text.strip()
            print(f"TRANSCRIPT: {user_text}")
    except Exception as e:
        print(f"ERROR in Whisper: {e}")
        os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=f"Whisper error: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if not user_text:
        raise HTTPException(status_code=400, detail="Empty transcript — no speech detected")

    # 3. Add user message to history and call Claude
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

    # 4. Parse Claude's JSON response
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

    # Add Claude's response to history
    conversation_history.append({"role": "assistant", "content": raw_text})

    # 5. Convert spoken message to TTS
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

    # 6. If spec is complete, generate and deploy app
    deploy_url = None
    if is_complete and spec:
        print("SPEC COMPLETE — generating and deploying app...")
        deploy_url = await generate_and_deploy(spec)

    return JSONResponse({
        "transcript": user_text,
        "message": spoken_message,
        "audio_url": f"/audio/{audio_filename}",
        "complete": is_complete,
        "spec": spec,
        "deploy_url": deploy_url,
    })


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    path = f"/tmp/{filename}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(path, media_type="audio/mpeg")


async def generate_and_deploy(spec: dict) -> str | None:
    # Generate the app HTML
    try:
        spec_json = json.dumps(spec, indent=2)
        prompt = BUILDER_USER_TEMPLATE.format(spec_json=spec_json)

        gen_response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=BUILDER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        html_content = gen_response.content[0].text.strip()

        if html_content.startswith("```"):
            html_content = html_content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        print(f"App HTML generated: {len(html_content)} chars")
    except Exception as e:
        print(f"ERROR generating app HTML: {e}")
        return None

    # Deploy to Vercel
    try:
        deploy_dir = tempfile.mkdtemp()
        html_path = os.path.join(deploy_dir, "index.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        project_name = f"ramble-{uuid.uuid4().hex[:8]}"
        print(f"Deploying to Vercel as project: {project_name}")

        result = subprocess.run(
            ["vercel", "--yes", "--name", project_name, "--prod", "--token", os.environ.get("VERCEL_TOKEN", "")],
            cwd=deploy_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        print(f"VERCEL STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"VERCEL STDERR:\n{result.stderr}")

        deploy_url = None
        for line in reversed(result.stdout.strip().split("\n")):
            line = line.strip()
            if line.startswith("https://"):
                deploy_url = line
                break

        if deploy_url:
            print(f"✓ DEPLOYED: {deploy_url}")
            return deploy_url
        else:
            print("WARNING: Could not extract URL from Vercel output")
            return None

    except subprocess.TimeoutExpired:
        print("ERROR: Vercel deploy timed out after 120s")
        return None
    except FileNotFoundError:
        print("ERROR: 'vercel' command not found — is Vercel CLI installed and in PATH?")
        return None
    except Exception as e:
        print(f"ERROR deploying to Vercel: {e}")
        return None