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

# --- AUTOMATIC MENU SEEDING ---
def seed_menu():
    if db.menu.count_documents({}) == 0:
        items = [
            {"name": "Truffle Mac & Cheese", "price": "$18", "photo": "https://images.unsplash.com/photo-1543339308-43e59d6b73a6?q=80&w=2070"},
            {"name": "Signature Velvet Latte", "price": "$7", "photo": "https://images.unsplash.com/photo-1541167760496-162955ed8a9f?q=80&w=2070"},
            {"name": "Wagyu Beef Sliders", "price": "$24", "photo": "https://images.unsplash.com/photo-1550317138-10000687ad32?q=80&w=2070"},
            {"name": "Avocado Burrata Toast", "price": "$16", "photo": "https://images.unsplash.com/photo-1525351484163-7529414344d8?q=80&w=2070"},
            {"name": "Spicy Tuna Crispy Rice", "price": "$21", "photo": "https://images.unsplash.com/photo-1579584425555-c3ce17fd4351?q=80&w=2070"},
            {"name": "Rooftop Berry Parfait", "price": "$12", "photo": "https://images.unsplash.com/photo-1488477181946-6428a0291777?q=80&w=2070"},
            {"name": "Lobster Ravioli", "price": "$32", "photo": "https://images.unsplash.com/photo-1551183053-bf91a1d81141?q=80&w=2070"},
            {"name": "Golden Saffron Risotto", "price": "$28", "photo": "https://images.unsplash.com/photo-1476124369491-e7addf5db371?q=80&w=2070"},
            {"name": "Artisanal Cheese Board", "price": "$26", "photo": "https://images.unsplash.com/photo-1631452180519-c014fe946bc7?q=80&w=2070"},
            {"name": "Midnight Chocolate Ganache", "price": "$14", "photo": "https://images.unsplash.com/photo-1578985545062-69928b1d9587?q=80&w=2070"}
        ]
        db.menu.insert_many(items)
seed_menu()

# --- GOOGLE SHEETS SETUP ---
try:
    scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_key("1eJXw0uQrqQRuWSBvUltN0Rq1pTpIGWtYr1K8AtTOqno").sheet1
except Exception as e:
    print(f"⚠️ Sheets Error: {e}")

# --- ADMIN AUTH (Username: Aditya | Pass: 092005) ---
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != "Aditya" or credentials.password != "092005":
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- JESSICA AI PROMPT ---
SYSTEM_PROMPT = f"""You are Jessica, the host at 'The Velvet Bean'. Today: {datetime.now().strftime('%A, %d %B %Y')}. Collect Name, Date, Time, and Number of Guests. Output JSON ONLY."""

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
                row = [datetime.now().strftime("%Y-%m-%d %H:%M"), ext['name'], ext['date'], ext['time'], ext['guests'], caller_number]
                sheet.append_row(row)
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
    bookings = list(db.bookings.find().sort("timestamp", -1).limit(5))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

@app.delete("/api/bookings/{id}")
async def delete_booking(id: str):
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "deleted"}

@app.get("/api/menu")
async def get_menu():
    items = list(db.menu.find())
    for i in items: i["_id"] = str(i["_id"])
    return items

@app.post("/api/menu")
async def add_menu(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return {"status": "added"}

@app.delete("/api/menu/{id}")
async def delete_menu(id: str):
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "deleted"}

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
    greeting = "Velvet Bean, Jessica speaking!"
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