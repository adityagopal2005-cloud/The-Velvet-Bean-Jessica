import os
import json
import uuid
import logging
from datetime import datetime
from fastapi import FastAPI, Form, Request, Body, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient
from pymongo import MongoClient
from groq import Groq 
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURATION & LOGGING ---
load_dotenv()
app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- RESOURCE INITIALIZATION ---
# We initialize all external connections here. If any fail, the server logs a warning.
try:
    # MongoDB Connection
    mongo_client = MongoClient(os.getenv("MONGO_URI"))
    db = mongo_client["RestaurantDB"] 
    
    # AI & Voice Engines
    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    
    # Twilio Telephony
    twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    
    # Google Sheets Integration (Retained)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    sheets_client = gspread.authorize(creds)
    # Ensure your Google Sheet is named 'Velvet Bean Reservations'
    sheet = sheets_client.open("Velvet Bean Reservations").get_worksheet(0)
    logger.info("All resources initialized successfully.")
except Exception as e:
    logger.error(f"Initialization Warning: {e}")

# Global session tracker for active calls
call_sessions = {}

# --- CORE UTILITY: GOOGLE SHEETS SYNC ---
def sync_to_sheets(data):
    """Pushes a confirmed reservation to the linked Google Sheet."""
    try:
        sheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            data.get("name", "N/A"),
            data.get("date", "N/A"),
            data.get("time", "N/A"),
            data.get("guests", "N/A"),
            data.get("contact", "N/A"),
            data.get("status", "Confirmed")
        ])
        return True
    except Exception as e:
        logger.error(f"Google Sheet Sync Failed: {e}")
        return False

# --- DATABASE SEEDING: THE SIGNATURE 10 ---
def seed_system_data():
    # 1. NEW PREMIUM DATA
    items = [
        {"name": "24K Gold Wagyu Sliders", "price": "₹2,850", "photo": "https://images.unsplash.com/photo-1550317138-10000687ad32?q=80&w=800"},
        {"name": "Truffle Lobster Thermidor", "price": "₹3,400", "photo": "https://images.unsplash.com/photo-1553618531-97aa2bc002fa?q=80&w=800"},
        {"name": "Saffron Infused Burrata", "price": "₹1,450", "photo": "https://images.unsplash.com/photo-1592417817098-8fd3d9eb14a5?q=80&w=800"},
        {"name": "Smoked Octopus Carpaccio", "price": "₹1,900", "photo": "https://images.unsplash.com/photo-1590577976322-3d2d6e2130ee?q=80&w=800"},
        {"name": "Wild Mushroom Risotto", "price": "₹1,200", "photo": "https://images.unsplash.com/photo-1476124369491-e7addf5db371?q=80&w=800"},
        {"name": "Pistachio Baklava Tower", "price": "₹850", "photo": "https://images.unsplash.com/photo-1519915028121-7d3463d20b13?q=80&w=800"},
        {"name": "The Velvet Martini", "price": "₹950", "photo": "https://images.unsplash.com/photo-1574096079513-d8259312b785?q=80&w=800"},
        {"name": "Aged Himalayan Lamb Chops", "price": "₹2,600", "photo": "https://images.unsplash.com/photo-1603048297172-c92544798d5a?q=80&w=800"},
        {"name": "Porcini Cappuccino Soup", "price": "₹750", "photo": "https://images.unsplash.com/photo-1541167760496-162955ed8a9f?q=80&w=800"},
        {"name": "Espresso Gold Old Fashioned", "price": "₹1,150", "photo": "https://images.unsplash.com/photo-1470337458703-46ad1756a187?q=80&w=800"}
    ]
    
    # 2. THE FORCE: Clear old items first so new links take effect
    db.menu.delete_many({}) 
    db.menu.insert_many(items)
    print("Database re-seeded with premium images.")

    # Seed Default Operating Hours
    if db.settings.count_documents({"type": "operating_hours"}) == 0:
        db.settings.insert_one({
            "type": "operating_hours",
            "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
            "open": "18:00",
            "close": "23:00"
        })
        logger.info("Operating hours seeded.")

seed_system_data()


# --- VOICE AI LOGIC (AI CONCIERGE) ---
def get_system_prompt():
    content_str = (
        f"You are Jessica, concierge at 'The Velvet Bean'. Today: {datetime.now().strftime('%A, %d %B %Y')}. "
        "Collect: Name, Date, Time, and Guests. "
        "CRITICAL: You must ONLY respond with a valid JSON object. No prose before or after the JSON."
        "Format: {\"reply\": \"your speech\", \"is_complete\": false, \"data\": {\"name\": \"\", \"date\": \"\", \"time\": \"\", \"guests\": \"\"}}"
    )
    return {"role": "system", "content": content_str}

def generate_audio(text, filename):
    """Converts text to high-quality audio via ElevenLabs."""
    try:
        audio = el_client.text_to_speech.convert(
            voice_id="cgSgspJ2msm6clMCkdW9", 
            text=text, 
            model_id="eleven_turbo_v2" # FAST MODEL FOR LOW LATENCY
        )
        # Ensure the directory exists right before saving
        if not os.path.exists("static"):
            os.makedirs("static")
        with open(f"static/{filename}.mp3", "wb") as f:
            for chunk in audio: f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Audio Generation Error: {e}")
        return False

@app.post("/voice")
async def voice_start(request: Request):
    """Initial entry point for incoming Twilio calls."""
    form_data = await request.form()
    caller = form_data.get("From", "unknown")
    session_id = f"sess_{uuid.uuid4().hex[:6]}"
    
    # Log the conversation start in the DB
    db.bookings.insert_one({
        "session_id": session_id, "contact": caller, "status": "Talking...", "created_at": datetime.now()
    })
    
    # FIX: We now call get_system_prompt() to ensure it's a string, not a function reference
    call_sessions[session_id] = [get_system_prompt()]
    
    response = VoiceResponse()
    msg = "Welcome to the Velvet Bean. I'm Jessica. How may I assist with your reservation?"
    fid = f"hi_{session_id}"
    
    # Always include the base URL for Railway compatibility
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    
    if generate_audio(msg, fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        response.say(msg)
    
    # IMPROVED GATHER: Added barge-in protection and faster timeout
    response.append(Gather(
        input='speech', 
        action=f"{base_url}/respond?sid={session_id}", 
        language='en-IN', 
        speech_timeout='1.0',
        hints="reservation, velvet bean, table for two",
        speech_model="numbers_and_commands"
    ))
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, sid: str, SpeechResult: str = Form(None), From: str = Form(None)):
    """Handles the back-and-forth conversation with the AI."""
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    if not SpeechResult:
        response.say("I'm sorry, I missed that. Could you please repeat the details?")
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.0'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    try:
        # Add user input to history
        if sid not in call_sessions:
            call_sessions[sid] = [get_system_prompt()]
        
        call_sessions[sid].append({"role": "user", "content": SpeechResult})
        
        # Process with Groq - USING 8B MODEL FOR INSTANT RESPONSES
        completion = groq_client.chat.completions.create(
            messages=call_sessions[sid], 
            model="llama3-8b-8192", 
            response_format={"type": "json_object"}
        )

        raw_content = completion.choices[0].message.content.strip()
        if raw_content.startswith("```json"):
            raw_content = raw_content.replace("```json", "").replace("```", "").strip()
        ai_res = json.loads(raw_content)
        
        # Track AI response in history
        call_sessions[sid].append({"role": "assistant", "content": completion.choices[0].message.content})
        
        data = ai_res.get("data", {})
        is_done = ai_res.get("is_complete", False)
        status = "Confirmed" if is_done else "Talking..."
        
        # Sync to DB
        db.bookings.update_one({"session_id": sid}, {"$set": {**data, "status": status}})
        
        # Sync to Sheets if finished
        if is_done:
            sync_to_sheets({**data, "contact": From, "status": "Confirmed"})

        fid = f"rep_{uuid.uuid4().hex[:6]}"
        if generate_audio(ai_res['reply'], fid):
            response.play(f"{base_url}/static/{fid}.mp3")
        else:
            response.say(ai_res['reply'])
        
        if not is_done:
            # CONTINUE GATHER WITH SAME FAST SETTINGS
            response.append(Gather(
                input='speech', 
                action=f"{base_url}/respond?sid={sid}", 
                language='en-IN', 
                speech_timeout='1.0',
                speech_model="numbers_and_commands"
            ))
        else:
            # Cleanup session to save memory
            call_sessions.pop(sid, None)
            response.hangup()

    except Exception as e:
        logger.error(f"Jessica Response Error: {e}")
        response.say("I apologize, but my connection was interrupted. Please tell me again?")
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.0'))
        
    return HTMLResponse(content=str(response), media_type="application/xml")

# --- AUTHENTICATION & SETTINGS API ---
@app.post("/api/login")
async def admin_login(data: dict = Body(...)):
    """Validates the admin credentials (Aditya / 092005)."""
    if data.get("username") == "Aditya" and data.get("password") == "092005":
        return {"status": "success"}
    raise HTTPException(status_code=401, detail="Unauthorized Access")

@app.get("/api/settings")
async def get_settings():
    """Fetches opening hours and open days for the frontend."""
    return db.settings.find_one({"type": "operating_hours"}, {"_id": 0})

@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    # This ensures the dictionary from the frontend is saved directly to MongoDB
    db.settings.update_one({"type": "operating_hours"}, {"$set": data})
    return {"status": "ok"}

# --- MENU & BOOKING MANAGEMENT ---
@app.get("/")
async def home_page(): 
    return HTMLResponse(open("home.html").read())

@app.get("/admin")
async def admin_page(): 
    return HTMLResponse(open("index.html").read())

@app.get("/api/bookings")
async def fetch_bookings():
    # 1. Sort by 'created_at' so the newest calls appear at the top
    bookings = list(db.bookings.find({}).sort("created_at", -1))
    
    for b in bookings:
        b["_id"] = str(b["_id"])
        # 2. Add a fallback 'Anonymous' name if the AI hasn't collected it yet
        # This prevents the Admin table from appearing blank
        if "name" not in b: 
            b["name"] = "Anonymous"
            
    return bookings

@app.delete("/api/bookings/{id}")
async def remove_booking(id: str):
    from bson import ObjectId
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

@app.post("/api/sync")
async def force_sync():
    """Manual trigger to push all confirmed bookings to Sheets."""
    bookings = list(db.bookings.find({"status": "Confirmed"}))
    for b in bookings: sync_to_sheets(b)
    return {"message": f"Synchronized {len(bookings)} bookings to Cloud Sheets."}

@app.get("/api/menu")
async def get_menu_items():
    items = list(db.menu.find({}))
    for i in items:
        i["_id"] = str(i["_id"])
    return items

@app.post("/api/menu")
async def create_menu_item(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return HTMLResponse("<script>window.location.href='/admin'</script>")

@app.delete("/api/menu/{id}")
async def delete_menu_item(id: str):
    from bson import ObjectId
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

@app.get("/static/{file}")
async def serve_static(file: str): 
    return FileResponse(f"static/{file}")

# --- SERVER START ---
if __name__ == "__main__":
    import uvicorn
    # Add these lines to ensure the server doesn't crash on the first call
    if not os.path.exists("static"):
        os.makedirs("static")
    uvicorn.run(app, host="0.0.0.0", port=8000)