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
        "content": f"""You are Jessica, the professional concierge at 'The Velvet Bean Bistro'. 
        TODAY: {datetime.now().strftime('%A, %d %B %Y')}
        
        GOAL: Collect Name, Date (include day), Time, and Number of Guests.
        NOTES: Capture ANY special requirements (e.g. window seat, anniversary) in 'notes'.
        
        RULES:
        - DO NOT finish the call until you have: Name, Date, Time, and Guests.
        - You must confirm all details back to the guest at the end.
        - Say a clear 'Goodbye' only when everything is done.
        - Only set 'is_complete' to true AFTER your final goodbye sentence.
        
        JSON STRUCTURE:
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
        print(f"❌ ElevenLabs Failed (Jessica Voice Disabled): {e}")
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
        
        extracted = res.get("data", {})
        if extracted.get("name") != "null" or extracted.get("date") != "null":
            db.bookings.update_one(
                {"contact": caller_number},
                {"$set": {
                    "name": extracted.get("name"),
                    "date": extracted.get("date"),
                    "day": extracted.get("day"),
                    "time": extracted.get("time"),
                    "guests": extracted.get("guests"),
                    "notes": extracted.get("notes"),
                    "contact": caller_number,
                    "status": "Confirmed" if res.get("is_complete") else "In-Progress"
                }},
                upsert=True
            )
        return res
    except Exception as e:
        print(f"AI/DB Error: {e}")
        return {"reply": "I'm sorry, I missed that. Could you repeat it?", "is_complete": False}

# --- TWILIO ROUTES ---

@app.post("/voice")
async def voice_start(request: Request):
    global chat_history
    chat_history = [get_system_prompt()]
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    
    response = VoiceResponse()
    greeting = "Welcome to The Velvet Bean. I'm Jessica. How can I help you with your reservation today?"
    
    if generate_audio(greeting, "greeting"):
        response.play(f"{base_url}/static/greeting.mp3")
    else:
        # Fail-safe voice if ElevenLabs is blocked
        response.say(greeting, voice='Polly.Aditi')
    
    gather = Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='1.5', enhanced=True)
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, SpeechResult: str = Form(None), From: str = Form(None)):
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    # If the user stayed silent
    if not SpeechResult:
        response.say("I'm sorry, I'm still here. Could you give me the details for the booking?")
        response.append(Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='1.5'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    ai_decision = get_ai_response(SpeechResult, From)
    filename = f"reply_{int(time.time())}"
    
    # Try Jessica, fallback to Aditi (Polly) if ElevenLabs blocks us
    if generate_audio(ai_decision['reply'], filename):
        response.play(f"{base_url}/static/{filename}.mp3")
    else:
        response.say(ai_decision['reply'], voice='Polly.Aditi')

    # THE FIX: Only hang up if AI explicitly marks call as finished
    if ai_decision.get("is_complete") is True:
        response.hangup()
    else:
        # Re-attach Gather so Jessica keeps listening
        gather = Gather(input='speech', action=f"{base_url}/respond", language='en-IN', speech_timeout='1.5', enhanced=True)
        response.append(gather)
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("index.html") as f: return f.read()

@app.get("/api/bookings")
async def get_bookings():
    bookings = list(db.bookings.find({}).sort("_id", -1))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)