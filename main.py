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
    creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_key("1eJXw0uQrqQRuWSBvUltN0Rq1pTpIGWtYr1K8AtTOqno").sheet1
except Exception as e:
    print(f"⚠️ Sheets Sync Error: {e}")

# --- ADMIN AUTHENTICATION ---
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != "Aditya" or credentials.password != "092005":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- HUMANIZED AI PROMPT ---
SYSTEM_PROMPT = f"""You are Jessica, the warm and professional host at 'The Velvet Bean Bistro'. 
TODAY: {datetime.now().strftime('%A, %d %B %Y')}

YOUR GOAL: Collect Name, Date, Time, and Number of Guests. 
PERSONALITY: Very human and charming. Acknowledge details warmly (e.g., 'A table for four sounds perfect!'). 
Don't be robotic. If you have all details, confirm them and say goodbye.

JSON ONLY: {{
  "reply": "your conversational response",
  "is_complete": false,
  "data": {{"name": "null", "date": "null", "time": "null", "guests": "null"}}
}}"""

# --- CORE FUNCTIONS ---

def sync_to_google_sheets(data):
    try:
        row = [datetime.now().strftime("%Y-%m-%d %H:%M"), data['name'], data['date'], data['time'], data['guests'], data['contact']]
        sheet.append_row(row)
    except Exception as e: print(f"❌ Sheet Sync Fail: {e}")

def get_ai_response(user_input, caller_number):
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_input}]
        completion = groq_client.chat.completions.create(
            messages=messages, model="llama-3.3-70b-versatile", response_format={"type": "json_object"}
        )
        res = json.loads(completion.choices[0].message.content)
        extracted = res.get("data", {})
        
        if extracted.get("name") != "null" or extracted.get("time") != "null":
            booking_data = {
                "name": extracted.get("name"),
                "date": extracted.get("date"),
                "time": extracted.get("time"),
                "guests": extracted.get("guests"),
                "contact": caller_number,
                "status": "Confirmed" if res.get("is_complete") else "In-Progress",
                "timestamp": datetime.now()
            }
            db.bookings.update_one({"contact": caller_number}, {"$set": booking_data}, upsert=True)
            
            if res.get("is_complete"):
                sync_to_google_sheets(booking_data)
                twilio_client.messages.create(
                    from_=os.getenv("TWILIO_PHONE_NUMBER"), to=caller_number,
                    body=f"Hi {extracted['name']}! Jessica here. Your reservation for {extracted['guests']} on {extracted['date']} at {extracted['time']} is confirmed!"
                )
        return res
    except: return {"reply": "I'm so sorry, I missed that.", "is_complete": False}

# --- WEB & API ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home_page():
    with open("home.html") as f: return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(username: str = Depends(authenticate)):
    with open("index.html") as f: return f.read()

@app.get("/api/bookings")
async def get_bookings():
    # Sort by most recent update and limit to 5
    bookings = list(db.bookings.find({"status": "Confirmed"}).sort("timestamp", -1).limit(5))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

@app.patch("/api/bookings/{id}")
async def update_booking(id: str, data: dict):
    db.bookings.update_one({"_id": ObjectId(id)}, {"$set": data})
    return {"status": "updated"}

@app.delete("/api/bookings/{id}")
async def delete_booking(id: str):
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "deleted"}

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

# --- BISTRO RULES API ---
@app.post("/api/rules")
async def update_rules(data: dict):
    db.rules.update_one({"type": "hours"}, {"$set": data}, upsert=True)
    return {"status": "rules updated"}

# --- VOICE ROUTES ---
def generate_audio(text, filename):
    try:
        audio = el_client.text_to_speech.convert(voice_id="cgSgspJ2msm6clMCkdW9", text=text, model_id="eleven_turbo_v2_5")
        if not os.path.exists("static"): os.makedirs("static")
        with open(f"static/{filename}.mp3", "wb") as f:
            for chunk in audio: f.write(chunk)
        return True
    except: return False

@app.post("/voice")
async def voice_start(request: Request):
    response = VoiceResponse()
    greeting = "The Velvet Bean Bistro, Jessica speaking! How can I help you today?"
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