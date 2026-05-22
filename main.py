import os
import json
import time
from datetime import datetime
from fastapi import FastAPI, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from pymongo import MongoClient
from bson import ObjectId
from groq import Groq 
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# --- CONNECTIONS ---
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["RestaurantDB"] 
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

# --- AI SYSTEM PROMPT ---
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
    except Exception as e: 
        print(f"❌ SMS Fail: {e}")

# --- AI RESPONSE LOGIC ---
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
    except: 
        return {"reply": "Sorry, I missed that.", "is_complete": False}

def generate_audio(text, filename):
    try:
        audio_generator = el_client.text_to_speech.convert(
            voice_id="cgSgspJ2msm6clMCkdW9", 
            text=text,
            model_id="eleven_turbo_v2_5"
        )
        # Ensure static folder exists
        if not os.path.exists("static"):
            os.makedirs("static")
        with open(f"static/{filename}.mp3", "wb") as f:
            for chunk in audio_generator: f.write(chunk)
        return True
    except: 
        return False

# --- WEB & ADMIN ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home_page():
    """Serves your Bistro Website (About, Menu, etc.)"""
    try:
        with open("home.html") as f: 
            return f.read()
    except FileNotFoundError:
        return "<h1>home.html not found!</h1><p>Please ensure your bistro page is named home.html</p>"

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serves your Command Center dashboard"""
    try:
        with open("index.html") as f: 
            return f.read()
    except FileNotFoundError:
        return "<h1>index.html not found!</h1><p>Please ensure your dashboard page is named index.html</p>"

# --- BOOKING API ---
@app.get("/api/bookings")
async def get_bookings():
    bookings = list(db.bookings.find({}).sort("_id", -1))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

@app.patch("/api/bookings/{id}")
async def update_booking(id: str, data: dict):
    db.bookings.update_one({"_id": ObjectId(id)}, {"$set": data})
    return {"status": "updated"}

@app.delete("/api/bookings/{id}")
async def delete_booking(id: str):
    booking = db.bookings.find_one({"_id": ObjectId(id)})
    if booking:
        send_sms(booking['contact'], f"Hi {booking['name']}, your reservation at The Velvet Bean has been cancelled.")
        db.bookings.delete_one({"_id": ObjectId(id)})
        return {"status": "deleted"}
    return {"status": "not found"}

# --- MENU API ---
@app.get("/api/menu")
async def get_menu():
    menu = list(db.menu.find({}))
    for m in menu: m["_id"] = str(m["_id"])
    return menu

@app.post("/api/menu")
async def add_menu_item(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return HTMLResponse("<script>window.location='/admin'</script>")

@app.delete("/api/menu/{id}")
async def delete_menu_item(id: str):
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "deleted"}

# --- VOICE ROUTES ---
@app.post("/voice")
async def voice_start(request: Request):
    global chat_history
    chat_history = chat_history[:1] # Reset history for new call
    response = VoiceResponse()
    greeting = "Hi! Welcome to The Velvet Bean. I'm Jessica. How can I help?"
    generate_audio(greeting, "greeting")
    response.play(f"{str(request.base_url)}static/greeting.mp3")
    response.append(Gather(input='speech', action=f"{str(request.base_url)}respond", language='en-IN', speech_timeout='0.8'))
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, SpeechResult: str = Form(...), From: str = Form(...)):
    ai_decision = get_ai_response(SpeechResult, From)
    filename = f"reply_{int(time.time())}"
    response = VoiceResponse()
    if generate_audio(ai_decision['reply'], filename):
        response.play(f"{str(request.base_url)}static/{filename}.mp3")
    if not ai_decision.get("is_complete"):
        response.append(Gather(input='speech', action=f"{str(request.base_url)}respond", language='en-IN', speech_timeout='0.8'))
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)