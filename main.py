# main.py
import os, re, json, tempfile, logging, subprocess
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest
from openai import OpenAI
import requests

logging.basicConfig(level=logging.INFO)

# --- ENV ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GCRED_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ OPENAI_API_KEY not set.")
if not GCRED_PATH or not os.path.exists(GCRED_PATH):
    raise RuntimeError("❌ GOOGLE_APPLICATION_CREDENTIALS path is invalid.")

client = OpenAI(api_key=OPENAI_API_KEY)

# --- APP ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HELPERS ---
def clean_transcript(text: str) -> str:
    text = re.sub(r"\\an\d+\\?.*?", "", text)
    text = re.sub(r"[-–—_=*#{}<>[\]\"'`|]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"\d{2,}[:.]\d{2,}[:.]\d{2,}", "", text)
    return text.strip()

def download_mp3_from_drive(file_id: str) -> str:
    logging.info(f"📥 Downloading MP3 from Google Drive: {file_id}")
    creds = service_account.Credentials.from_service_account_file(
        GCRED_PATH, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    creds.refresh(GoogleAuthRequest())
    drive_service = build("drive", "v3", credentials=creds)
    file_metadata = drive_service.files().get(fileId=file_id, fields="name").execute()
    base = os.path.splitext(file_metadata["name"])[0]
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {creds.token}"}
    r = requests.get(url, headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to download MP3: {r.status_code} - {r.text}")
    mp3_path = os.path.join(tempfile.gettempdir(), base + ".mp3")
    with open(mp3_path, "wb") as f:
        f.write(r.content)
    return mp3_path

def split_audio(mp3_path: str, chunk_seconds: int = 600) -> list[str]:
    logging.info("🔪 Splitting audio into chunks...")
    outdir = tempfile.mkdtemp()
    pattern = os.path.join(outdir, "chunk_%03d.mp3")
    try:
        # Convert to 16k mono mp3 segments
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_path, "-f", "segment",
                "-segment_time", str(chunk_seconds),
                "-ar", "16000", "-ac", "1", "-vn",
                "-codec:a", "libmp3lame",
                pattern,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        logging.error("❌ FFmpeg splitting failed: %s", e.stderr.decode("utf-8", errors="ignore"))
        raise RuntimeError("FFmpeg splitting failed")
    chunks = sorted(os.path.join(outdir, f) for f in os.listdir(outdir) if f.endswith(".mp3"))
    if not chunks:
        raise RuntimeError("No chunks produced by ffmpeg.")
    return chunks

def transcribe_audio(mp3_path: str) -> str:
    logging.info(f"🎧 Transcribing audio: {mp3_path}")
    with open(mp3_path, "rb") as fh:
        tr = client.audio.transcriptions.create(
            model="whisper-1",
            file=fh,
            response_format="text",
            language="en",
        )
    return tr.strip()

def generate_openai_report(transcript: str) -> str:
    logging.info("📝 Generating OpenAI CRM report...")
    prompt = f"""
You are a senior customer experience auditor. Analyze the call transcript and provide a detailed evaluation.

TRANSCRIPT
---
{transcript}
---

OUTPUT FORMAT
1) First write the full human-readable report exactly as specified below (each parameter must include a line '... Score: X/Max').
2) Then, on a NEW line, output ONLY this JSON between markers for machine parsing:
<<<SCORES_JSON_START>>>
{{ "greeting": <int>, "listening": <int>, "understanding_needs": <int>, "product_explanation": <int>, "personalization": <int>, "objection_handling": <int>, "pricing_communication": <int>, "trust_building": <int>, "call_closure": <int> }}
<<<SCORES_JSON_END>>>

[CALL ANALYSIS REPORT]

1. Overall Summary & Customer Intent

2. Detailed Parameter Evaluation:
* Professional Greeting & Introduction
  - Analysis: …
  - Professional Greeting & Introduction Score: __/15

* Active Listening & Empathy
  - Analysis: …
  - Active Listening & Empathy Score: __/15

* Understanding Customer’s Needs (Problem Diagnosis)
  - Analysis: …
  - Understanding Customer’s Needs Score: __/10

* Product/Service Explanation (Hair Systems & Solutions)
  - Analysis: …
  - Product/Service Explanation Score: __/10

* Personalization & Lifestyle Suitability
  - Analysis: …
  - Personalization & Lifestyle Suitability Score: __/10

* Handling Objections & Answering Queries
  - Analysis: …
  - Handling Objections & Answering Queries Score: __/10

* Pricing & Value Communication
  - Analysis: …
  - Pricing & Value Communication Score: __/10

* Trust & Confidence Building
  - Analysis: …
  - Trust & Confidence Building Score: __/10

* Call Closure & Next Step Commitment
  - Analysis: …
  - Call Closure & Next Step Commitment Score: __/10

3. Final Verdict & Recommendation
"""
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2200,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def extract_scores_from_json_block(report_text: str) -> dict | None:
    try:
        start_tag, end_tag = "<<<SCORES_JSON_START>>>", "<<<SCORES_JSON_END>>>"
        start = report_text.index(start_tag) + len(start_tag)
        end = report_text.index(end_tag, start)
        data = json.loads(report_text[start:end].strip())
        keys = [
            "greeting","listening","understanding_needs","product_explanation",
            "personalization","objection_handling","pricing_communication",
            "trust_building","call_closure",
        ]
        return {k: int(data.get(k, 0)) for k in keys}
    except Exception as e:
        logging.warning(f"⚠️ JSON score parse failed; falling back to regex. {e}")
        return None

# tolerant regex fallback
def parse_scores_from_report(report_text: str) -> dict:
    def grab(label_regex: str, text: str) -> int:
        m = re.search(
            label_regex + r".{0,200}?Score\s*[:\-]?\s*(\d{1,2})\s*/\s*\d{1,2}",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        return int(m.group(1)) if m else 0

    scores = {
        "greeting": grab(r"Professional\s+Greeting\s*&\s*Introduction", report_text),
        "listening": grab(r"Active\s+Listening\s*&\s*Empathy", report_text),
        "understanding_needs": grab(r"Understanding\s+Customer[’']?s\s+Needs", report_text),
        "product_explanation": grab(r"Product/?Service\s+Explanation", report_text),
        "personalization": grab(r"Personalization\s*&\s*Lifestyle(?:\s*Suitability)?", report_text),
        "objection_handling": grab(r"Handling\s+Objections\s*&\s*Answering\s+Queries", report_text),
        "pricing_communication": grab(r"Pricing\s*&\s*Value\s+Communication", report_text),
        "trust_building": grab(r"Trust\s*&\s*Confidence\s+Building", report_text),
        "call_closure": grab(r"Call\s+Closure\s*&\s*Next\s+Step\s+Commitment", report_text),
    }
    logging.info(f"📊 Parsed Scores (regex fallback): {scores}")
    return scores

# --- ROUTE ---
@app.post("/generate-report")
async def generate_report_endpoint(request: Request):
    try:
        data = await request.json()
        file_id = data.get("file_id")
        if not file_id:
            return JSONResponse(status_code=400, content={"error": "Missing file_id"})

        mp3_path = download_mp3_from_drive(file_id)
        chunks = split_audio(mp3_path)

        # build transcript
        parts: list[str] = []
        try:
            for p in chunks:
                parts.append(transcribe_audio(p))
        finally:
            # cleanup chunk files
            for p in chunks:
                try: os.remove(p)
                except: pass
            try: os.remove(mp3_path)
            except: pass

        full_transcript = clean_transcript(" ".join(parts).strip())
        report_text = generate_openai_report(full_transcript)

        # 1) JSON scores preferred
        scores = extract_scores_from_json_block(report_text)
        # 2) fallback to regex if needed
        if scores is None:
            scores = parse_scores_from_report(report_text)

        return {"report": report_text, "scores": scores}

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})
