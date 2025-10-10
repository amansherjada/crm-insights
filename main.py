# main.py
import os, re, json, tempfile, logging, subprocess, requests
from typing import List, Dict, Optional, Tuple, Union
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
JSON_START = "<<<SCORES_11_JSON_START>>>"
JSON_END   = "<<<SCORES_11_JSON_END>>>"

# Score type can be int or "N/A"
ScoreValue = Union[int, str]

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
    if file_id_or_url.startswith('http://') or file_id_or_url.startswith('https://'):
        logging.info(f"📥 Downloading audio from URL: {file_id_or_url}")
        return download_from_url(file_id_or_url)
    else:
        logging.info(f"📥 Downloading audio from Google Drive: {file_id_or_url}")
        return download_mp3_from_drive(file_id_or_url)

def download_from_url(url: str) -> str:
    """
    Download audio file from any HTTP/HTTPS URL (S3, etc.)
    Returns path to the downloaded file.
    """
    try:
        response = requests.get(url, timeout=120, stream=True)
        
        if response.status_code != 200:
            raise RuntimeError(f"Failed to download from URL: {response.status_code}")
        
        file_ext = ".mp3"
        if url.endswith('.aac'):
            file_ext = ".aac"
        elif url.endswith('.wav'):
            file_ext = ".wav"
        elif url.endswith('.m4a'):
            file_ext = ".m4a"
        
        filename = url.split('/')[-1].split('?')[0]
        if not filename:
            filename = f"audio_download_{os.urandom(8).hex()}"
        
        if not any(filename.endswith(ext) for ext in ['.mp3', '.aac', '.wav', '.m4a']):
            filename += file_ext
        
        file_path = os.path.join(tempfile.gettempdir(), filename)
        
        logging.info(f"💾 Saving audio to: {file_path}")
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
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
                os.remove(file_path)
                file_path = mp3_path
                logging.info(f"✅ Converted to MP3: {mp3_path}")
            except subprocess.CalledProcessError as e:
                logging.error(f"❌ FFmpeg conversion failed: {e.stderr.decode('utf-8', errors='ignore')}")
        
        file_size = os.path.getsize(file_path) / (1024 * 1024)
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
    Generate comprehensive CRM audit report with SMART CONDITIONAL SCORING (11 Parameters).
    Uses GPT-4o with Hybrid Smart Approach.
    Returns human-readable report + machine-readable JSON block with N/A support.
    """
    logging.info("📝 Generating OpenAI CRM report with smart conditional scoring (GPT-4o)...")
    prompt = f"""
📞 [CRM Call Audit Evaluation – Smart Conditional Scoring System]

You are a senior customer experience auditor for American Hairline, reviewing how a CRM executive handled a first-time inquiry call. Your evaluation must be FAIR and CONTEXT-AWARE.

## CALL TRANSCRIPT:
{full_transcript}

---

## 🎯 EVALUATION INSTRUCTIONS

You will assess this call using the **HYBRID SMART APPROACH** with 11 parameters. Your job is to be INTELLIGENT and FAIR - not every parameter applies to every call.

### **CRITICAL SCORING LOGIC (READ CAREFULLY):**

For EACH of the 11 parameters below, follow this 4-STEP DECISION PROCESS:

#### **STEP 1: Was this topic discussed in the call?**
- **YES** → Score the quality (0-10) based on how well CRM handled it, then move to next parameter
- **NO** → Continue to STEP 2

#### **STEP 2: Is this topic RELEVANT to the customer's inquiry?**
Ask yourself: "Given what the customer was asking about, does this topic make sense for this call?"
- **NO (completely irrelevant)** → Mark **"N/A"** and move to next parameter
- **YES (relevant to inquiry)** → Continue to STEP 3

#### **STEP 3: Evaluate CONTEXT FACTORS**
Consider ALL of these factors:

**a) Call Duration:**
- < 2 minutes → Very short, most topics get leniency
- 2-5 minutes → Short, optional topics get leniency
- 5-10 minutes → Medium, core topics should be covered
- > 10 minutes → Long, CRM had time for comprehensive discussion

**b) Customer Engagement Level:**
- **High**: Asks multiple questions, responds with interest, wants details
- **Medium**: Asks 1-2 questions, polite but brief
- **Low**: Rushed, monosyllabic ("okay", "fine"), just gathering basic info

**c) Call Type:**
- General inquiry → Should cover multiple topics
- Specific question → Focus on that question only
- Price inquiry → Focus on pricing, budget justification
- Appointment booking → Just needs logistics, details come later
- Quick question → Brief answer sufficient

**d) Topic Priority Level:**
- **CRITICAL** (almost always needed in general calls): Product Explanation, Budget Justification (if price discussed)
- **IMPORTANT** (should cover if opportunity exists): Brand USPs, Understanding Needs, Greeting
- **CONTEXTUAL** (depends on inquiry): Hairline Types, Delivery Timeline, Servicing Details

#### **STEP 4: Make SMART FINAL JUDGMENT**

**If topic is CRITICAL + call was general inquiry + customer engaged + topic NOT mentioned:**
→ **Score 0-5** (CRM should have brought it up proactively)

**If topic is IMPORTANT but context factors justify not mentioning:**
→ **Mark "N/A"** (understandable omission given call context)

**If topic is CONTEXTUAL and wasn't relevant to this specific inquiry:**
→ **Mark "N/A"** (not applicable to this call)

**GOLDEN RULE:** Be FAIR to the CRM. Don't penalize for topics that genuinely didn't fit the natural flow of THIS specific call.

---

## 📊 11-PARAMETER EVALUATION

### **CUSTOMER PROFILING**
First, identify:
- **Customer Type**: Price-sensitive | Confused/Over-researching | Serious Buyer | Just Exploring | Referral/Follower | Skeptical/Fearful
- **Call Type**: General Inquiry | Specific Question | Price-Focused | Booking | Technical Query | Comparison
- **Engagement Level**: High | Medium | Low
- **Call Duration**: Approximate minutes

---

### **CORE COMMUNICATION SKILLS (44 points max)**

#### **1. Professional Greeting & Introduction (Score: __/10 or N/A)**
**Priority: IMPORTANT**

Evaluate IF greeting occurred:
- Was greeting warm, professional, and confident?
- Did CRM introduce themselves and brand clearly?
- Was tone appropriate for customer's mood?

**Score 0-10** if greeting happened, **N/A** only if call started mid-conversation.

---

#### **2. Active Listening & Empathy (Score: __/10 or N/A)**
**Priority: IMPORTANT**

- Did CRM listen without interrupting?
- Were empathetic responses given to concerns?
- Did they acknowledge customer's emotions?

**Score 0-10** for any conversation, **N/A** only if call was too brief to judge.

---

#### **3. Understanding Customer Needs (Score: __/8 or N/A)**
**Priority: IMPORTANT**

- Were qualifying questions asked?
- Did CRM identify what customer needs?
- Was there probing for hair loss type, lifestyle, budget?

**Score 0-8** if conversation allowed, **N/A** if customer only asked one specific thing and left.

---

#### **4. Call Closure & Next Step (Score: __/8 or N/A)**
**Priority: IMPORTANT**

- Was clear next step communicated?
- Did CRM create urgency or excitement?
- Was commitment secured?

**Score 0-8** for most calls, **N/A** only if customer abruptly ended call.

---

#### **5. Trust & Confidence Building (Score: __/8 or N/A)**
**Priority: IMPORTANT**

- Did CRM sound knowledgeable and confident?
- Were testimonials, celebrity clients, or social proof mentioned?
- Was reassurance provided?

**Score 0-8** if opportunity existed, **N/A** if call was too brief.

---

### **PRODUCT & SERVICE KNOWLEDGE (28 points max)**

#### **6. General Product Explanation (Score: __/10 or N/A)**
**Priority: CRITICAL** (almost always needed in general inquiries)

- Were American Hairline's offerings explained?
- Customization options mentioned?
- System types discussed?
- Natural look emphasized?

**Scoring:**
- If general inquiry + NOT explained → **Score 0-5** (critical miss)
- If specific question + product explained → **Score 6-10**
- If customer only asked location/timing → **N/A**

---

#### **7. Hairline Types Differentiation (Score: __/8 or N/A)**
**Priority: CONTEXTUAL** (only if customer mentioned "hairline")

**ONLY score this if customer specifically mentioned hairline/front hairline.**

If customer said "hairline":
- Did CRM differentiate "just hairline" (₹15K-18K) vs "hairline patch" (₹25K+)?
- Were both options explained with pricing?

**Scoring:**
- If hairline discussed + well explained → **Score 6-8**
- If hairline discussed + poorly explained → **Score 0-5**
- If hairline NOT mentioned by customer → **N/A**

---

#### **8. Brand Differentiation (USPs) (Score: __/10 or N/A)**
**Priority: IMPORTANT** (should mention in general inquiries)

USPs: Handmade systems, Premium Remy hair, Custom fit, Natural hairlines, Training support, Transparent consultation, Pan-India reach

**Scoring:**
- If customer asked "why you?" or general inquiry + USPs explained → **Score 7-10**
- If general inquiry + USPs NOT mentioned → **Score 3-6** (missed opportunity)
- If specific quick question → **N/A**

---

### **PRICING & SERVICE CLARITY (28 points max)**

#### **9. Budget Justification (₹25K+ Packages) (Score: __/10 or N/A)**
**Priority: CRITICAL** (if pricing discussed)

**RED FLAG**: If customer said "too expensive" or asked about pricing, CRM MUST justify value.

**Scoring:**
- If price discussed + excellent justification → **Score 8-10**
- If price discussed + weak justification → **Score 4-7**
- If price discussed + just said "come for consultation" → **Score 0-3** (critical failure)
- If pricing NOT discussed at all → **N/A**

---

#### **10. Delivery Timeline & Rush Charges (Score: __/8 or N/A)**
**Priority: CONTEXTUAL** (only if customer asked about timing)

Standard: 25-30 days. Rush: Ask "how soon?" + $40 charge.

**Scoring:**
- If customer asked about delivery + CRM explained well → **Score 6-8**
- If customer asked + CRM vague → **Score 0-5**
- If timing NOT discussed → **N/A**

---

#### **11. Stick-On Servicing Details (Score: __/10 or N/A)**
**Priority: CONTEXTUAL** (only if customer asked about maintenance)

Details: ₹2,500/session, packages available, first 2 sessions must be professional.

**Scoring:**
- If customer asked about servicing + CRM explained well → **Score 8-10**
- If customer asked + CRM incomplete info → **Score 4-7**
- If servicing NOT discussed → **N/A**

---

## 📝 REPORT STRUCTURE

Now write your comprehensive report:

### **1) CUSTOMER PROFILING**
- Customer Type:
- Call Type:
- Engagement Level:
- Approximate Duration:

### **2) DETAILED EVALUATION**
For EACH parameter:
- State whether it was discussed
- If scored: Explain score with evidence
- If N/A: Briefly explain why (e.g., "Not discussed - customer only asked about branch location")

### **3) OBJECTIONS & HOW HANDLED**
List any objections and quality of responses

### **4) MISSED OPPORTUNITIES**
What could have been done better (only mention realistic opportunities given the call context)

### **5) CALL OUTCOME**
- Status: Booked Consultation | Follow-up Scheduled | Undecided | Not Interested
- Next Action: No Action | Minor Feedback | Coaching Required | Retraining Needed

### **6) FINAL VERDICT**
2-3 sentence summary with key strengths and critical improvement area

---

## 📊 SCORECARD (FILL WITH REAL VALUES)

**CORE COMMUNICATION SKILLS:**
- Professional Greeting & Introduction Score: __/10 (or "N/A")
- Active Listening & Empathy Score: __/10 (or "N/A")
- Understanding Customer Needs Score: __/8 (or "N/A")
- Call Closure & Next Step Score: __/8 (or "N/A")
- Trust & Confidence Building Score: __/8 (or "N/A")

**PRODUCT & SERVICE KNOWLEDGE:**
- General Product Explanation Score: __/10 (or "N/A")
- Hairline Types Differentiation Score: __/8 (or "N/A")
- Brand Differentiation (USPs) Score: __/10 (or "N/A")

**PRICING & SERVICE CLARITY:**
- Budget Justification (₹25K+) Score: __/10 (or "N/A")
- Delivery Timeline & Rush Charges Score: __/8 (or "N/A")
- Stick-On Servicing Details Score: __/10 (or "N/A")

**TOTAL SCORE:** Will be calculated by system

---

## ⚙️ MACHINE-READABLE JSON OUTPUT

After completing the human-readable report, append this JSON between markers (no code fences, no extra text):

{JSON_START}
{{"greeting": <int or "N/A">, "listening": <int or "N/A">, "understanding_needs": <int or "N/A">, "call_closure": <int or "N/A">, "trust_building": <int or "N/A">, "product_explanation": <int or "N/A">, "hairline_types": <int or "N/A">, "brand_differentiation": <int or "N/A">, "budget_justification": <int or "N/A">, "delivery_timeline": <int or "N/A">, "servicing_details": <int or "N/A">}}
{JSON_END}

**CRITICAL:**
- Use actual integers (0-10 or 0-8) for scored parameters
- Use string "N/A" for not applicable parameters
- Example: {{"greeting": 8, "hairline_types": "N/A", "servicing_details": "N/A"}}
"""
    
    resp = client.chat.completions.create(
        model="gpt-4o",  # Using GPT-4o for better performance and 128K context
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3500,  # Increased for comprehensive reports
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def extract_json_and_strip(report_text: str) -> Tuple[Optional[Dict[str, ScoreValue]], str]:
    """
    Extract the JSON between markers; return (scores_dict, cleaned_report_without_json).
    Handles both integer scores and "N/A" values.
    If extraction fails, scores_dict=None and report_text is returned unchanged.
    """
    try:
        start = report_text.index(JSON_START) + len(JSON_START)
        end   = report_text.index(JSON_END, start)
        json_str = report_text[start:end].strip()
        data = json.loads(json_str)

        # Remove the entire JSON block with markers from the human report
        cleaned = report_text[:report_text.index(JSON_START)].rstrip()
        
        # Process each score - keep as int or "N/A"
        scores = {}
        for key in [
            "greeting", "listening", "understanding_needs", "call_closure", 
            "trust_building", "product_explanation", "hairline_types", 
            "brand_differentiation", "budget_justification", "delivery_timeline", 
            "servicing_details"
        ]:
            value = data.get(key, "N/A")
            if value == "N/A" or value == "n/a":
                scores[key] = "N/A"
            else:
                try:
                    scores[key] = int(value)
                except (ValueError, TypeError):
                    scores[key] = "N/A"
        
        return scores, cleaned
        
    except Exception as e:
        logging.warning(f"⚠️ JSON block extraction failed, will fallback to regex. {e}")
        return None, report_text

# Regex fallback for 11 parameters (supports N/A)
def parse_scores_from_report(report_text: str) -> Dict[str, ScoreValue]:
    """
    Fallback regex parser that handles both numeric scores and N/A values.
    """
    def grab(label_regex: str) -> ScoreValue:
        # Try to match "N/A" first
        na_match = re.search(
            label_regex + r".{0,200}?Score\s*[:\-]?\s*[\"']?N/?A[\"']?",
            report_text,
            re.IGNORECASE | re.DOTALL,
        )
        if na_match:
            return "N/A"
        
        # Try to match numeric score
        m = re.search(
            label_regex + r".{0,200}?Score\s*[:\-]?\s*(\d{1,2})\s*/\s*\d{1,2}",
            report_text,
            re.IGNORECASE | re.DOTALL,
        )
        return int(m.group(1)) if m else "N/A"

    scores = {
        "greeting":               grab(r"Professional\s+Greeting\s*&\s*Introduction"),
        "listening":              grab(r"Active\s+Listening\s*&\s*Empathy"),
        "understanding_needs":    grab(r"Understanding\s+Customer\s*Needs"),
        "call_closure":           grab(r"Call\s+Closure\s*&\s*Next\s*Step"),
        "trust_building":         grab(r"Trust\s*&\s*Confidence\s*Building"),
        "product_explanation":    grab(r"General\s+Product\s+Explanation"),
        "hairline_types":         grab(r"Hairline\s+Types\s+Differentiation"),
        "brand_differentiation":  grab(r"Brand\s+Differentiation\s*\(?USPs?\)?"),
        "budget_justification":   grab(r"Budget\s+Justification\s*\(₹25K\+\)"),
        "delivery_timeline":      grab(r"Delivery\s+Timeline\s*&\s*Rush\s+Charges"),
        "servicing_details":      grab(r"Stick-On\s+Servicing\s+Details"),
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

        # Generate report with smart conditional scoring
        full_transcript = clean_transcript(" ".join(parts).strip())
        raw_output = generate_openai_report(full_transcript)

        # 1) Try to extract JSON scores & strip from report (so PDF won't show JSON)
        scores, cleaned_report = extract_json_and_strip(raw_output)

        # 2) If JSON failed, fallback to regex; keep full text as report
        if scores is None:
            scores = parse_scores_from_report(raw_output)
            cleaned_report = raw_output

        logging.info(f"✅ Report generated with smart conditional scoring: {scores}")
        return {"report": cleaned_report, "scores": scores}

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})
