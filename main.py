# main.py
import os, re, json, tempfile, logging, subprocess, requests
from typing import List, Dict, Optional, Tuple
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest
from openai import OpenAI

logging.basicConfig(level=logging.INFO)

# ========== ENV ==========
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GCRED_PATH     = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if not OPENAI_API_KEY:
    raise RuntimeError("❌ OPENAI_API_KEY not set.")
if not GCRED_PATH or not os.path.exists(GCRED_PATH):
    raise RuntimeError("❌ GOOGLE_APPLICATION_CREDENTIALS path is invalid.")

client = OpenAI(api_key=OPENAI_API_KEY)

# ========== APP ==========
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== CONSTANTS ==========
JSON_START = "<<<SCORES_9_JSON_START>>>"
JSON_END   = "<<<SCORES_9_JSON_END>>>"

# ========== HELPERS ==========
def clean_transcript(text: str) -> str:
    text = re.sub(r"\\an\d+\\?.*?", "", text)
    text = re.sub(r"[-–—_=*#{}<>[\]\"'`|]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"\d{2,}[:.]\d{2,}[:.]\d{2,}", "", text)
    return text.strip()

def download_audio_file(file_id_or_url: str) -> str:
    """
    Download audio from either Google Drive (file ID) or S3/HTTP URL.
    Returns path to the downloaded MP3 file.
    """
    # Check if it's an HTTP/HTTPS URL (S3 or other web URL)
    if file_id_or_url.startswith('http://') or file_id_or_url.startswith('https://'):
        logging.info(f"📥 Downloading audio from URL: {file_id_or_url}")
        return download_from_url(file_id_or_url)
    else:
        # It's a Google Drive file ID
        logging.info(f"📥 Downloading audio from Google Drive: {file_id_or_url}")
        return download_mp3_from_drive(file_id_or_url)

def download_from_url(url: str) -> str:
    """
    Download audio file from any HTTP/HTTPS URL (S3, etc.)
    Returns path to the downloaded file.
    """
    try:
        # Make request to download file
        response = requests.get(url, timeout=120, stream=True)
        
        if response.status_code != 200:
            raise RuntimeError(f"Failed to download from URL: {response.status_code}")
        
        # Determine file extension from URL or Content-Type
        file_ext = ".mp3"  # Default
        if url.endswith('.aac'):
            file_ext = ".aac"
        elif url.endswith('.wav'):
            file_ext = ".wav"
        elif url.endswith('.m4a'):
            file_ext = ".m4a"
        
        # Generate filename from URL
        filename = url.split('/')[-1].split('?')[0]  # Get last part of URL, remove query params
        if not filename:
            filename = f"audio_download_{os.urandom(8).hex()}"
        
        # Ensure extension
        if not any(filename.endswith(ext) for ext in ['.mp3', '.aac', '.wav', '.m4a']):
            filename += file_ext
        
        # Save to temp directory
        file_path = os.path.join(tempfile.gettempdir(), filename)
        
        logging.info(f"💾 Saving audio to: {file_path}")
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Convert to MP3 if not already MP3
        if not file_path.endswith('.mp3'):
            logging.info(f"🔄 Converting {file_ext} to MP3...")
            mp3_path = file_path.rsplit('.', 1)[0] + '.mp3'
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", file_path,
                        "-ar", "16000", "-ac", "1", "-vn",
                        "-codec:a", "libmp3lame",
                        mp3_path
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                # Remove original file
                os.remove(file_path)
                file_path = mp3_path
                logging.info(f"✅ Converted to MP3: {mp3_path}")
            except subprocess.CalledProcessError as e:
                logging.error(f"❌ FFmpeg conversion failed: {e.stderr.decode('utf-8', errors='ignore')}")
                # Continue with original file if conversion fails
        
        file_size = os.path.getsize(file_path) / (1024 * 1024)  # MB
        logging.info(f"✅ Downloaded successfully: {file_path} ({file_size:.2f} MB)")
        
        return file_path
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Failed to download from URL: {e}")
        raise RuntimeError(f"Failed to download audio from URL: {str(e)}")
    except Exception as e:
        logging.error(f"❌ Unexpected error downloading from URL: {e}")
        raise RuntimeError(f"Error downloading audio: {str(e)}")

def download_mp3_from_drive(file_id: str) -> str:
    """
    Download MP3 from Google Drive using file ID.
    Returns path to the downloaded MP3 file.
    """
    logging.info(f"📥 Downloading MP3 from Google Drive: {file_id}")
    creds = service_account.Credentials.from_service_account_file(
        GCRED_PATH, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    creds.refresh(GoogleAuthRequest())
    drive_service = build("drive", "v3", credentials=creds)
    
    try:
        meta = drive_service.files().get(fileId=file_id, fields="name").execute()
        base = os.path.splitext(meta["name"])[0]
    except Exception as e:
        logging.error(f"❌ Failed to get file metadata from Drive: {e}")
        raise RuntimeError(f"Failed to access Drive file {file_id}: {str(e)}")

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {creds.token}"}
    r = requests.get(url, headers=headers, timeout=120)
    
    if r.status_code != 200:
        raise RuntimeError(f"Failed to download MP3 from Drive: {r.status_code} - {r.text}")

    mp3_path = os.path.join(tempfile.gettempdir(), base + ".mp3")
    with open(mp3_path, "wb") as f:
        f.write(r.content)
    
    logging.info(f"✅ Downloaded from Drive: {mp3_path}")
    return mp3_path

def split_audio(mp3_path: str, chunk_seconds: int = 600) -> List[str]:
    logging.info("🔪 Splitting audio into chunks...")
    outdir = tempfile.mkdtemp()
    pattern = os.path.join(outdir, "chunk_%03d.mp3")
    try:
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
            model="whisper-1", file=fh, response_format="text", language="en"
        )
    return tr.strip()

# ========== REPORT GEN ==========
def generate_openai_report(full_transcript: str) -> str:
    """
    Human-readable report WITH the Legacy 9-Parameter Scorecard (numbers),
    then a machine-readable JSON block between markers for reliable parsing.
    """
    logging.info("📝 Generating OpenAI CRM report...")
    prompt = f"""
📞 [CRM Call Audit Evaluation – First-Time Inquiry Call]

You are a senior customer experience auditor reviewing how a CRM executive handled a first-time inquiry call. Analyze this transcript:

{full_transcript}

Your job is to assess:

1. What kind of customer this was (e.g., Price-sensitive, Confused, Serious buyer, Skeptical, Just Exploring)
2. Whether the CRM delivered a confident, informative pitch.
3. If all the customer's questions and objections were handled properly.
4. Whether the lead was moved forward effectively.

--- 

1) **Customer Type & Intent:**
   - Identify the type and cite clues.

2) **Call Opening & Tone Matching:**
   - Was greeting professional/warm?
   - Was tone confident and aligned with customer?
   - Did the CRM actively listen without interrupting?
   - Score: __/10

3) **CRM Pitch & Communication Quality:**
   - Qualifying questions asked?
   - Clear brand/service intro?
   - Key USPs conveyed (customization, natural look, celebs, etc.)?
   - Guided to clear next step?
   - Score: __/10

4) **Questions & Objection Handling:**
   - Which questions/objections?
   - Were they handled confidently?
   - Score: __/10

5) **Missed Opportunities or Gaps:**
   - What was under-explained or missed?

6) **Call Outcome:**
   - Consultation booked? If not, what next step?
   - Follow-up planned?
   - ✔ Call Status: Booked / Follow-up / Undecided / Not Interested

7) **Customer Tag (Pick One):**
   Price-sensitive | Confused/Over-researching | Serious Buyer | Just Exploring | Referral/Follower | Skeptical/Fearful

8) **Action Required (Pick One):**
   No Action | Minor Feedback | Coaching Required | Retraining Needed | Escalate

---

✅ **Final Verdict & Recommendation:** Concise next steps for CRM and lead.

---

📊 **Legacy 9-Parameter Scorecard (fill REAL numbers, keep EXACT labels):**
- Professional Greeting & Introduction Score: __/15
- Active Listening & Empathy Score: __/15
- Understanding Customer's Needs Score: __/10
- Product/Service Explanation Score: __/10
- Personalization & Lifestyle Suitability Score: __/10
- Handling Objections & Answering Queries Score: __/10
- Pricing & Value Communication Score: __/10
- Trust & Confidence Building Score: __/10
- Call Closure & Next Step Commitment Score: __/10

RULES:
- Replace every "__" with integers.
- Keep labels and "Score:" pattern so an automated parser can read it.

---
IMPORTANT: After finishing the human-readable report above,
append on a NEW line ONLY this machine-readable JSON (no extra words, no code fences)
between these exact markers:

{JSON_START}
{{ "greeting": <int>, "listening": <int>, "understanding_needs": <int>, "product_explanation": <int>, "personalization": <int>, "objection_handling": <int>, "pricing_communication": <int>, "trust_building": <int>, "call_closure": <int> }}
{JSON_END}
"""
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2400,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def extract_json_and_strip(report_text: str) -> Tuple[Optional[Dict[str,int]], str]:
    """
    Extract the JSON between markers; return (scores_dict, cleaned_report_without_json).
    If extraction fails, scores_dict=None and report_text is returned unchanged.
    """
    try:
        start = report_text.index(JSON_START) + len(JSON_START)
        end   = report_text.index(JSON_END, start)
        json_str = report_text[start:end].strip()
        data = json.loads(json_str)

        # Remove the entire JSON block with markers from the human report
        cleaned = report_text[:report_text.index(JSON_START)].rstrip()
        return (
            {
                "greeting": int(data.get("greeting", 0)),
                "listening": int(data.get("listening", 0)),
                "understanding_needs": int(data.get("understanding_needs", 0)),
                "product_explanation": int(data.get("product_explanation", 0)),
                "personalization": int(data.get("personalization", 0)),
                "objection_handling": int(data.get("objection_handling", 0)),
                "pricing_communication": int(data.get("pricing_communication", 0)),
                "trust_building": int(data.get("trust_building", 0)),
                "call_closure": int(data.get("call_closure", 0)),
            },
            cleaned,
        )
    except Exception as e:
        logging.warning(f"⚠️ JSON block extraction failed, will fallback to regex. {e}")
        return None, report_text

# Tolerant regex fallback for the 9 legacy params
def parse_scores_from_report(report_text: str) -> Dict[str, int]:
    def grab(label_regex: str) -> int:
        m = re.search(
            label_regex + r".{0,200}?Score\s*[:\-]?\s*(\d{1,2})\s*/\s*\d{1,2}",
            report_text,
            re.IGNORECASE | re.DOTALL,
        )
        return int(m.group(1)) if m else 0

    scores = {
        "greeting":               grab(r"Professional\s+Greeting\s*&\s*Introduction"),
        "listening":              grab(r"Active\s+Listening\s*&\s*Empathy"),
        "understanding_needs":    grab(r"Understanding\s+Customer['']?s\s+Needs"),
        "product_explanation":    grab(r"Product/?Service\s+Explanation"),
        "personalization":        grab(r"Personalization\s*&\s*Lifestyle(?:\s*Suitability)?"),
        "objection_handling":     grab(r"Handling\s+Objections\s*&\s*Answering\s*Queries"),
        "pricing_communication":  grab(r"Pricing\s*&\s*Value\s*Communication"),
        "trust_building":         grab(r"Trust\s*&\s*Confidence\s*Building"),
        "call_closure":           grab(r"Call\s*Closure\s*&\s*Next\s*Step\s*Commitment"),
    }
    logging.info(f"📊 Parsed Scores (regex fallback): {scores}")
    return scores

# ========== ROUTE ==========
@app.post("/generate-report")
async def generate_report_endpoint(request: Request):
    try:
        data = await request.json()
        file_id_or_url = data.get("file_id")
        
        if not file_id_or_url:
            return JSONResponse(status_code=400, content={"error": "Missing file_id"})
        
        logging.info(f"🎯 Processing request for: {file_id_or_url}")

        # Download audio (handles both Drive and S3/URL)
        mp3_path = download_audio_file(file_id_or_url)
        
        # Split audio into chunks
        chunks = split_audio(mp3_path)

        # Transcribe all chunks
        parts: List[str] = []
        try:
            for p in chunks:
                parts.append(transcribe_audio(p))
        finally:
            # cleanup chunks
            for p in chunks:
                try: os.remove(p)
                except: pass
            # cleanup main mp3
            try: os.remove(mp3_path)
            except: pass

        # Generate report
        full_transcript = clean_transcript(" ".join(parts).strip())
        raw_output = generate_openai_report(full_transcript)

        # 1) Try to extract JSON scores & strip from report (so PDF won't show JSON)
        scores, cleaned_report = extract_json_and_strip(raw_output)

        # 2) If JSON failed, fallback to regex; keep full text as report
        if scores is None:
            scores = parse_scores_from_report(raw_output)
            cleaned_report = raw_output

        logging.info("✅ Report generated successfully")
        return {"report": cleaned_report, "scores": scores}

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})
