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

# NEW, MORE FLEXIBLE VERSION
def parse_scores_from_report(report_text):
    """
    Parses scores from the generated report text using more flexible regular expressions.
    """
    scores = {}
    
    def extract_score(pattern, text):
        # We use the re.MULTILINE flag to help search line by line
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return int(match.group(1)) if match else 0

    # The new regex patterns now look for the keywords and allow for other text (like bullet points or parentheticals) around them.
    scores['greeting'] = extract_score(r"Professional Greeting.*?Score:\s*(\d{1,2})", report_text)
    scores['listening'] = extract_score(r"Active Listening.*?Score:\s*(\d{1,2})", report_text)
    scores['understanding_needs'] = extract_score(r"Understanding Customer’s Needs.*?Score:\s*(\d{1,2})", report_text)
    scores['product_explanation'] = extract_score(r"Product/Service Explanation.*?Score:\s*(\d{1,2})", report_text)
    scores['personalization'] = extract_score(r"Personalization & Lifestyle.*?Score:\s*(\d{1,2})", report_text)
    scores['objection_handling'] = extract_score(r"Handling Objections & Answering.*?Score:\s*(\d{1,2})", report_text)
    scores['pricing_communication'] = extract_score(r"Pricing & Value Communication.*?Score:\s*(\d{1,2})", report_text)
    scores['trust_building'] = extract_score(r"Trust & Confidence Building.*?Score:\s*(\d{1,2})", report_text)
    scores['call_closure'] = extract_score(r"Call Closure & Next Step.*?Score:\s*(\d{1,2})", report_text)
    
    logging.info(f"📊 Parsed Scores (Flexible): {scores}")
    return scores

# REPLACED FUNCTION WITH NEW PROMPT
def generate_openai_report(transcript):
    """
    Generate a report using OpenAI GPT with a structured scoring section.
    """
    logging.info("📝 Generating OpenAI CRM report...")

    prompt = f"""
    You are a senior customer experience auditor. Analyze the following call transcript and provide a detailed evaluation based ONLY on the provided text.

    **Transcript:**
    ---
    {transcript}
    ---

    **Instructions:**
    Evaluate the call based on the 9 parameters below. For each parameter, provide a brief analysis and a score. The output for each parameter MUST be in the format "Parameter Name Score: [Score]/[Max Score]".

    ---
    **[CALL ANALYSIS REPORT]**

    **1. Overall Summary & Customer Intent:**
    Briefly summarize the call's purpose and outcome. Identify the customer's primary intent (e.g., Price-sensitive, Serious buyer, Confused, Just Exploring).

    **2. Detailed Parameter Evaluation:**

    * **Professional Greeting & Introduction:** (Did the agent sound professional, state their name and the company's name clearly, and set a positive tone?)
        * **Analysis:** [Your brief analysis here]
        * **Professional Greeting & Introduction Score:** __/15

    * **Active Listening & Empathy:** (Did the agent listen without interrupting, acknowledge the customer's points, and show empathy towards their concerns?)
        * **Analysis:** [Your brief analysis here]
        * **Active Listening & Empathy Score:** __/15

    * **Understanding Customer’s Needs (Problem Diagnosis):** (Did the agent ask effective questions to understand the customer's specific problem, history, and desired outcome?)
        * **Analysis:** [Your brief analysis here]
        * **Understanding Customer’s Needs Score:** __/10

    * **Product/Service Explanation (Hair Systems & Solutions):** (How clearly and confidently did the agent explain the solutions, their benefits, and the process?)
        * **Analysis:** [Your brief analysis here]
        * **Product/Service Explanation Score:** __/10

    * **Personalization & Lifestyle Suitability:** (Did the agent connect the solution to the customer's personal lifestyle, job, or activities mentioned?)
        * **Analysis:** [Your brief analysis here]
        * **Personalization & Lifestyle Suitability Score:** __/10

    * **Handling Objections & Answering Queries:** (How effectively were the customer's objections (e.g., price, maintenance, fear) and questions addressed?)
        * **Analysis:** [Your brief analysis here]
        * **Handling Objections & Answering Queries Score:** __/10

    * **Pricing & Value Communication:** (Was pricing explained clearly? Did the agent effectively communicate the value to justify the cost?)
        * **Analysis:** [Your brief analysis here]
        * **Pricing & Value Communication Score:** __/10

    * **Trust & Confidence Building:** (Did the agent build credibility through testimonials, explaining expertise, or maintaining a confident and reassuring tone?)
        * **Analysis:** [Your brief analysis here]
        * **Trust & Confidence Building Score:** __/10

    * **Call Closure & Next Step Commitment:** (Did the agent summarize the call, clearly define the next step (e.g., booking a consultation), and gain commitment from the customer?)
        * **Analysis:** [Your brief analysis here]
        * **Call Closure & Next Step Commitment Score:** __/10

    **3. Final Verdict & Recommendation:**
    Provide a final assessment of the call quality and recommend the next action for the agent (e.g., No Action, Minor Feedback, Coaching Required).
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            temperature=0.5
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

        mp3_path = download_mp3_from_drive(file_id)
        
        chunks = split_audio(mp3_path)
        full_transcript = ""
        for chunk_path in chunks:
            full_transcript += transcribe_audio(chunk_path) + " "
            os.remove(chunk_path)
        os.remove(mp3_path)

        report_text = generate_openai_report(full_transcript.strip())
        
        # --- ADD THIS LINE ---
        logging.info(f"--- RAW REPORT TEXT ---\n{report_text}\n--- END RAW REPORT TEXT ---")
        # ---------------------

        scores = parse_scores_from_report(report_text)

        return {"report": report_text, "scores": scores}

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})
