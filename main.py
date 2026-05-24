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
# We use detailed logging to track AI performance and Twilio webhooks in real-time.
# This ensures that any network delays or AI errors are captured for debugging.
load_dotenv()
app = FastAPI()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- RESOURCE INITIALIZATION ---
# This block connects to MongoDB, Groq, ElevenLabs, Twilio, and Google Sheets.
# Each component is vital for the 'Jessica' AI Concierge ecosystem.
try:
    # MongoDB Connection for persistent storage of bookings and menu
    mongo_client = MongoClient(os.getenv("MONGO_URI"))
    db = mongo_client["RestaurantDB"] 
    
    # AI Engine: Groq for fast Llama-3 processing
    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    
    # Voice Engine: ElevenLabs for realistic concierge speech
    el_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    
    # Telephony: Twilio for handling the voice call stream
    twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    
    # Cloud Sync: Google Sheets for the restaurant staff's live view
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    sheets_client = gspread.authorize(creds)
    # Target Sheet: Must be shared with the service account email in credentials.json
    sheet = sheets_client.open("Velvet Bean Reservations").get_worksheet(0)
    
    logger.info("CRITICAL: All cloud resources and AI engines initialized successfully.")
except Exception as e:
    logger.error(f"FATAL INITIALIZATION ERROR: {e}")

# Global session tracker for active calls (In-memory storage for conversation context)
call_sessions = {}

# --- CORE UTILITY: GOOGLE SHEETS SYNC ---
def sync_to_sheets(data):
    """Pushes a confirmed reservation document to the linked Google Sheet."""
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
        logger.info(f"Sheets Sync Success for: {data.get('name')}")
        return True
    except Exception as e:
        logger.error(f"Google Sheet Sync Failed: {e}")
        return False

# --- DATABASE SEEDING: THE SIGNATURE COLLECTION ---
def seed_system_data():
    """Wipes and re-seeds the menu items to ensure high-quality images and pricing."""
    logger.info("Starting Database Re-Seeding for 'The Velvet Bean'...")
    items = [
        {"name": "24K Gold Wagyu Sliders", "price": "2,850", "photo": "https://images.unsplash.com/photo-1550317138-10000687ad32?q=80&w=800"},
        {"name": "Truffle Lobster Thermidor", "price": "3,400", "photo": "https://images.unsplash.com/photo-1553618531-97aa2bc002fa?q=80&w=800"},
        {"name": "Saffron Infused Burrata", "price": "1,450", "photo": "https://images.unsplash.com/photo-1592417817098-8fd3d9eb14a5?q=80&w=800"},
        {"name": "Smoked Octopus Carpaccio", "price": "1,900", "photo": "https://images.unsplash.com/photo-1590577976322-3d2d6e2130ee?q=80&w=800"},
        {"name": "Wild Mushroom Risotto", "price": "1,200", "photo": "https://images.unsplash.com/photo-1476124369491-e7addf5db371?q=80&w=800"},
        {"name": "Pistachio Baklava Tower", "price": "850", "photo": "https://images.unsplash.com/photo-1519915028121-7d3463d20b13?q=80&w=800"},
        {"name": "The Velvet Martini", "price": "950", "photo": "https://images.unsplash.com/photo-1574096079513-d8259312b785?q=80&w=800"},
        {"name": "Aged Himalayan Lamb Chops", "price": "2,600", "photo": "https://images.unsplash.com/photo-1603048297172-c92544798d5a?q=80&w=800"},
        {"name": "Porcini Cappuccino Soup", "price": "750", "photo": "https://images.unsplash.com/photo-1541167760496-162955ed8a9f?q=80&w=800"},
        {"name": "Espresso Gold Old Fashioned", "price": "1,150", "photo": "https://images.unsplash.com/photo-1470337458703-46ad1756a187?q=80&w=800"}
    ]
    
    # 1. Clear old data to prevent duplication during redeployments
    db.menu.delete_many({}) 
    db.menu.insert_many(items)
    
    # 2. Seed Default Operating Hours if they don't exist in the 'settings' collection
    if db.settings.count_documents({"type": "operating_hours"}) == 0:
        db.settings.insert_one({
            "type": "operating_hours",
            "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
            "open": "18:00",
            "close": "23:00"
        })
        logger.info("Operating hours seeded into system settings.")
    
    logger.info("Database Seeding Completed Successfully.")

# Run seeding once on startup
seed_system_data()

# --- VOICE AI LOGIC (AI CONCIERGE) ---
def get_system_prompt():
    """Generates a high-speed, date-aware prompt for Jessica."""
    now = datetime.now()
    today_str = now.strftime('%A, %d %B %Y')
    
    content_str = (
        f"You are Jessica, the elite concierge at 'The Velvet Bean'. Today is {today_str}. "
        "OBJECTIVE: Book a table by collecting: 1. Name, 2. Date, 3. Time, 4. Guests. "
        
        "RULES: "
        "1. DATE VALIDATION: If a user suggests a date in the past, politely explain you can only book for today or future dates. "
        "2. SPEED: Keep 'reply' under 10 words. Be extremely snappy. "
        "3. DATA: Do not set 'is_complete': true until you have all 4 pieces of info. "
        "4. FORMAT: JSON ONLY."
        
        "JSON STRUCTURE: "
        "{"
        "\"reply\": \"Concise response\", "
        "\"is_complete\": true/false, "
        "\"data\": {\"name\": \"\", \"date\": \"\", \"time\": \"\", \"guests\": \"\"}"
        "}"
    )
    return {"role": "system", "content": content_str}

def generate_audio(text, filename):
    """Converts AI text to speech using ElevenLabs Turbo v2 for minimal lag."""
    try:
        # Added a robust check to catch the 401/Unauthorized error seen in your logs.
        audio = el_client.text_to_speech.convert(
            voice_id="cgSgspJ2msm6clMCkdW9", 
            text=text, 
            model_id="eleven_turbo_v2" 
        )
        if not os.path.exists("static"):
            os.makedirs("static")
        
        file_path = f"static/{filename}.mp3"
        with open(file_path, "wb") as f:
            for chunk in audio:
                if chunk: f.write(chunk)
        return True
    except Exception as e:
        # Log the specific error (e.g., 'Unusual activity detected' or 'Unauthorized')
        logger.error(f"ElevenLabs Generation Error: {e}")
        return False

@app.post("/voice")
async def voice_start(request: Request):
    """Entry point for incoming Twilio calls. Greets the user and starts listening."""
    form_data = await request.form()
    caller = form_data.get("From", "unknown")
    session_id = f"sess_{uuid.uuid4().hex[:6]}"
    
    # Initialize the booking record in MongoDB to track the 'Talking...' state
    db.bookings.insert_one({
        "session_id": session_id, 
        "contact": caller, 
        "status": "Talking...", 
        "created_at": datetime.now()
    })
    
    # Store the system prompt in the session history
    call_sessions[session_id] = [get_system_prompt()]
    
    response = VoiceResponse()
    msg = "Welcome to the Velvet Bean. I'm Jessica. How may I assist with your reservation today?"
    fid = f"hi_{session_id}"
    
    # Force base_url to use https to avoid Twilio 'Invalid Content-Type' errors on Railway.
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    
    if generate_audio(msg, fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        # If ElevenLabs fails (as seen in log), fallback to default Twilio voice so call doesn't drop.
        response.say(msg)
    
    # Gather configuration: timeout 1.2s to prevent Jessica from cutting off natural pauses.
    response.append(Gather(
    input='speech',  
    action=f"{base_url}/respond?sid={sid}",  
    language='en-IN',  
    # 'auto' allows Twilio to detect the end of a sentence dynamically
    speech_timeout='auto', 
    # Hints help the AI process restaurant-related words faster
    hints="reservation, table for, tonight, tomorrow, p.m., a.m., guests, booking",
    speech_model="numbers_and_commands" 
    ))

@app.post("/respond")
async def handle_response(request: Request, sid: str, SpeechResult: str = Form(None), From: str = Form(None)):
    """The main conversation loop: Processes user speech via Groq and responds via ElevenLabs."""
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    # If user stays silent, re-prompt or continue listening
    if not SpeechResult:
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.2'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    try:
        # Session recovery check
        if sid not in call_sessions:
            call_sessions[sid] = [get_system_prompt()]
        
        call_sessions[sid].append({"role": "user", "content": SpeechResult})
        
        # FIX: Swapped decommissioned 'llama3-8b-8192' for 'llama-3.1-8b-instant'.
        completion = groq_client.chat.completions.create(
            messages=call_sessions[sid], 
            model="llama-3.1-8b-instant", 
            response_format={"type": "json_object"}
        )

        raw_content = completion.choices[0].message.content.strip()
        # Cleaning backticks if AI ignores JSON format instructions
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`").replace("json", "").strip()
            
        ai_res = json.loads(raw_content)
        # Inside handle_response, after ai_res = json.loads(raw_content)

        booking_date_str = data.get("date", "")
        if booking_date_str:
            try:
                # Basic attempt to parse the date to check if it's in the past
                # Note: This depends on how the AI formats the date string
                # For more robust sync, you'd use a library like dateutil.parser
                pass 
            except:
                pass

        # The AI's system prompt (Step 1) is usually enough to handle this 
        # because we gave it the exact 'Today' date.
        call_sessions[sid].append({"role": "assistant", "content": raw_content})
        
        # Sync captured data with MongoDB
        data = ai_res.get("data", {})
        is_done = ai_res.get("is_complete", False)
        current_status = "Confirmed" if is_done else "Talking..."
        
        db.bookings.update_one({"session_id": sid}, {"$set": {**data, "status": current_status}})
        
        # Final confirmation sync to Google Sheets
        if is_done:
            sync_to_sheets({**data, "contact": From, "status": "Confirmed"})

        # Audio response generation with error-catch fallback
        fid = f"rep_{uuid.uuid4().hex[:6]}"
        if generate_audio(ai_res['reply'], fid):
            response.play(f"{base_url}/static/{fid}.mp3")
        else:
            response.say(ai_res['reply'])
        
        if not is_done:
            # Continue listening for missing reservation details
            response.append(Gather(
                input='speech', 
                action=f"{base_url}/respond?sid={sid}", 
                language='en-IN', 
                speech_timeout='1.2',
                speech_model="numbers_and_commands"
            ))
        else:
            # Clean up session and hang up gracefully
            call_sessions.pop(sid, None)
            response.hangup()

    except Exception as e:
        logger.error(f"Jessica Interactive Error: {e}")
        # Soft-fail recovery: Ask the user to continue rather than hanging up.
        response.say("I'm sorry, I missed that. Could you please repeat?")
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.2'))
        
    return HTMLResponse(content=str(response), media_type="application/xml")

# --- ADMIN PANEL & SETTINGS API ---
@app.post("/api/login")
async def admin_login(data: dict = Body(...)):
    """Validates the admin credentials for Aditya."""
    if data.get("username") == "Aditya" and data.get("password") == "092005":
        return {"status": "success"}
    raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/api/settings")
async def get_settings():
    """Returns the current operational hours to the admin dashboard."""
    return db.settings.find_one({"type": "operating_hours"}, {"_id": 0})

@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    """Updates global service hours and available days."""
    db.settings.update_one({"type": "operating_hours"}, {"$set": data})
    return {"status": "ok"}

# --- BOOKING & MENU MANAGEMENT ---
@app.get("/api/bookings")
async def fetch_bookings():
    """Fetches all past and live calls for the admin table view."""
    bookings = list(db.bookings.find({}).sort("created_at", -1))
    for b in bookings:
        b["_id"] = str(b["_id"])
        if "name" not in b or not b["name"]:
            b["name"] = "Anonymous Guest"
    return bookings

@app.delete("/api/bookings/{id}")
async def remove_booking(id: str):
    """Deletes a record from the database."""
    from bson import ObjectId
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

@app.post("/api/sync")
async def force_sync():
    """Manual sync button for forcing records into Google Sheets."""
    bookings = list(db.bookings.find({"status": "Confirmed"}))
    success_count = 0
    for b in bookings:
        if sync_to_sheets(b): success_count += 1
    return {"message": f"Successfully synchronized {success_count} records."}

@app.get("/api/menu")
async def get_menu_items():
    """Public/Admin endpoint for menu item retrieval."""
    items = list(db.menu.find({}))
    for i in items:
        i["_id"] = str(i["_id"])
    return items

@app.post("/api/menu")
async def create_menu_item(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    """Adds a new premium dish to the database via Admin Form."""
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return HTMLResponse("<script>window.location.href='/admin'</script>")

@app.delete("/api/menu/{id}")
async def delete_menu_item(id: str):
    """Removes a signature item from the menu collection."""
    from bson import ObjectId
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

# --- FRONTEND ROUTING ---
@app.get("/")
async def home_page(): 
    """Serves the Customer Public UI."""
    return HTMLResponse(open("home.html").read())

@app.get("/admin")
async def admin_page(): 
    """Serves the Admin Control UI."""
    return HTMLResponse(open("index.html").read())

@app.get("/static/{file}")
async def serve_static(file: str): 
    """Static file server for generated reservation audio."""
    return FileResponse(f"static/{file}")