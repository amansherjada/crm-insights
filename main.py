from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAuthRequest
import os
import re
import requests
import tempfile
import logging
import subprocess

# === Logging ===
logging.basicConfig(level=logging.INFO)

# === Env Vars Check ===
openai_api_key = os.getenv("OPENAI_API_KEY")
gcred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

if not openai_api_key:
    raise RuntimeError("❌ OPENAI_API_KEY not set.")
if not gcred_path or not os.path.exists(gcred_path):
    raise RuntimeError("❌ GOOGLE_APPLICATION_CREDENTIALS path is invalid.")

# Initialize OpenAI Client
client = OpenAI(api_key=openai_api_key)

# === FastAPI Setup ===
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, restrict this!
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Helpers ===

def clean_transcript(text):
    """Remove unwanted characters from the transcript."""
    text = re.sub(r"\\an\d+\\?.*?", "", text)
    text = re.sub(r"[-–—_=*#{}<>[\]\"\'`|]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"\d{2,}[:.]\d{2,}[:.]\d{2,}", "", text)
    return text.strip()

def download_mp3_from_drive(file_id):
    """Download MP3 file from Google Drive."""
    logging.info(f"📥 Downloading MP3 from Google Drive: {file_id}")
    credentials = service_account.Credentials.from_service_account_file(
        gcred_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    credentials.refresh(GoogleAuthRequest())
    drive_service = build("drive", "v3", credentials=credentials)

    file_metadata = drive_service.files().get(fileId=file_id, fields="name").execute()
    file_name = os.path.splitext(file_metadata['name'])[0]
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {credentials.token}"}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        raise Exception(f"Failed to download MP3: {response.status_code} - {response.text}")

    mp3_path = os.path.join(tempfile.gettempdir(), file_name + ".mp3")
    with open(mp3_path, "wb") as f:
        f.write(response.content)

    return mp3_path

def split_audio(mp3_path, chunk_duration=600):
    """Split audio into chunks."""
    logging.info("🔪 Splitting audio into chunks...")
    output_dir = tempfile.mkdtemp()
    output_pattern = os.path.join(output_dir, "chunk_%03d.mp3")

    try:
        subprocess.run([
            "ffmpeg", "-i", mp3_path,
            "-f", "segment",
            "-segment_time", str(chunk_duration),
            "-ar", "16000",
            "-ac", "1",
            "-vn",
            "-codec:a", "libmp3lame",
            output_pattern
        ], check=True)
    except subprocess.CalledProcessError as e:
        logging.error("❌ FFmpeg splitting failed")
        raise RuntimeError(str(e))

    return sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.endswith(".mp3")
    ])

def transcribe_audio(mp3_path):
    """Transcribe audio using the OpenAI Whisper API."""
    logging.info(f"🎧 Transcribing audio: {mp3_path}")
    try:
        with open(mp3_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
                language="en"
            )
        return transcript.strip()
    except Exception as e:
        logging.error(f"❌ Error during transcription: {str(e)}")
        raise

def generate_openai_report(transcript):
    """Generate a report using OpenAI GPT."""
    logging.info("📝 Generating OpenAI CRM report...")

    prompt = f"""
    📞 [CRM Call Audit Evaluation Prompt – First-Time Inquiry Call]

    You are a senior customer experience auditor reviewing how a CRM executive handled a first-time inquiry call. Analyze this transcript:

    {transcript}

    Your job is to assess:

    1. What kind of customer this was (e.g., Price-sensitive, Confused, Serious buyer, Skeptical, Just Exploring)
    2. Whether the CRM delivered a confident, informative pitch.
    3. If all the customer's questions and objections were handled properly.
    4. Whether the lead was moved forward effectively.

    --- 

    1. **Customer Type & Intent:**
       - What kind of customer was this? What clues (words, tone, objections) helped you identify this?

    2. **Call Opening & Tone Matching:**
       - Did the CRM greet the customer professionally and with warmth?
       - Was the CRM's tone confident, friendly, and aligned with the customer’s energy?
       - Did the CRM actively listen and allow the client to speak without interruption? 
       - Score: __/10

    3. **CRM Pitch & Communication Quality:**
       - Did the CRM ask the right qualifying questions?
       - Was the brand/service introduced clearly?
       - Were key USPs conveyed? (customization, natural look, celebrity clientele, etc.)
       - Did the CRM guide the customer toward a consultation or next step?
       - Score: __/10

    4. **Customer Questions & Objection Handling:**
       - What were the main questions or concerns raised by the customer?
       - Did the CRM address all queries properly?
       - Were objections (price, maintenance, surgery fear, etc.) handled confidently?
       - Score: __/10

    5. **Missed Opportunities or Gaps:**
       - What information was left out or under-explained?
       - Did the CRM miss any chance to build trust, share a testimonial, or clarify a next step?

    6. **Call Outcome:**
       - Was a consultation booked? If not, was the next step explained clearly (follow-up, visit, etc.)? 
       - Was a follow-up planned?
       - ✔ Call Status: Booked / Follow-up / Undecided / Not Interested

    7. **Customer Tag (Pick One):**
       🔘 Price-sensitive
       🔘 Confused / Over-researching
       🔘 Serious Buyer
       🔘 Just Exploring
       🔘 Referral / Follower
       🔘 Skeptical / Fearful

    8. **Action Required (Pick One):**
       🔘 No Action – Call handled well
       🔘 Minor Feedback – Needs polishing
       🔘 Coaching Required – Moderate gaps in handling
       🔘 Retraining Needed – Major pitch or process issues
       🔘 Escalate – Serious concern or customer mishandling

    --- 

    ✅ **Final Verdict & Recommendation:**
       - Was the call handled effectively? What should be the immediate next step (for the CRM or the lead)?

    --- 

    🧮 **Scorecard (Out of 10)**:
       - Customer Identification Accuracy: __/10
       - Tone & Opening: __/10
       - CRM Pitch & Info Delivery: __/10
       - Handling of Questions & Objections: __/10
       - Overall Lead Handling Quality: __/10
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ Error during OpenAI report generation: {str(e)}")
        raise

# === Endpoints ===

@app.post("/generate-report")
async def generate_report_endpoint(request: Request):
    try:
        data = await request.json()
        file_id = data.get("file_id")
        if not file_id:
            return JSONResponse(status_code=400, content={"error": "Missing file_id"})

        # Download MP3 file and transcribe it
        mp3_path = download_mp3_from_drive(file_id)
        transcript = transcribe_audio(mp3_path)
        os.remove(mp3_path)

        # Generate report using OpenAI GPT
        report = generate_openai_report(transcript)

        return {"report": report}

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})
