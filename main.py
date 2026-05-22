import os
import json
import time
from datetime import datetime
from fastapi import FastAPI, Form, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from pymongo import MongoClient
from bson import ObjectId
from groq import Groq 
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()
app = FastAPI()
security = HTTPBasic()

# --- CONNECTIONS ---
mongo_client = MongoClient(os.getenv("MONGO_URI"))
db = mongo_client["RestaurantDB"] 
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

# --- GOOGLE SHEETS SETUP ---
try:
    scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']
    # Looking for the file you just renamed
    creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_key("1eJXw0uQrqQRuWSBvUltN0Rq1pTpIGWtYr1K8AtTOqno").sheet1
except Exception as e:
    print(f"⚠️ Sheets Error: {e}")

# --- ADMIN AUTH (Username: Aditya | Pass: 092005) ---
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != "Aditya" or credentials.password != "092005":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- JESSICA AI PROMPT ---
SYSTEM_PROMPT = f"""You are Jessica, the host at 'The Velvet Bean'. 
TODAY: {datetime.now().strftime('%A, %d %B %Y')}
GOAL: Collect Name, Date, Time, and Number of Guests. 
STYLE: Warm and human. If they give a detail, acknowledge it (e.g. 'Table for 4? Perfect!').
JSON ONLY: {{"reply": "text", "is_complete": false, "data": {{"name": "null", "date": "null", "time": "null", "guests": "null"}}}}"""

# --- CORE LOGIC ---
def sync_to_sheets(data):
    try:
        row = [datetime.now().strftime("%Y-%m-%d %H:%M"), data['name'], data['date'], data['time'], data['guests'], data['contact']]
        sheet.append_row(row)
    except Exception as e: print(f"❌ Sheets Fail: {e}")

def get_ai_response(user_input, caller_number):
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_input}]
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.3-70b-versatile", response_format={"type": "json_object"})
        res = json.loads(completion.choices[0].message.content)
        ext = res.get("data", {})
        if ext.get("name") != "null":
            booking = {
                "name": ext.get("name"), "date": ext.get("date"), "time": ext.get("time"), 
                "guests": ext.get("guests"), "contact": caller_number,
                "status": "Confirmed" if res.get("is_complete") else "In-Progress",
                "timestamp": datetime.now()
            }
            db.bookings.update_one({"contact": caller_number}, {"$set": booking}, upsert=True)
            if res.get("is_complete"): 
                sync_to_sheets(booking)
                twilio_client.messages.create(from_=os.getenv("TWILIO_PHONE_NUMBER"), to=caller_number, body=f"Confirmed! Table for {ext['guests']} on {ext['date']} at {ext['time']}. See you soon!")
        return res
    except: return {"reply": "Sorry, could you repeat that?", "is_complete": False}

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def home():
    with open("home.html") as f: return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin(username: str = Depends(authenticate)):
    with open("index.html") as f: return f.read()

@app.get("/api/bookings")
async def get_bookings():
    # Returns 5 closest entries
    bookings = list(db.bookings.find({"status": "Confirmed"}).sort("timestamp", -1).limit(5))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

# --- VOICE LOGIC ---
def gen_audio(text, fname):
    try:
        audio = el_client.text_to_speech.convert(voice_id="cgSgspJ2msm6clMCkdW9", text=text, model_id="eleven_turbo_v2_5")
        if not os.path.exists("static"): os.makedirs("static")
        with open(f"static/{fname}.mp3", "wb") as f:
            for chunk in audio: f.write(chunk)
        return True
    except: return False

@app.post("/voice")
async def voice_start(request: Request):
    response = VoiceResponse()
    greeting = "The Velvet Bean, Jessica speaking! How can I help you today?"
    gen_audio(greeting, "greeting")
    response.play(f"{str(request.base_url)}static/greeting.mp3")
    response.append(Gather(input='speech', action=f"{str(request.base_url)}respond", language='en-IN', speech_timeout='0.8'))
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_voice(request: Request, SpeechResult: str = Form(...), From: str = Form(...)):
    ai = get_ai_response(SpeechResult, From)
    fname = f"r_{int(time.time())}"
    res = VoiceResponse()
    if gen_audio(ai['reply'], fname): res.play(f"{str(request.base_url)}static/{fname}.mp3")
    if not ai.get("is_complete"): res.append(Gather(input='speech', action=f"{str(request.base_url)}respond", language='en-IN', speech_timeout='0.8'))
    return HTMLResponse(content=str(res), media_type="application/xml")

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)