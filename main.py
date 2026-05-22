import os
import json
import time
from datetime import datetime
from fastapi import FastAPI, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from pymongo import MongoClient
from groq import Groq 
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv

# Load variables from .env or Railway environment
load_dotenv()
app = FastAPI()

# Ensure the 'static' directory exists to prevent crash during audio generation
if not os.path.exists("static"):
    os.makedirs("static")

# CONNECTIONS
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["RestaurantDB"] 
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

# SYSTEM PROMPT
chat_history = [
    {
        "role": "system", 
        "content": f"""You are Jessica from 'The Velvet Bean Bistro'. 
        TODAY: {datetime.now().strftime('%A, %d %B %Y')}
        RULES: 
        1. Date format: '20th May 2026'
        2. Identify the Weekday correctly.
        JSON ONLY: {{"reply": "...", "is_complete": false, "data": {{"name": "null", "date": "null", "day": "null", "time": "null", "guests": "null"}}}}"""
    }
]

def send_sms(to_number, message):
    try:
        twilio_client.messages.create(
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            to=to_number,
            body=message
        )
    except Exception as e: print(f"❌ SMS Fail: {e}")

# AI RESPONSE LOGIC
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
        if extracted.get("name") != "null":
            db.bookings.update_one(
                {"contact": caller_number},
                {"$set": {
                    "name": extracted.get("name"),
                    "date": extracted.get("date"),
                    "day": extracted.get("day"),
                    "time": extracted.get("time"),
                    "guests": extracted.get("guests"),
                    "contact": caller_number,
                    "status": "Confirmed" if res.get("is_complete") else "In-Progress"
                }},
                upsert=True
            )
            if res.get("is_complete"):
                send_sms(caller_number, f"Hi {extracted['name']}! Your booking at The Velvet Bean for {extracted['date']} is confirmed.")
        return res
    except Exception as e:
        print(f"Groq/DB Error: {e}")
        return {"reply": "I'm having a little trouble hearing you. Could you repeat that?", "is_complete": False}

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
        print(f"ElevenLabs Error: {e}")
        return False

# --- FRONTEND ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home_page():
    with open("home.html") as f: 
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("index.html") as f: 
        return f.read()

# --- API ENDPOINTS ---

@app.get("/api/settings")
async def get_settings():
    settings = db.settings.find_one({"type": "bistro_rules"})
    if settings:
        settings["_id"] = str(settings["_id"])
        return settings
    return {"open_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], "open_time": 10, "close_time": 23}

@app.post("/api/settings")
async def save_settings(data: dict):
    db.settings.update_one({"type": "bistro_rules"}, {"$set": data}, upsert=True)
    return {"status": "success"}

@app.get("/api/menu")
async def get_menu():
    menu = list(db.menu.find({}))
    for m in menu: m["_id"] = str(m["_id"])
    return menu

@app.post("/api/menu")
async def add_menu(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return HTMLResponse("<script>window.location.href='/admin'</script>")

@app.delete("/api/menu/{id}")
async def delete_menu(id: str):
    from bson import ObjectId
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "deleted"}

@app.get("/api/bookings")
async def get_bookings():
    bookings = list(db.bookings.find({}).sort("_id", -1))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

@app.patch("/api/bookings/{id}")
async def update_booking(id: str, data: dict):
    from bson import ObjectId
    db.bookings.update_one({"_id": ObjectId(id)}, {"$set": data})
    return {"status": "updated"}

@app.delete("/api/bookings/{id}")
async def delete_booking(id: str):
    from bson import ObjectId
    booking = db.bookings.find_one({"_id": ObjectId(id)})
    if booking:
        send_sms(booking['contact'], f"Hi {booking['name']}, your reservation at The Velvet Bean has been cancelled.")
        db.bookings.delete_one({"_id": ObjectId(id)})
        return {"status": "deleted"}
    return {"status": "not found"}

# --- TWILIO VOICE LOGIC ---

@app.post("/voice")
async def voice_start(request: Request):
    global chat_history
    chat_history = chat_history[:1] # Reset history for new call
    response = VoiceResponse()
    greeting = "Hi! I'm Jessica from The Velvet Bean. How can I help you today?"
    
    # Generate audio for the initial greeting
    generate_audio(greeting, "greeting")
    
    audio_url = f"{str(request.base_url).rstrip('/')}/static/greeting.mp3"
    response.play(audio_url)
    
    # Listen for user input
    gather = Gather(input='speech', action=f"{str(request.base_url).rstrip('/')}/respond", language='en-IN', speech_timeout='0.8')
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, SpeechResult: str = Form(None), From: str = Form(...)):
    if not SpeechResult:
        response = VoiceResponse()
        response.say("I'm sorry, I didn't catch that. Could you repeat it?")
        response.append(Gather(input='speech', action=f"{str(request.base_url).rstrip('/')}/respond", language='en-IN', speech_timeout='0.8'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    ai_decision = get_ai_response(SpeechResult, From)
    filename = f"reply_{int(time.time())}"
    
    response = VoiceResponse()
    if generate_audio(ai_decision['reply'], filename):
        audio_url = f"{str(request.base_url).rstrip('/')}/static/{filename}.mp3"
        response.play(audio_url)
    else:
        # Fallback to robotic voice if ElevenLabs fails
        response.say(ai_decision['reply'])

    if not ai_decision.get("is_complete"):
        response.append(Gather(input='speech', action=f"{str(request.base_url).rstrip('/')}/respond", language='en-IN', speech_timeout='0.8'))
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)