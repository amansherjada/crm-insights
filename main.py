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
JSON_START = "<<<SCORES_11_JSON_START>>>"
JSON_END   = "<<<SCORES_11_JSON_END>>>"

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
    Generate comprehensive CRM audit report with NEW 11-Parameter Scorecard.
    Returns human-readable report + machine-readable JSON block.
    """
    logging.info("📝 Generating OpenAI CRM report with 11 parameters...")
    prompt = f"""
📞 [CRM Call Audit Evaluation – First-Time Inquiry Call]

You are a senior customer experience auditor for American Hairline, reviewing how a CRM executive handled a first-time inquiry call. Analyze this transcript:

{full_transcript}

Your job is to comprehensively assess the call across 11 critical parameters and provide actionable feedback.

---

## EVALUATION FRAMEWORK

### 1) CUSTOMER PROFILING
**Customer Type & Intent:**
- Identify the customer type: Price-sensitive | Confused/Over-researching | Serious Buyer | Just Exploring | Referral/Follower | Skeptical/Fearful
- Cite specific clues from the conversation that reveal their intent and mindset

---

### 2) CORE COMMUNICATION SKILLS (44 points)

#### A. Professional Greeting & Introduction (Score: __/10)
- Was the greeting warm, professional, and confident?
- Did the CRM introduce themselves and the brand clearly?
- Was the tone set appropriately for the customer's mood?

#### B. Active Listening & Empathy (Score: __/10)
- Did the CRM listen without interrupting?
- Were empathetic responses given to customer concerns?
- Did they acknowledge the customer's emotions and situation?

#### C. Understanding Customer Needs (Score: __/8)
- Were effective qualifying questions asked?
- Did the CRM accurately identify what the customer needs?
- Was there probing for hair loss type, lifestyle, budget range?

#### D. Call Closure & Next Step (Score: __/8)
- Was a clear next step communicated (consultation booking, follow-up, etc.)?
- Did the CRM create urgency or excitement about next steps?
- Was commitment secured or follow-up plan established?

#### E. Trust & Confidence Building (Score: __/8)
- Did the CRM sound knowledgeable and confident?
- Were testimonials, celebrity clients, or social proof mentioned?
- Was reassurance provided about quality and results?

---

### 3) PRODUCT & SERVICE KNOWLEDGE (28 points)

#### F. General Product Explanation (Score: __/10)
- Were American Hairline's key offerings explained clearly?
- Did the CRM mention customization options (semi-custom vs fully custom)?
- Were system types discussed (stick-on, clip-on, integration)?
- Was the natural look and quality emphasized?

#### G. Hairline Types Differentiation (Score: __/8) **[NEW CRITICAL PARAMETER]**
**IMPORTANT: If customer mentioned "hairline" or "front hairline", this MUST be evaluated carefully.**

The CRM must differentiate between TWO types:
1. **Just Hairline**: Only the front hairline is missing, crown and temples intact
   - Cost: ₹15,000 to ₹18,000
   - Simple, natural front hairline patch
2. **Hairline Patch**: Hair loss includes hairline + temple areas + some crown coverage
   - Cost: Starts from ₹25,000
   - More comprehensive coverage

**Evaluation Criteria:**
- Did the CRM ask clarifying questions to determine which type the customer needs?
- Did they explain the difference between "just hairline" vs "hairline patch"?
- Was pricing for each type clearly communicated?
- If customer only needs front hairline, did CRM mention the ₹15K-18K option?

**Score 8/8 if**: CRM clearly identified need and explained both options with correct pricing.
**Score 4-6/8 if**: CRM mentioned hairline but didn't differentiate types or pricing clearly.
**Score 0-3/8 if**: CRM confused the customer or failed to explain the difference.

#### H. Brand Differentiation (USPs) (Score: __/10) **[NEW CRITICAL PARAMETER]**
**Did the CRM effectively explain how American Hairline is different from competitors?**

Key USPs that should be mentioned:
- **Handmade Systems**: Single-strand implants, not machine-made
- **Premium Human Hair**: Double-drawn Remy human hair (superior quality)
- **Custom Fit Options**: Semi-customized (faster) and fully customized systems
- **Natural Hairline Expertise**: HD lace, low-density configurations for undetectable look
- **Phased Transition Approach**: Start with low density, gradually increase for natural change
- **Training & DIY Support**: Clients outside major cities taught self-servicing
- **Transparent Consultation**: Educates on pros/cons, helps make right long-term choice
- **Pan-India & International**: Ships internationally, branches in Mumbai, Delhi, Bangalore

**Evaluation Criteria:**
- Did the CRM proactively mention any USPs when customer asked "why you?" or "what's different?"?
- Were at least 3-4 key differentiators clearly explained?
- Did they sound confident and proud about the brand's unique value?

**Score 9-10/10 if**: CRM confidently explained 4+ USPs with clarity and enthusiasm.
**Score 6-8/10 if**: CRM mentioned 2-3 USPs but lacked depth or confidence.
**Score 0-5/10 if**: CRM failed to differentiate the brand or gave generic answers.

---

### 4) PRICING & SERVICE CLARITY (28 points)

#### I. Budget Justification (₹25K+ Packages) (Score: __/10) **[NEW CRITICAL PARAMETER]**
**CRITICAL: If customer mentioned budget below ₹25,000 or asked "why so expensive?", this is a key moment.**

**What CRM should do:**
1. Acknowledge the budget concern empathetically
2. Explain why packages start from ₹25,000:
   - Handmade, custom systems (not mass-produced)
   - Premium Remy human hair (lasts 1+ year with care)
   - Skilled craftsmanship and natural appearance
   - Includes consultation, styling, training
3. Frame it as an investment, not an expense
4. Compare to cheaper alternatives (which look fake, don't last)
5. Offer to show samples/portfolio during consultation

**RED FLAG**: If customer says budget is low and CRM simply says "just come for consultation" without justifying the price, this is a MAJOR FAILURE.

**Evaluation Criteria:**
- Did the CRM justify why systems start at ₹25K (not just deflect to consultation)?
- Was the value proposition clearly explained?
- Did they help the customer understand quality vs cost trade-off?

**Score 9-10/10 if**: CRM gave detailed, confident justification of pricing with value emphasis.
**Score 5-8/10 if**: CRM mentioned quality but didn't fully justify the ₹25K minimum.
**Score 0-4/10 if**: CRM avoided the question or just said "come for consultation" without explanation.

#### J. Delivery Timeline & Rush Charges (Score: __/8) **[NEW CRITICAL PARAMETER]**
**Standard delivery is 25-30 days. If customer asked about faster delivery, CRM must handle this properly.**

**What CRM should communicate:**
1. Standard delivery: 25-30 days (for semi-custom systems)
2. If customer needs urgent delivery:
   - Ask: "How soon do you need it?" (within a week? same day?)
   - Inform about $40 rush charge for expedited orders
   - Explain that rush orders may have limited customization

**Evaluation Criteria:**
- Did the CRM clearly state the 25-30 day standard timeline?
- If urgency mentioned, did they ask "how soon?" and explain rush charges?
- Was the timeline communicated without ambiguity?

**Score 7-8/8 if**: Timeline clearly stated, rush charges explained if relevant.
**Score 4-6/8 if**: Timeline mentioned but no clarity on rush charges when needed.
**Score 0-3/8 if**: No timeline mentioned or confused the customer about delivery.

#### K. Stick-On Servicing Details (Score: __/10) **[NEW CRITICAL PARAMETER]**
**If customer asked about servicing (maintenance, re-application, cleaning), CRM must provide specific details.**

**What CRM should communicate:**
1. **Servicing Cost**: ₹2,500 per session
2. **Service Packages Available**: Mention that consultant will explain package discounts in detail
3. **DIY Servicing**: If customer asks "Can I do it myself?":
   - Answer: "Yes, but NOT initially"
   - First 2 sessions MUST be done professionally
   - After that, we can train you for self-servicing (especially for clients outside major cities)

**Evaluation Criteria:**
- Did the CRM mention the ₹2,500 servicing cost?
- Did they mention service packages exist (even if details are for consultant)?
- If DIY asked, did they explain the "first 2 sessions professional" rule?

**Score 9-10/10 if**: All three points covered clearly (cost, packages, DIY policy).
**Score 6-8/10 if**: Mentioned cost and packages but missed DIY details.
**Score 0-5/10 if**: Vague or incomplete servicing information given.

---

## 5) OBJECTION HANDLING
**Questions & Objections Raised:**
List all questions and objections the customer raised.

**How CRM Handled Each:**
For each objection, assess:
- Was it addressed confidently?
- Was the response factually accurate?
- Did it resolve the customer's concern?

---

## 6) MISSED OPPORTUNITIES
**What Could Have Been Done Better:**
- What key information was not mentioned?
- What questions should have been asked but weren't?
- Where could the CRM have been more proactive?

---

## 7) CALL OUTCOME & NEXT STEPS
- **✔ Call Status**: Booked Consultation | Follow-up Scheduled | Undecided | Not Interested
- **Next Action Required**: No Action | Minor Feedback | Coaching Required | Retraining Needed | Escalate

---

## ✅ FINAL VERDICT & RECOMMENDATION
Provide a concise 2-3 sentence summary:
- Overall performance assessment
- Key strengths
- Most critical improvement area
- Actionable next step for this CRM executive

---

## 📊 11-PARAMETER SCORECARD (FILL WITH REAL NUMBERS)

**CORE COMMUNICATION SKILLS (44 points total):**
- Professional Greeting & Introduction Score: __/10
- Active Listening & Empathy Score: __/10
- Understanding Customer Needs Score: __/8
- Call Closure & Next Step Score: __/8
- Trust & Confidence Building Score: __/8

**PRODUCT & SERVICE KNOWLEDGE (28 points total):**
- General Product Explanation Score: __/10
- Hairline Types Differentiation Score: __/8
- Brand Differentiation (USPs) Score: __/10

**PRICING & SERVICE CLARITY (28 points total):**
- Budget Justification (₹25K+) Score: __/10
- Delivery Timeline & Rush Charges Score: __/8
- Stick-On Servicing Details Score: __/10

**TOTAL SCORE: __/100**

---

## CRITICAL INSTRUCTIONS FOR SCORING:
1. Replace EVERY "__" with actual integer scores
2. Keep the exact label format so the parser can extract scores
3. Be strict but fair - don't give high scores unless truly deserved
4. If a topic wasn't discussed in the call, score based on whether CRM SHOULD have brought it up

---

## MACHINE-READABLE JSON OUTPUT

After completing the human-readable report above, append on a NEW line ONLY this JSON (no extra words, no code fences) between these exact markers:

{JSON_START}
{{"greeting": <int>, "listening": <int>, "understanding_needs": <int>, "call_closure": <int>, "trust_building": <int>, "product_explanation": <int>, "hairline_types": <int>, "brand_differentiation": <int>, "budget_justification": <int>, "delivery_timeline": <int>, "servicing_details": <int>}}
{JSON_END}

Replace <int> with the actual scores from your evaluation above.
"""
    
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000,
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
                "call_closure": int(data.get("call_closure", 0)),
                "trust_building": int(data.get("trust_building", 0)),
                "product_explanation": int(data.get("product_explanation", 0)),
                "hairline_types": int(data.get("hairline_types", 0)),
                "brand_differentiation": int(data.get("brand_differentiation", 0)),
                "budget_justification": int(data.get("budget_justification", 0)),
                "delivery_timeline": int(data.get("delivery_timeline", 0)),
                "servicing_details": int(data.get("servicing_details", 0)),
            },
            cleaned,
        )
    except Exception as e:
        logging.warning(f"⚠️ JSON block extraction failed, will fallback to regex. {e}")
        return None, report_text

# Regex fallback for 11 parameters
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

        # Generate report
        full_transcript = clean_transcript(" ".join(parts).strip())
        raw_output = generate_openai_report(full_transcript)

        # 1) Try to extract JSON scores & strip from report (so PDF won't show JSON)
        scores, cleaned_report = extract_json_and_strip(raw_output)

        # 2) If JSON failed, fallback to regex; keep full text as report
        if scores is None:
            scores = parse_scores_from_report(raw_output)
            cleaned_report = raw_output

        logging.info("✅ Report generated successfully with 11 parameters")
        return {"report": cleaned_report, "scores": scores}

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})
