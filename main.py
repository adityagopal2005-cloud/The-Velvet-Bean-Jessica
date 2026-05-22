import os
import json
import time
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
    print(f"CRITICAL: Resource Connection Failed: {e}")

chat_history = []

def get_system_prompt():
    return {
        "role": "system", 
        "content": f"""You are Jessica from 'The Velvet Bean Bistro'. 
        TODAY: {datetime.now().strftime('%A, %d %B %Y')}
        RULES: 1. Date format: '20th May 2026' 2. Identify Weekday.
        JSON ONLY: {{"reply": "...", "is_complete": false, "data": {{"name": "null", "date": "null", "day": "null", "time": "null", "guests": "null"}}}}"""
    }

# --- HELPER FUNCTIONS ---

def generate_audio(text, filename):
    try:
        if not os.getenv("ELEVENLABS_API_KEY"):
            print("❌ ElevenLabs Key Missing")
            return False

        audio_generator = el_client.text_to_speech.convert(
            voice_id="cgSgspJ2msm6clMCkdW9", 
            text=text,
            model_id="eleven_turbo_v2_5"
        )
        
        file_path = f"static/{filename}.mp3"
        with open(file_path, "wb") as f:
            for chunk in audio_generator: f.write(chunk)
        
        print(f"✅ Audio Created: {file_path}")
        return True
    except Exception as e:
        print(f"❌ ElevenLabs Error: {e}")
        return False

def get_ai_response(user_input, caller_number):
    chat_history.append({"role": "user", "content": user_input})
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=chat_history,
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )
        res = json.loads(chat_completion.choices[0].message.content)
        chat_history.append({"role": "assistant", "content": res['reply']})
        
        # Database extraction logic
        extracted = res.get("data", {})
        if extracted.get("name") != "null":
            db.bookings.update_one(
                {"contact": caller_number},
                {"$set": {
                    "name": extracted.get("name"), "date": extracted.get("date"),
                    "day": extracted.get("day"), "time": extracted.get("time"),
                    "guests": extracted.get("guests"), "contact": caller_number,
                    "status": "Confirmed" if res.get("is_complete") else "In-Progress"
                }},
                upsert=True
            )
        return res
    except Exception as e:
        print(f"AI Error: {e}")
        return {"reply": "I'm sorry, I didn't catch that.", "is_complete": False}

# --- WEB ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home_page():
    with open("home.html") as f: return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("index.html") as f: return f.read()

# --- TWILIO AI VOICE LOGIC ---

@app.post("/voice")
async def voice_start(request: Request):
    global chat_history
    chat_history = [get_system_prompt()]
    
    # Corrected base_url logic for Railway
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    
    response = VoiceResponse()
    greeting = "Hi! I'm Jessica from The Velvet Bean. How can I help you today?"
    
    # FIXED: Audio generation happens first, then we use the known filename 'greeting'
    if generate_audio(greeting, "greeting"):
        response.play(f"{base_url}/static/greeting.mp3")
    else:
        response.say(greeting)
    
    gather = Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='0.8')
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, SpeechResult: str = Form(None), From: str = Form(None)):
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    if not SpeechResult:
        response.say("I'm sorry, I didn't hear anything.")
        response.append(Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='0.8'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    ai_decision = get_ai_response(SpeechResult, From)
    # Generate unique filename using timestamp
    filename = f"reply_{int(time.time())}"
    
    if generate_audio(ai_decision['reply'], filename):
        response.play(f"{base_url}/static/{filename}.mp3")
    else:
        response.say(ai_decision['reply'])

    if not ai_decision.get("is_complete"):
        response.append(Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='0.8'))
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

# --- API (Simplified for brevity) ---
@app.get("/api/bookings")
async def get_bookings():
    bookings = list(db.bookings.find({}).sort("_id", -1))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)