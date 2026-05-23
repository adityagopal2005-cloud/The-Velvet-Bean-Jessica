import os
import json
import uuid
from datetime import datetime
from fastapi import FastAPI, Form, Request, Body
from fastapi.responses import HTMLResponse, FileResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from pymongo import MongoClient
from groq import Groq 
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()
app = FastAPI()

# --- RESOURCES ---
try:
    mongo_client = MongoClient(os.getenv("MONGO_URI"))
    db = mongo_client["RestaurantDB"] 
    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    
    # Google Sheets Initialization
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    sheets_client = gspread.authorize(creds)
    # Ensure you have a sheet named 'Velvet Bean Reservations' shared with your client_email
    sheet = sheets_client.open("Velvet Bean Reservations").get_worksheet(0)
except Exception as e:
    print(f"Startup Warning (Check API Keys/Sheet Name): {e}")

call_sessions = {}

# --- GOOGLE SHEETS SYNC LOGIC ---
def sync_to_sheets(data):
    try:
        sheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            data.get("name", "N/A"),
            data.get("date", "N/A"),
            data.get("time", "N/A"),
            data.get("guests", "N/A"),
            data.get("contact", "N/A"),
            data.get("status", "Talking")
        ])
    except Exception as e:
        print(f"Sync Error: {e}")

# --- MENU SEEDING (10 Gourmet Items) ---
def seed_menu():
    if db.menu.count_documents({}) == 0:
        items = [
            {"name": "Truffle Mushroom Risotto", "price": "₹1,250", "photo": "https://images.unsplash.com/photo-1476124369491-e7addf5db371?q=80&w=800"},
            {"name": "Saffron Sea Bass", "price": "₹2,100", "photo": "https://images.unsplash.com/photo-1519708227418-c8fd9a32b7a2?q=80&w=800"},
            {"name": "Aged Wagyu Sliders", "price": "₹1,850", "photo": "https://images.unsplash.com/photo-1550317138-10000687ad32?q=80&w=800"},
            {"name": "Burrata & Heirloom Tomato", "price": "₹950", "photo": "https://images.unsplash.com/photo-1592417817098-8fd3d9eb14a5?q=80&w=800"},
            {"name": "Smoked Octopus Tentacles", "price": "₹1,600", "photo": "https://images.unsplash.com/photo-1590577976322-3d2d6e2130ee?q=80&w=800"},
            {"name": "Velvet Martini (Signature)", "price": "₹850", "photo": "https://images.unsplash.com/photo-1574096079513-d8259312b785?q=80&w=800"},
            {"name": "Charred Asparagus & Feta", "price": "₹750", "photo": "https://images.unsplash.com/photo-1515412612224-48607c36caec?q=80&w=800"},
            {"name": "Deconstructed Tiramisu", "price": "₹650", "photo": "https://images.unsplash.com/photo-1571877227200-a0d98ea607e9?q=80&w=800"},
            {"name": "Himalayan Salt Tart", "price": "₹550", "photo": "https://images.unsplash.com/photo-1519915028121-7d3463d20b13?q=80&w=800"},
            {"name": "Espresso Old Fashioned", "price": "₹900", "photo": "https://images.unsplash.com/photo-1470337458703-46ad1756a187?q=80&w=800"}
        ]
        db.menu.insert_many(items)

seed_menu()

# --- VOICE LOGIC ---
def get_system_prompt():
    return {
        "role": "system", 
        "content": f"""You are Jessica, the concierge at 'The Velvet Bean'. 
        DATE: {datetime.now().strftime('%A, %d %B %Y')}
        COLLECT: Name, Date, Time, Pax. 
        REPLY JSON: {{"reply": "string", "is_complete": bool, "data": {{"name": "str", "date": "str", "time": "str", "guests": "str"}}}}"""
    }

def generate_audio(text, filename):
    try:
        audio = el_client.text_to_speech.convert(voice_id="cgSgspJ2msm6clMCkdW9", text=text, model_id="eleven_turbo_v2_5")
        with open(f"static/{filename}.mp3", "wb") as f:
            for chunk in audio: f.write(chunk)
        return True
    except: return False

@app.post("/voice")
async def voice_start(request: Request):
    form_data = await request.form()
    caller = form_data.get("From", "unknown")
    session_id = f"sess_{uuid.uuid4().hex[:6]}"
    
    # UNIQUE ENTRY CREATION (Prevents overwriting previous calls)
    db.bookings.insert_one({
        "session_id": session_id, "contact": caller, "status": "Talking...", "created_at": datetime.now()
    })
    
    call_sessions[session_id] = [get_system_prompt()]
    response = VoiceResponse()
    msg = "Welcome to The Velvet Bean. I'm Jessica. How can I help you today?"
    fid = f"hi_{session_id}"
    if generate_audio(msg, fid): response.play(f"/static/{fid}.mp3")
    else: response.say(msg, voice='Polly.Aditi')
    
    gather = Gather(input='speech', action=f"/respond?sid={session_id}", language='en-IN', speech_timeout='1.2')
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, sid: str, SpeechResult: str = Form(None), From: str = Form(None)):
    if not SpeechResult:
        res = VoiceResponse()
        res.append(Gather(input='speech', action=f"/respond?sid={sid}", language='en-IN'))
        return HTMLResponse(content=str(res), media_type="application/xml")

    # AI Logic
    call_sessions[sid].append({"role": "user", "content": SpeechResult})
    completion = groq_client.chat.completions.create(
        messages=call_sessions[sid], model="llama-3.3-70b-versatile", response_format={"type": "json_object"}
    )
    ai_res = json.loads(completion.choices[0].message.content)
    
    # Update unique entry
    data = ai_res.get("data", {})
    status = "Confirmed" if ai_res.get("is_complete") else "Talking..."
    db.bookings.update_one({"session_id": sid}, {"$set": {**data, "status": status}})
    
    if ai_res.get("is_complete"):
        sync_to_sheets({**data, "contact": From, "status": "Confirmed"})

    response = VoiceResponse()
    fid = f"rep_{uuid.uuid4().hex[:6]}"
    generate_audio(ai_res['reply'], fid)
    response.play(f"/static/{fid}.mp3")
    
    if not ai_res.get("is_complete"):
        response.append(Gather(input='speech', action=f"/respond?sid={sid}", language='en-IN'))
    else:
        response.hangup()
    return HTMLResponse(content=str(response), media_type="application/xml")

# --- API ENDPOINTS ---
@app.get("/")
async def home(): return HTMLResponse(open("home.html").read())

@app.get("/admin")
async def admin(): return HTMLResponse(open("index.html").read())

@app.get("/api/bookings")
async def get_bookings():
    bookings = list(db.bookings.find({}))
    for b in bookings: b["_id"] = str(b["_id"])
    return bookings

@app.delete("/api/bookings/{id}")
async def del_booking(id: str):
    from bson import ObjectId
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

@app.post("/api/sync")
async def sync_all():
    bookings = list(db.bookings.find({"status": "Confirmed"}))
    for b in bookings: sync_to_sheets(b)
    return {"message": f"Successfully pushed {len(bookings)} entries to Google Sheets."}

@app.get("/api/menu")
async def get_menu():
    items = list(db.menu.find({}))
    for i in items: i["_id"] = str(i["_id"])
    return items

@app.post("/api/menu")
async def add_menu(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return HTMLResponse("<script>window.location.href='/admin'</script>")

@app.delete("/api/menu/{id}")
async def del_menu(id: str):
    from bson import ObjectId
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

@app.get("/static/{file}")
async def static(file: str): return FileResponse(f"static/{file}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)