import os
import json
import time
import uuid
from datetime import datetime
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, FileResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from pymongo import MongoClient
from groq import Groq 
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# --- INITIALIZATION ---
if not os.path.exists("static"):
    os.makedirs("static")

try:
    mongo_client = MongoClient(os.getenv("MONGO_URI"))
    db = mongo_client["RestaurantDB"] 
    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
except Exception as e:
    print(f"CRITICAL Resource Failure: {e}")

# Use a dictionary to keep track of histories for different callers
call_sessions = {}

def get_system_prompt():
    return {
        "role": "system", 
        "content": f"""You are Jessica, the professional concierge at 'The Velvet Bean Bistro'. 
        TODAY: {datetime.now().strftime('%A, %d %B %Y')}
        GOAL: Collect 1. Name, 2. Date, 3. Day, 4. Time, 5. Guests.
        JSON ONLY FORMAT:
        {{
            "reply": "verbal response",
            "is_complete": false,
            "data": {{"name": "null", "date": "null", "day": "null", "time": "null", "guests": "null", "notes": "null"}}
        }}"""
    }

# --- HELPER FUNCTIONS ---

def generate_audio(text, filename):
    try:
        audio_generator = el_client.text_to_speech.convert(
            voice_id="cgSgspJ2msm6clMCkdW9", 
            text=text,
            model_id="eleven_turbo_v2_5"
        )
        file_path = f"static/{filename}.mp3"
        with open(file_path, "wb") as f:
            for chunk in audio_generator: f.write(chunk)
        return True
    except Exception as e:
        print(f"ElevenLabs Failover: {e}")
        return False

def get_ai_response(user_input, caller_number):
    if caller_number not in call_sessions:
        call_sessions[caller_number] = [get_system_prompt()]
    
    call_sessions[caller_number].append({"role": "user", "content": user_input})
    
    try:
        completion = groq_client.chat.completions.create(
            messages=call_sessions[caller_number],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )
        res = json.loads(completion.choices[0].message.content)
        call_sessions[caller_number].append({"role": "assistant", "content": res['reply']})
        
        # Database Update
        extracted = res.get("data", {})
        if extracted.get("name") != "null":
            db.bookings.update_one(
                {"contact": caller_number},
                {"$set": {**extracted, "status": "Confirmed" if res.get("is_complete") else "In-Progress"}},
                upsert=True
            )
        return res
    except:
        return {"reply": "I'm sorry, I'm having trouble connecting. Could you repeat that?", "is_complete": False}

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home_page():
    with open("home.html") as f: return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("index.html") as f: return f.read()

@app.post("/voice")
async def voice_start(request: Request):
    # Clear session on new call
    form_data = await request.form()
    caller = form_data.get("From", "unknown")
    call_sessions[caller] = [get_system_prompt()]
    
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()
    msg = "Welcome to The Velvet Bean. I'm Jessica. How can I help you today?"
    
    # Generate unique filename to force Twilio to fetch fresh audio
    fid = f"start_{uuid.uuid4().hex[:8]}"
    if generate_audio(msg, fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        response.say(msg, voice='Polly.Aditi')
    
    # Enhanced Gather for better speech recognition
    gather = Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='1.2', enhanced=True)
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, SpeechResult: str = Form(None), From: str = Form(None)):
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    if not SpeechResult:
        response.say("I'm sorry, I missed that. What were the details?", voice='Polly.Aditi')
        response.append(Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='1.2'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    ai_decision = get_ai_response(SpeechResult, From)
    fid = f"reply_{uuid.uuid4().hex[:8]}"
    
    if generate_audio(ai_decision['reply'], fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        response.say(ai_decision['reply'], voice='Polly.Aditi')

    if not ai_decision.get("is_complete"):
        response.append(Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='1.2'))
    else:
        # Cleanup session on completion
        call_sessions.pop(From, None)
        response.hangup()
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.get("/api/bookings")
async def get_bookings():
    bookings = list(db.bookings.find({}).sort("_id", -1))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))