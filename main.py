# main.py - UPDATED VERSION WITH CONSULTATION CHECKLIST & CLIENT BEHAVIOR
# Version: 4.0
# New Features:
# - Consultation booking checklist detection (5 items)
# - Client behavior analysis (interest level + budget category)
# - Both are metadata (do NOT affect core scoring)

import os, re, json, tempfile, logging, subprocess, requests
from typing import List, Dict, Optional, Tuple, Union
from datetime import datetime
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

# ========== HELPERS (EXISTING) ==========
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

# ========== SCORE VALIDATION FUNCTION ==========
def validate_and_cap_scores(scores: Dict[str, ScoreValue]) -> Dict[str, ScoreValue]:
    """
    Validate scores don't exceed max values and cap them if they do.
    This is the "belt and suspenders" approach - validating in backend.
    
    Returns corrected scores with logging of any corrections made.
    """
    MAX_SCORES = {
        'greeting': 10,
        'listening': 10,
        'understanding_needs': 8,
        'call_closure': 8,
        'trust_building': 8,
        'product_explanation': 10,
        'hairline_types': 8,
        'brand_differentiation': 10,
        'budget_justification': 10,
        'delivery_timeline': 8,
        'servicing_details': 10
    }
    
    validated = {}
    corrections_made = []
    
    for key, value in scores.items():
        if value == "N/A" or value == "n/a" or value is None:
            validated[key] = "N/A"
        else:
            try:
                numeric_value = int(value) if isinstance(value, (int, float)) else int(value)
                max_allowed = MAX_SCORES.get(key, 10)
                
                if numeric_value > max_allowed:
                    corrections_made.append(
                        f"{key}: {numeric_value} → {max_allowed} (exceeded max)"
                    )
                    validated[key] = max_allowed
                    logging.warning(f"⚠️ Score capped: {key} from {numeric_value} to {max_allowed}")
                elif numeric_value < 0:
                    corrections_made.append(
                        f"{key}: {numeric_value} → 0 (negative score)"
                    )
                    validated[key] = 0
                    logging.warning(f"⚠️ Negative score corrected: {key} from {numeric_value} to 0")
                else:
                    validated[key] = numeric_value
            except (ValueError, TypeError):
                logging.warning(f"⚠️ Invalid score value for {key}: {value}, setting to N/A")
                validated[key] = "N/A"
    
    if corrections_made:
        logging.info(f"✅ Score validation applied: {len(corrections_made)} corrections made")
        for correction in corrections_made:
            logging.info(f"   - {correction}")
    
    return validated

# ========== ✨ NEW: INDIVIDUAL REPORT GENERATION (ENHANCED) ==========
def generate_openai_report(full_transcript: str) -> str:
    """
    ✨ UPDATED VERSION 4.0: Added consultation checklist & client behavior analysis
    Generate comprehensive CRM audit report with SMART CONDITIONAL SCORING (11 Parameters).
    Plus: Consultation Checklist & Client Behavior (metadata, not scored)
    """
    logging.info("📝 Generating OpenAI CRM report with consultation checklist & client behavior...")
    prompt = f"""
📞 [CRM Call Audit Evaluation – Enhanced with Consultation Checklist & Client Behavior]

You are a senior customer experience auditor for American Hairline, reviewing how a CRM executive handled a first-time inquiry call. Your evaluation must be FAIR and CONTEXT-AWARE.

## CALL TRANSCRIPT:
{full_transcript}

---

## ⚠️ CRITICAL SCORING RULES - READ CAREFULLY BEFORE SCORING:

**ABSOLUTE MAXIMUM SCORES - NEVER EVER EXCEED THESE:**

YOU MUST NEVER GIVE SCORES HIGHER THAN THESE MAXIMUMS. THIS IS NON-NEGOTIABLE.

- Professional Greeting & Introduction: **MAXIMUM 10** (Not 11, not 12, not 14 - MAX IS 10!)
- Active Listening & Empathy: **MAXIMUM 10** (Not 11, not 12, not 14 - MAX IS 10!)
- Understanding Customer Needs: **MAXIMUM 8** (Not 9, not 10 - MAX IS 8!)
- Call Closure & Next Step: **MAXIMUM 8** (Not 9, not 10 - MAX IS 8!)
- Trust & Confidence Building: **MAXIMUM 8** (Not 9, not 10 - MAX IS 8!)
- General Product Explanation: **MAXIMUM 10** (Not 11, not 12 - MAX IS 10!)
- Hairline Types Differentiation: **MAXIMUM 8** (Not 9, not 10 - MAX IS 8!)
- Brand Differentiation (USPs): **MAXIMUM 10** (Not 11, not 12 - MAX IS 10!)
- Budget Justification (₹25K+): **MAXIMUM 10** (Not 11, not 12 - MAX IS 10!)
- Delivery Timeline & Rush Charges: **MAXIMUM 8** (Not 9, not 10 - MAX IS 8!)
- Stick-On Servicing Details: **MAXIMUM 10** (Not 11, not 12 - MAX IS 10!)

---

## 🎯 EVALUATION INSTRUCTIONS

You will assess this call using the **HYBRID SMART APPROACH** with 11 parameters. Your job is to be INTELLIGENT and FAIR - not every parameter applies to every call.

### **CRITICAL SCORING LOGIC (READ CAREFULLY):**

For EACH of the 11 parameters below, follow this 4-STEP DECISION PROCESS:

#### **STEP 1: Was this topic discussed in the call?**
- **YES** → Score the quality (0-10 or 0-8 based on max) based on how well CRM handled it, then move to next parameter
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
**MAXIMUM SCORE: 10**

Evaluate IF greeting occurred:
- Was greeting warm, professional, and confident?
- Did CRM introduce themselves and brand clearly?
- Was tone appropriate for customer's mood?

**Score 0-10** if greeting happened, **N/A** only if call started mid-conversation.
**REMEMBER: 10 is the MAXIMUM. Do not give 11, 12, 13, or 14!**

---

#### **2. Active Listening & Empathy (Score: __/10 or N/A)**
**Priority: IMPORTANT**
**MAXIMUM SCORE: 10**

- Did CRM listen without interrupting?
- Were empathetic responses given to concerns?
- Did they acknowledge customer's emotions?

**Score 0-10** for any conversation, **N/A** only if call was too brief to judge.
**REMEMBER: 10 is the MAXIMUM. Do not give 11, 12, 13, or 14!**

---

#### **3. Understanding Customer Needs (Score: __/8 or N/A)**
**Priority: IMPORTANT**
**MAXIMUM SCORE: 8**

- Were qualifying questions asked?
- Did CRM identify what customer needs?
- Was there probing for hair loss type, lifestyle, budget?

**Score 0-8** if conversation allowed, **N/A** if customer only asked one specific thing and left.
**REMEMBER: 8 is the MAXIMUM. Do not give 9 or 10!**

---

#### **4. Call Closure & Next Step (Score: __/8 or N/A)**
**Priority: IMPORTANT**
**MAXIMUM SCORE: 8**

- Was clear next step communicated?
- Did CRM create urgency or excitement?
- Was commitment secured?

**Score 0-8** for most calls, **N/A** only if customer abruptly ended call.
**REMEMBER: 8 is the MAXIMUM. Do not give 9 or 10!**

---

#### **5. Trust & Confidence Building (Score: __/8 or N/A)**
**Priority: IMPORTANT**
**MAXIMUM SCORE: 8**

- Did CRM sound knowledgeable and confident?
- Were testimonials, celebrity clients, or social proof mentioned?
- Was reassurance provided?

**Score 0-8** if opportunity existed, **N/A** if call was too brief.
**REMEMBER: 8 is the MAXIMUM. Do not give 9 or 10!**

---

### **PRODUCT & SERVICE KNOWLEDGE (28 points max)**

#### **6. General Product Explanation (Score: __/10 or N/A)**
**Priority: CRITICAL** (almost always needed in general inquiries)
**MAXIMUM SCORE: 10**

- Were American Hairline's offerings explained?
- Customization options mentioned?
- System types discussed?
- Natural look emphasized?

**Scoring:**
- If general inquiry + NOT explained → **Score 0-5** (critical miss)
- If specific question + product explained → **Score 6-10**
- If customer only asked location/timing → **N/A**

**REMEMBER: 10 is the MAXIMUM. Do not give 11, 12, or higher!**

---

#### **7. Hairline Types Differentiation (Score: __/8 or N/A)**
**Priority: CONTEXTUAL** (only if customer mentioned "hairline")
**MAXIMUM SCORE: 8**

**ONLY score this if customer specifically mentioned hairline/front hairline.**

If customer said "hairline":
- Did CRM differentiate "just hairline" (₹15K-18K) vs "hairline patch" (₹25K+)?
- Were both options explained with pricing?

**Scoring:**
- If hairline discussed + well explained → **Score 6-8**
- If hairline discussed + poorly explained → **Score 0-5**
- If hairline NOT mentioned by customer → **N/A**

**REMEMBER: 8 is the MAXIMUM. Do not give 9 or 10!**

---

#### **8. Brand Differentiation (USPs) (Score: __/10 or N/A)**
**Priority: IMPORTANT** (should mention in general inquiries)
**MAXIMUM SCORE: 10**

USPs: Handmade systems, Premium Remy hair, Custom fit, Natural hairlines, Training support, Transparent consultation, Pan-India reach

**Scoring:**
- If customer asked "why you?" or general inquiry + USPs explained → **Score 7-10**
- If general inquiry + USPs NOT mentioned → **Score 3-6** (missed opportunity)
- If specific quick question → **N/A**

**REMEMBER: 10 is the MAXIMUM. Do not give 11, 12, or higher!**

---

### **PRICING & SERVICE CLARITY (28 points max)**

#### **9. Budget Justification (₹25K+ Packages) (Score: __/10 or N/A)**
**Priority: CRITICAL** (if pricing discussed)
**MAXIMUM SCORE: 10**

**RED FLAG**: If customer said "too expensive" or asked about pricing, CRM MUST justify value.

**Scoring:**
- If price discussed + excellent justification → **Score 8-10**
- If price discussed + weak justification → **Score 4-7**
- If price discussed + just said "come for consultation" → **Score 0-3** (critical failure)
- If pricing NOT discussed at all → **N/A**

**REMEMBER: 10 is the MAXIMUM. Do not give 11, 12, or higher!**

---

#### **10. Delivery Timeline & Rush Charges (Score: __/8 or N/A)**
**Priority: CONTEXTUAL** (only if customer asked about timing)
**MAXIMUM SCORE: 8**

Standard: 25-30 days. Rush: Ask "how soon?" + $40 charge.

**Scoring:**
- If customer asked about delivery + CRM explained well → **Score 6-8**
- If customer asked + CRM vague → **Score 0-5**
- If timing NOT discussed → **N/A**

**REMEMBER: 8 is the MAXIMUM. Do not give 9 or 10!**

---

#### **11. Stick-On Servicing Details (Score: __/10 or N/A)**
**Priority: CONTEXTUAL** (only if customer asked about maintenance)
**MAXIMUM SCORE: 10**

Details: ₹2,500/session, packages available, first 2 sessions must be professional.

**Scoring:**
- If customer asked about servicing + CRM explained well → **Score 8-10**
- If customer asked + CRM incomplete info → **Score 4-7**
- If servicing NOT discussed → **N/A**

**REMEMBER: 10 is the MAXIMUM. Do not give 11, 12, or higher!**

---

## ✨ NEW SECTION: CONSULTATION BOOKING CHECKLIST

**IMPORTANT: This section does NOT affect the caller's score. It is for process compliance tracking only.**

### **STEP 1: Determine if this is a Consultation Booking Call**

Look for these indicators:
- Customer explicitly says "I want to book a consultation" or "schedule an appointment"
- Customer asks "how do I book?" or "what's the next step?"
- CRM offers to book a consultation and customer agrees
- Discussion about consultation fee (₹500)
- Talk about sending forms, videos, or scheduling

**If YES → This is a consultation booking call, proceed to STEP 2**
**If NO → Skip this entire section, mark as "Not Applicable"**

### **STEP 2: Check if CRM communicated these 5 mandatory items**

For consultation booking calls ONLY, verify if the CRM mentioned/explained:

#### **1. Payment Fee (₹500) - Did CRM mention it?**
- Did CRM inform that consultation fee is ₹500?
- Did CRM ask if client can make payment or guide on payment method?
- **YES** = Payment mentioned | **NO** = Payment NOT mentioned

#### **2. Mandatory Form - Did CRM mention it?**
- Did CRM tell client that a mandatory form will be shared on WhatsApp?
- Did CRM inform that client must fill the form before consultation?
- **YES** = Form mentioned | **NO** = Form NOT mentioned

#### **3. Pre-Consultation Videos - Did CRM mention them?**
- Did CRM mention that pre-consultation videos will be shared on WhatsApp?
- Did CRM instruct client to watch videos before consultation?
- **YES** = Videos mentioned | **NO** = Videos NOT mentioned

#### **4. Client Questions Request - Did CRM ask for them?**
- Did CRM ask client to send their questions (topics they want discussed)?
- Did CRM request client to prepare questions beforehand?
- **YES** = Questions requested | **NO** = Questions NOT requested

#### **5. Photo Requirements - Did CRM ask for them?**
- Did CRM tell client to share their recent picture?
- Did CRM ask for the hairstyle client wants?
- Did CRM mention both old photo + desired style?
- **YES** = Photos requested | **NO** = Photos NOT requested

---

## ✨ NEW SECTION: CLIENT BEHAVIOR ANALYSIS

**IMPORTANT: This section does NOT affect the caller's score. It is for internal lead quality classification only.**

Analyze the CLIENT's behavior and intent from the conversation:

### **1. Interest Level Assessment**

Based on the client's engagement, classify as:

**HIGH INTEREST:**
- Asks multiple detailed questions (3+ questions about product, process, pricing)
- Discusses specific needs (density, color, style, hair type)
- Agrees to consultation/payment without hesitation
- Uses action-oriented language: "when can I get", "I need this by", "let's book", "I'm ready"
- Shows urgency or commitment
- Responds with enthusiasm and follow-up questions

**MEDIUM INTEREST:**
- Asks 1-2 basic questions
- Responds positively but non-committal ("sounds good", "okay", "I see")
- Says "I'll think about it", "let me check", "I need to discuss"
- Polite but not deeply engaged
- Asks to call back later or requests more info via WhatsApp

**LOW INTEREST:**
- Very brief responses ("okay", "fine", "alright")
- Just price shopping - only asks "how much?" with no other questions
- No follow-up questions after initial answer
- Says "too expensive" and quickly ends call
- Sounds distracted or uninterested
- Gives vague responses or tries to end call quickly

**CANNOT DETERMINE:**
- Call too short (< 1 minute)
- Call dropped or technical issue
- Client only asked one specific administrative question (location, hours, contact)
- Insufficient conversation to gauge interest

**Select ONE: HIGH | MEDIUM | LOW | CANNOT_DETERMINE**

---

### **2. Budget Category Assessment**

Based on what the client revealed about their budget:

**ABOVE ₹25,000:**
- Client explicitly states budget above ₹25K (e.g., "I can spend ₹30,000")
- Client asks about ₹25K+ packages or premium options
- Client doesn't object when hearing ₹25K+ pricing
- Client agrees to ₹500 consultation fee without hesitation
- Client discusses customization, premium features (indicates higher budget)

**BELOW ₹25,000:**
- Client explicitly states budget below ₹25K (e.g., "my budget is ₹15,000")
- Client says "too expensive" when hearing ₹25K+ prices
- Client specifically asks "do you have anything cheaper?"
- Client asks about basic/economy options only
- Client hesitates or objects to ₹500 consultation fee

**NOT DISCUSSED:**
- Price/budget never mentioned in the call
- Client didn't reveal or hint at their budget range
- Client deflected budget questions
- Conversation ended before budget discussion

**Select ONE: ABOVE_25K | BELOW_25K | NOT_DISCUSSED**

---

### **3. Reasoning (Brief Explanation)**

Provide a 1-2 sentence explanation for your interest level and budget classification.

**Example:**
"Client asked detailed questions about customization options and agreed to ₹500 consultation without hesitation, indicating serious interest and budget above ₹25K."

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

**REMEMBER: DO NOT EXCEED MAXIMUM SCORES!**

**CORE COMMUNICATION SKILLS:**
- Professional Greeting & Introduction Score: __/10 (or "N/A") - MAX IS 10!
- Active Listening & Empathy Score: __/10 (or "N/A") - MAX IS 10!
- Understanding Customer Needs Score: __/8 (or "N/A") - MAX IS 8!
- Call Closure & Next Step Score: __/8 (or "N/A") - MAX IS 8!
- Trust & Confidence Building Score: __/8 (or "N/A") - MAX IS 8!

**PRODUCT & SERVICE KNOWLEDGE:**
- General Product Explanation Score: __/10 (or "N/A") - MAX IS 10!
- Hairline Types Differentiation Score: __/8 (or "N/A") - MAX IS 8!
- Brand Differentiation (USPs) Score: __/10 (or "N/A") - MAX IS 10!

**PRICING & SERVICE CLARITY:**
- Budget Justification (₹25K+) Score: __/10 (or "N/A") - MAX IS 10!
- Delivery Timeline & Rush Charges Score: __/8 (or "N/A") - MAX IS 8!
- Stick-On Servicing Details Score: __/10 (or "N/A") - MAX IS 10!

**TOTAL SCORE:** Will be calculated by system

---

## ✨ CONSULTATION BOOKING CHECKLIST (NEW - NOT SCORED)

**Is this a consultation booking call?** YES / NO

**If YES, check these 5 items:**
- Payment Fee (₹500) Mentioned: YES / NO
- Mandatory Form Explained: YES / NO
- Pre-Consultation Videos Mentioned: YES / NO
- Client Questions Requested: YES / NO
- Photo Requirements Explained: YES / NO

**If NO:** Mark entire section as "Not Applicable"

---

## ✨ CLIENT BEHAVIOR ANALYSIS (NEW - NOT SCORED)

**Interest Level:** HIGH / MEDIUM / LOW / CANNOT_DETERMINE

**Budget Category:** ABOVE_25K / BELOW_25K / NOT_DISCUSSED

**Reasoning:** [1-2 sentence explanation of why you classified the client this way]

---

## ⚙️ MACHINE-READABLE JSON OUTPUT

After completing the human-readable report, append this JSON between markers (no code fences, no extra text):

{JSON_START}
{{
  "greeting": <int 0-10 or "N/A">,
  "listening": <int 0-10 or "N/A">,
  "understanding_needs": <int 0-8 or "N/A">,
  "call_closure": <int 0-8 or "N/A">,
  "trust_building": <int 0-8 or "N/A">,
  "product_explanation": <int 0-10 or "N/A">,
  "hairline_types": <int 0-8 or "N/A">,
  "brand_differentiation": <int 0-10 or "N/A">,
  "budget_justification": <int 0-10 or "N/A">,
  "delivery_timeline": <int 0-8 or "N/A">,
  "servicing_details": <int 0-10 or "N/A">,
  "consultation_checklist": {{
    "is_booking_call": true/false,
    "payment_mentioned": true/false or null,
    "form_mentioned": true/false or null,
    "videos_mentioned": true/false or null,
    "questions_requested": true/false or null,
    "photos_requested": true/false or null
  }},
  "client_behavior": {{
    "interest_level": "HIGH"/"MEDIUM"/"LOW"/"CANNOT_DETERMINE",
    "budget_category": "ABOVE_25K"/"BELOW_25K"/"NOT_DISCUSSED",
    "reasoning": "Brief 1-2 sentence explanation..."
  }}
}}
{JSON_END}

**CRITICAL JSON RULES:**
- Use actual integers (0-10 or 0-8) for scored parameters within the maximum limits
- Use string "N/A" for not applicable parameters
- DO NOT EXCEED MAXIMUM SCORES IN JSON!
- For consultation_checklist: if is_booking_call is false, set all other fields to null
- For client_behavior: always provide all three fields (never null)
- Example: {{"greeting": 8, "hairline_types": "N/A", "consultation_checklist": {{"is_booking_call": false, "payment_mentioned": null, ...}}, "client_behavior": {{"interest_level": "HIGH", "budget_category": "ABOVE_25K", "reasoning": "..."}}}}
- WRONG: {{"greeting": 14, ...}} ← This exceeds max of 10!
"""
    
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4500,  # Increased for longer response
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def extract_json_and_strip(report_text: str) -> Tuple[Optional[Dict], str]:
    """
    ✨ UPDATED: Extract enhanced JSON with consultation checklist & client behavior
    """
    try:
        start = report_text.index(JSON_START) + len(JSON_START)
        end   = report_text.index(JSON_END, start)
        json_str = report_text[start:end].strip()
        data = json.loads(json_str)

        # Remove the entire JSON block with markers from the human report
        cleaned = report_text[:report_text.index(JSON_START)].rstrip()
        
        # Process 11 core scores
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
        
        # ✨ NEW: Extract consultation checklist
        consultation_checklist = data.get("consultation_checklist", {
            "is_booking_call": False,
            "payment_mentioned": None,
            "form_mentioned": None,
            "videos_mentioned": None,
            "questions_requested": None,
            "photos_requested": None
        })
        
        # ✨ NEW: Extract client behavior
        client_behavior = data.get("client_behavior", {
            "interest_level": "CANNOT_DETERMINE",
            "budget_category": "NOT_DISCUSSED",
            "reasoning": "Insufficient data to determine"
        })
        
        return {
            "scores": scores,
            "consultation_checklist": consultation_checklist,
            "client_behavior": client_behavior
        }, cleaned
        
    except Exception as e:
        logging.warning(f"⚠️ JSON block extraction failed, will fallback to regex. {e}")
        return None, report_text

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

# ========== CONSOLIDATED REPORT GENERATION (EXISTING - NO CHANGES) ==========

def generate_consolidated_daily_report(
    agent_name: str,
    report_date: str,
    calls_data: List[Dict],
    aggregate_stats: Dict
) -> Dict:
    """
    Generate AI-powered consolidated daily report for an agent.
    ✨ UPDATED: Now includes consultation checklist & client behavior insights
    """
    logging.info(f"📊 Generating consolidated daily report for {agent_name} on {report_date}")
    
    # Prepare call summaries for the prompt
    call_summaries = []
    for i, call in enumerate(calls_data, 1):
        summary = f"""
**Call {i}/{len(calls_data)}:**
- Customer: {call.get('customer', 'Unknown')}
- Duration: {call.get('duration', 'N/A')}
- Final Score: {call.get('final_score', 'N/A')}
- Individual Scores: {json.dumps(call.get('scores', {}), indent=2)}

**Transcript Excerpt (First 500 chars):**
{call.get('transcript', '')[:500]}...

**Individual Report Summary:**
{call.get('individual_report', '')[:800]}...
"""
        call_summaries.append(summary)
    
    all_calls_text = "\n\n---\n\n".join(call_summaries)
    
    # Build the prompt
    prompt = f"""
📊 **CONSOLIDATED DAILY PERFORMANCE REPORT - AGENT ANALYSIS**

You are a senior CRM performance analyst at American Hairline. Your task is to analyze ALL calls made by **{agent_name}** on **{report_date}** and provide actionable insights for coaching and improvement.

---

## 📈 AGGREGATE STATISTICS

- **Total Calls Analyzed:** {aggregate_stats.get('total_calls', 0)}
- **Average Final Score:** {aggregate_stats.get('avg_final_score', 0):.1f}/100
- **Average Parameter Scores:**
{json.dumps(aggregate_stats.get('avg_scores', {}), indent=2)}

---

## 📞 INDIVIDUAL CALL DATA

{all_calls_text}

---

## 🎯 YOUR TASK

Analyze ALL the calls above and provide a comprehensive consolidated report with the following sections:

### **1. COMMON MISTAKES (Top 3-5 recurring issues)**
Identify patterns of mistakes that appear across multiple calls. Be specific and cite call numbers as evidence.

Format:
- "Issue description (appeared in X/Y calls)" - [Example: Call 2, Call 5]

### **2. STRENGTHS (Top 3-4 consistent strong points)**
Highlight what the agent does well across most calls.

Format:
- "Strength description" - [Example: Consistently in Call 1, Call 3, Call 7]

### **3. PRIORITY ACTION ITEMS (Top 3-4 specific improvements)**
Provide concrete, actionable steps for improvement. Prioritize by impact.

Format:
1. **[HIGH PRIORITY]** Action item with specific technique or approach
2. **[MEDIUM PRIORITY]** Action item
3. **[LOW PRIORITY]** Action item

### **4. COACHING NOTES**
2-3 sentences summarizing the agent's overall performance level and recommended coaching approach.

### **5. SPECIFIC EXAMPLES**
- **Best Moment:** Cite the specific call and what was done exceptionally well
- **Worst Moment:** Cite the specific call and what went wrong
- **Teaching Moment:** One specific example that would be valuable for training

---

## ⚙️ OUTPUT FORMAT

Return your analysis in the following JSON structure (no code fences, just raw JSON):

{{
  "common_mistakes": [
    "Mistake 1 description (X/Y calls) - [Call numbers]",
    "Mistake 2 description (X/Y calls) - [Call numbers]",
    ...
  ],
  "strengths": [
    "Strength 1 description - [Call numbers]",
    "Strength 2 description - [Call numbers]",
    ...
  ],
  "action_items": [
    {{
      "priority": "HIGH",
      "action": "Specific action item description"
    }},
    {{
      "priority": "MEDIUM",
      "action": "Specific action item description"
    }},
    ...
  ],
  "coaching_notes": "2-3 sentence summary of performance and coaching approach",
  "specific_examples": {{
    "best_moment": "Call X - Description of what was done well",
    "worst_moment": "Call Y - Description of what went wrong",
    "teaching_moment": "Call Z - Specific example valuable for training"
  }}
}}

**CRITICAL INSTRUCTIONS:**
- Be specific and cite call numbers as evidence
- Focus on ACTIONABLE insights, not generic feedback
- Identify PATTERNS across multiple calls, not isolated incidents
- Prioritize issues by frequency and impact
- Keep coaching notes constructive and solution-focused
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3,
        )
        
        response_text = resp.choices[0].message.content.strip()
        
        # Try to parse as JSON
        # Remove markdown code fences if present
        if response_text.startswith("```"):
            response_text = re.sub(r'```json\s*|\s*```', '', response_text).strip()
        
        insights = json.loads(response_text)
        logging.info(f"✅ Successfully generated consolidated daily report for {agent_name}")
        return insights
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ Failed to parse JSON from GPT-4o response: {e}")
        # Return fallback structure
        return {
            "common_mistakes": ["Error: Could not parse AI response"],
            "strengths": ["Error: Could not parse AI response"],
            "action_items": [{"priority": "HIGH", "action": "Manual review needed"}],
            "coaching_notes": "AI analysis failed - manual review required",
            "specific_examples": {
                "best_moment": "N/A",
                "worst_moment": "N/A",
                "teaching_moment": "N/A"
            }
        }
    except Exception as e:
        logging.error(f"❌ Error generating consolidated report: {e}")
        raise


def generate_consolidated_weekly_report(
    agent_name: str,
    week_start: str,
    week_end: str,
    daily_summaries: List[Dict],
    aggregate_stats: Dict
) -> Dict:
    """
    Generate AI-powered consolidated weekly report for an agent.
    (No changes needed - this function is working correctly)
    """
    logging.info(f"📊 Generating consolidated weekly report for {agent_name} ({week_start} to {week_end})")
    
    # Prepare daily summaries
    daily_texts = []
    for i, day in enumerate(daily_summaries, 1):
        daily_text = f"""
**Day {i} - {day.get('date', 'N/A')}:**
- Calls: {day.get('total_calls', 0)}
- Avg Score: {day.get('avg_score', 0):.1f}/100
- Common Mistakes: {', '.join(day.get('common_mistakes', [])[:3])}
- Strengths: {', '.join(day.get('strengths', [])[:2])}
"""
        daily_texts.append(daily_text)
    
    all_days_text = "\n".join(daily_texts)
    
    prompt = f"""
📊 **CONSOLIDATED WEEKLY PERFORMANCE REPORT - AGENT ANALYSIS**

You are a senior CRM performance analyst. Analyze the WEEKLY performance of **{agent_name}** for the week of **{week_start} to {week_end}**.

---

## 📈 WEEKLY AGGREGATE STATISTICS

- **Total Calls This Week:** {aggregate_stats.get('total_calls', 0)}
- **Average Weekly Score:** {aggregate_stats.get('avg_final_score', 0):.1f}/100
- **Weekly Parameter Averages:**
{json.dumps(aggregate_stats.get('avg_scores', {}), indent=2)}

---

## 📅 DAILY BREAKDOWN

{all_days_text}

---

## 🎯 YOUR TASK

Provide a weekly performance analysis with:

### **1. WEEKLY TREND ANALYSIS**
Analyze if performance is improving, declining, or stable. Cite specific evidence from daily data.

### **2. KEY WEEKLY INSIGHTS (3-4 points)**
What are the most important patterns or observations from this week?

### **3. PRIORITY ACTION ITEMS FOR NEXT WEEK (Top 3)**
What should the agent focus on in the coming week?

### **4. WEEKLY COACHING RECOMMENDATION**
Brief recommendation on coaching approach for next week.

---

## ⚙️ OUTPUT FORMAT (JSON)

{{
  "trend_analysis": "Improving/Declining/Stable with specific evidence",
  "weekly_insights": [
    "Insight 1 with supporting data",
    "Insight 2 with supporting data",
    ...
  ],
  "action_items": [
    {{
      "priority": "HIGH",
      "action": "Specific weekly goal"
    }},
    ...
  ],
  "coaching_recommendation": "Brief coaching strategy for next week"
}}

**CRITICAL:**
- Compare performance across days
- Identify improving/declining trends
- Be specific with data points
- Focus on actionable weekly goals
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.3,
        )
        
        response_text = resp.choices[0].message.content.strip()
        if response_text.startswith("```"):
            response_text = re.sub(r'```json\s*|\s*```', '', response_text).strip()
        
        insights = json.loads(response_text)
        logging.info(f"✅ Successfully generated consolidated weekly report for {agent_name}")
        return insights
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ Failed to parse JSON from GPT-4o response: {e}")
        return {
            "trend_analysis": "Error: Could not parse AI response",
            "weekly_insights": ["Error: Could not parse AI response"],
            "action_items": [{"priority": "HIGH", "action": "Manual review needed"}],
            "coaching_recommendation": "AI analysis failed - manual review required"
        }
    except Exception as e:
        logging.error(f"❌ Error generating weekly report: {e}")
        raise


def generate_consolidated_monthly_report(
    agent_name: str,
    month: str,
    year: int,
    weekly_summaries: List[Dict],
    aggregate_stats: Dict,
    previous_month_stats: Optional[Dict] = None
) -> Dict:
    """
    Generate AI-powered consolidated monthly report for an agent.
    (No changes needed - this function is working correctly)
    """
    logging.info(f"📊 Generating consolidated monthly report for {agent_name} ({month} {year})")
    
    # Prepare weekly summaries
    weekly_texts = []
    for i, week in enumerate(weekly_summaries, 1):
        weekly_text = f"""
**Week {i} ({week.get('week_start', 'N/A')} to {week.get('week_end', 'N/A')}):**
- Calls: {week.get('total_calls', 0)}
- Avg Score: {week.get('avg_score', 0):.1f}/100
- Trend: {week.get('trend', 'N/A')}
- Key Insights: {', '.join(week.get('weekly_insights', [])[:2])}
"""
        weekly_texts.append(weekly_text)
    
    all_weeks_text = "\n".join(weekly_texts)
    
    # Month-over-month comparison
    mom_comparison = ""
    if previous_month_stats:
        prev_score = previous_month_stats.get('avg_final_score', 0)
        curr_score = aggregate_stats.get('avg_final_score', 0)
        change = curr_score - prev_score
        mom_comparison = f"""
## 📊 MONTH-OVER-MONTH COMPARISON

- **Previous Month Average:** {prev_score:.1f}/100
- **Current Month Average:** {curr_score:.1f}/100
- **Change:** {'+' if change >= 0 else ''}{change:.1f} points ({'+' if change >= 0 else ''}{(change/prev_score*100 if prev_score > 0 else 0):.1f}%)
"""
    
    prompt = f"""
📊 **CONSOLIDATED MONTHLY PERFORMANCE REPORT - AGENT ANALYSIS**

You are a senior CRM performance analyst. Provide a comprehensive MONTHLY analysis for **{agent_name}** for **{month} {year}**.

---

## 📈 MONTHLY AGGREGATE STATISTICS

- **Total Calls This Month:** {aggregate_stats.get('total_calls', 0)}
- **Average Monthly Score:** {aggregate_stats.get('avg_final_score', 0):.1f}/100
- **Monthly Parameter Averages:**
{json.dumps(aggregate_stats.get('avg_scores', {}), indent=2)}

{mom_comparison}

---

## 📅 WEEKLY BREAKDOWN

{all_weeks_text}

---

## 🎯 YOUR TASK

Provide a comprehensive monthly performance analysis with:

### **1. MONTHLY TREND ANALYSIS**
Analyze overall trajectory over the month. Did performance improve, decline, or plateau?

### **2. KEY ACHIEVEMENTS (Top 3-4)**
What did the agent do well this month? What improved?

### **3. FOCUS AREAS (Top 3-4)**
What needs work? What didn't improve or declined?

### **4. MONTHLY GOALS FOR NEXT MONTH (Top 3)**
Based on this month's data, what should be the priority goals for next month?

### **5. STRATEGIC COACHING RECOMMENDATION**
High-level coaching strategy and development plan for the agent.

---

## ⚙️ OUTPUT FORMAT (JSON)

{{
  "monthly_trend": "Detailed trend analysis with evidence",
  "key_achievements": [
    "Achievement 1 with supporting data",
    "Achievement 2 with supporting data",
    ...
  ],
  "focus_areas": [
    "Focus area 1 with specific metrics",
    "Focus area 2 with specific metrics",
    ...
  ],
  "monthly_goals": [
    {{
      "priority": "HIGH",
      "goal": "Specific measurable goal for next month"
    }},
    ...
  ],
  "coaching_recommendation": "Strategic coaching and development plan"
}}

**CRITICAL:**
- Use actual data points and metrics
- Compare week-to-week progression
- Identify long-term patterns
- Set SMART goals for next month
- Be strategic, not tactical
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3,
        )
        
        response_text = resp.choices[0].message.content.strip()
        if response_text.startswith("```"):
            response_text = re.sub(r'```json\s*|\s*```', '', response_text).strip()
        
        insights = json.loads(response_text)
        logging.info(f"✅ Successfully generated consolidated monthly report for {agent_name}")
        return insights
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ Failed to parse JSON from GPT-4o response: {e}")
        return {
            "monthly_trend": "Error: Could not parse AI response",
            "key_achievements": ["Error: Could not parse AI response"],
            "focus_areas": ["Error: Could not parse AI response"],
            "monthly_goals": [{"priority": "HIGH", "goal": "Manual review needed"}],
            "coaching_recommendation": "AI analysis failed - manual review required"
        }
    except Exception as e:
        logging.error(f"❌ Error generating monthly report: {e}")
        raise


# ========== API ROUTES ==========

@app.post("/generate-report")
async def generate_report_endpoint(request: Request):
    """
    ✨ UPDATED: Individual call audit endpoint with consultation checklist & client behavior
    """
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

        # Generate report with smart conditional scoring + new features
        full_transcript = clean_transcript(" ".join(parts).strip())
        raw_output = generate_openai_report(full_transcript)

        # 1) Try to extract JSON (scores + consultation + behavior) & strip from report
        extracted_data, cleaned_report = extract_json_and_strip(raw_output)

        # 2) If JSON failed, fallback to regex for scores only
        if extracted_data is None:
            scores = parse_scores_from_report(raw_output)
            cleaned_report = raw_output
            
            # Set defaults for new fields
            consultation_checklist = {
                "is_booking_call": False,
                "payment_mentioned": None,
                "form_mentioned": None,
                "videos_mentioned": None,
                "questions_requested": None,
                "photos_requested": None
            }
            client_behavior = {
                "interest_level": "CANNOT_DETERMINE",
                "budget_category": "NOT_DISCUSSED",
                "reasoning": "Failed to extract from report"
            }
        else:
            scores = extracted_data["scores"]
            consultation_checklist = extracted_data["consultation_checklist"]
            client_behavior = extracted_data["client_behavior"]

        # 3) Validate and cap scores
        if scores:
            scores = validate_and_cap_scores(scores)

        logging.info(f"✅ Report generated with validated scores: {scores}")
        logging.info(f"✅ Consultation checklist: {consultation_checklist}")
        logging.info(f"✅ Client behavior: {client_behavior}")
        
        return {
            "report": cleaned_report,
            "scores": scores,
            "consultation_checklist": consultation_checklist,  # ✨ NEW
            "client_behavior": client_behavior  # ✨ NEW
        }

    except Exception as e:
        logging.exception("❌ Report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/generate-consolidated-report")
async def generate_consolidated_report_endpoint(request: Request):
    """
    Generate consolidated daily report for multiple calls by same agent
    (No changes needed - working correctly)
    """
    try:
        data = await request.json()
        
        # Validate required fields
        agent_name = data.get("agent_name")
        report_type = data.get("report_type", "daily")
        date = data.get("date")
        calls = data.get("calls", [])
        aggregate_stats = data.get("aggregate_stats", {})
        
        if not agent_name or not date or not calls:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing required fields: agent_name, date, or calls"}
            )
        
        logging.info(f"🎯 Generating consolidated {report_type} report for {agent_name} on {date}")
        logging.info(f"📊 Processing {len(calls)} calls")
        
        # Generate consolidated report
        if report_type == "daily":
            insights = generate_consolidated_daily_report(
                agent_name=agent_name,
                report_date=date,
                calls_data=calls,
                aggregate_stats=aggregate_stats
            )
        else:
            return JSONResponse(
                status_code=400,
                content={"error": f"Unsupported report_type: {report_type}"}
            )
        
        logging.info(f"✅ Consolidated report generated successfully for {agent_name}")
        return {
            "agent_name": agent_name,
            "report_type": report_type,
            "date": date,
            "insights": insights,
            "aggregate_stats": aggregate_stats
        }
        
    except Exception as e:
        logging.exception("❌ Consolidated report generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/generate-weekly-insights")
async def generate_weekly_insights_endpoint(request: Request):
    """
    Generate consolidated weekly report
    (No changes needed - working correctly)
    """
    try:
        data = await request.json()
        
        agent_name = data.get("agent_name")
        week_start = data.get("week_start")
        week_end = data.get("week_end")
        daily_summaries = data.get("daily_summaries", [])
        aggregate_stats = data.get("aggregate_stats", {})
        
        if not agent_name or not week_start or not week_end:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing required fields: agent_name, week_start, or week_end"}
            )
        
        logging.info(f"🎯 Generating weekly insights for {agent_name} ({week_start} to {week_end})")
        
        insights = generate_consolidated_weekly_report(
            agent_name=agent_name,
            week_start=week_start,
            week_end=week_end,
            daily_summaries=daily_summaries,
            aggregate_stats=aggregate_stats
        )
        
        logging.info(f"✅ Weekly insights generated successfully for {agent_name}")
        return {
            "agent_name": agent_name,
            "week_start": week_start,
            "week_end": week_end,
            "insights": insights,
            "aggregate_stats": aggregate_stats
        }
        
    except Exception as e:
        logging.exception("❌ Weekly insights generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/generate-monthly-insights")
async def generate_monthly_insights_endpoint(request: Request):
    """
    Generate consolidated monthly report
    (No changes needed - working correctly)
    """
    try:
        data = await request.json()
        
        agent_name = data.get("agent_name")
        month = data.get("month")
        year = data.get("year")
        weekly_summaries = data.get("weekly_summaries", [])
        aggregate_stats = data.get("aggregate_stats", {})
        previous_month_stats = data.get("previous_month_stats")
        
        if not agent_name or not month or not year:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing required fields: agent_name, month, or year"}
            )
        
        logging.info(f"🎯 Generating monthly insights for {agent_name} ({month} {year})")
        
        insights = generate_consolidated_monthly_report(
            agent_name=agent_name,
            month=month,
            year=year,
            weekly_summaries=weekly_summaries,
            aggregate_stats=aggregate_stats,
            previous_month_stats=previous_month_stats
        )
        
        logging.info(f"✅ Monthly insights generated successfully for {agent_name}")
        return {
            "agent_name": agent_name,
            "month": month,
            "year": year,
            "insights": insights,
            "aggregate_stats": aggregate_stats
        }
        
    except Exception as e:
        logging.exception("❌ Monthly insights generation failed")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ========== HEALTH CHECK ==========
@app.get("/")
async def root():
    return {
        "status": "running",
        "service": "CRM Insights API with Consolidated Reporting",
        "version": "4.0",
        "updates": [
            "Added consultation booking checklist (5 items)",
            "Added client behavior analysis (interest level + budget category)",
            "Both new features are metadata (do NOT affect core scoring)",
            "Enhanced JSON response structure"
        ],
        "endpoints": [
            "/generate-report (POST) - Individual call audit with checklist & behavior",
            "/generate-consolidated-report (POST) - Daily consolidated report",
            "/generate-weekly-insights (POST) - Weekly consolidated report",
            "/generate-monthly-insights (POST) - Monthly consolidated report"
        ]
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "4.0"
    }
