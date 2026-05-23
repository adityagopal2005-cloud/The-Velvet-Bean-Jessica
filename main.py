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
load_dotenv()
app = FastAPI()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- RESOURCE INITIALIZATION ---
# This block connects to MongoDB, Groq, ElevenLabs, Twilio, and Google Sheets.
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
    logger.info("Starting Database Re-Seeding...")
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
    
    # Reset Menu Collection
    db.menu.delete_many({}) 
    db.menu.insert_many(items)
    
    # Seed Default Operating Hours if they don't exist
    if db.settings.count_documents({"type": "operating_hours"}) == 0:
        db.settings.insert_one({
            "type": "operating_hours",
            "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
            "open": "18:00",
            "close": "23:00"
        })
        logger.info("Operating hours seeded into system settings.")

seed_system_data()

# --- VOICE AI LOGIC (AI CONCIERGE) ---
def get_system_prompt():
    """Generates the fresh system prompt for Jessica with strict JSON constraints."""
    content_str = (
        "You are Jessica, the professional and elegant concierge at 'The Velvet Bean' restaurant. "
        f"Today is {datetime.now().strftime('%A, %d %B %Y')}. "
        "Your primary objective is to book a table by collecting four pieces of information: "
        "1. Guest Name, 2. Date of reservation, 3. Time of arrival, 4. Number of guests. "
        "Guidelines: Keep your spoken 'reply' warm but concise (max 15 words). "
        "Respond ONLY in the following JSON format. Do not include any text, backticks, or explanations outside the JSON."
        "{"
        "\"reply\": \"Your conversational response here\", "
        "\"is_complete\": true or false, "
        "\"data\": {\"name\": \"\", \"date\": \"\", \"time\": \"\", \"guests\": \"\"}"
        "}"
    )
    return {"role": "system", "content": content_str}

def generate_audio(text, filename):
    """Converts AI text to speech using ElevenLabs Turbo v2 for minimal lag."""
    try:
        # We use the Turbo model specifically to keep the Twilio webhook response under 10 seconds.
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
        logger.error(f"ElevenLabs Generation Error: {e}")
        return False

@app.post("/voice")
async def voice_start(request: Request):
    """Entry point for incoming calls. Greets the user and starts listening."""
    form_data = await request.form()
    caller = form_data.get("From", "unknown")
    session_id = f"sess_{uuid.uuid4().hex[:6]}"
    
    # Initialize the booking record in MongoDB
    db.bookings.insert_one({
        "session_id": session_id, 
        "contact": caller, 
        "status": "Talking...", 
        "created_at": datetime.now()
    })
    
    # Initialize conversation history
    call_sessions[session_id] = [get_system_prompt()]
    
    response = VoiceResponse()
    msg = "Welcome to the Velvet Bean. I'm Jessica. How may I assist with your reservation today?"
    fid = f"hi_{session_id}"
    
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    
    if generate_audio(msg, fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        response.say(msg)
    
    # Gather configuration: timeout 1.2s to prevent Jessica from cutting off natural pauses.
    response.append(Gather(
        input='speech', 
        action=f"{base_url}/respond?sid={session_id}", 
        language='en-IN', 
        speech_timeout='1.2',
        speech_model="numbers_and_commands"
    ))
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, sid: str, SpeechResult: str = Form(None), From: str = Form(None)):
    """The main loop: Processes user speech via Groq and responds with ElevenLabs audio."""
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    # Fallback if no speech was detected
    if not SpeechResult:
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.2'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    try:
        # Retrieve session context
        if sid not in call_sessions:
            call_sessions[sid] = [get_system_prompt()]
        
        call_sessions[sid].append({"role": "user", "content": SpeechResult})
        
        # Groq Llama-3-8B is used here for ultra-fast JSON generation
        completion = groq_client.chat.completions.create(
            messages=call_sessions[sid], 
            model="llama3-8b-8192", 
            response_format={"type": "json_object"}
        )

        raw_content = completion.choices[0].message.content.strip()
        # Cleaning AI content in case it wrapped JSON in markdown backticks
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`").replace("json", "").strip()
            
        ai_res = json.loads(raw_content)
        call_sessions[sid].append({"role": "assistant", "content": raw_content})
        
        # Extract reservation details
        data = ai_res.get("data", {})
        is_done = ai_res.get("is_complete", False)
        current_status = "Confirmed" if is_done else "Talking..."
        
        # Update Database with latest extraction
        db.bookings.update_one({"session_id": sid}, {"$set": {**data, "status": current_status}})
        
        # If reservation is complete, trigger the Sheets sync
        if is_done:
            sync_to_sheets({**data, "contact": From, "status": "Confirmed"})

        # Generate the audio response
        fid = f"rep_{uuid.uuid4().hex[:6]}"
        if generate_audio(ai_res['reply'], fid):
            response.play(f"{base_url}/static/{fid}.mp3")
        else:
            # Emergency fallback to standard Twilio voice to prevent call drop
            response.say(ai_res['reply'])
        
        if not is_done:
            response.append(Gather(
                input='speech', 
                action=f"{base_url}/respond?sid={sid}", 
                language='en-IN', 
                speech_timeout='1.2',
                speech_model="numbers_and_commands"
            ))
        else:
            # End the call gracefully once booking is confirmed
            call_sessions.pop(sid, None)
            response.hangup()

    except Exception as e:
        logger.error(f"Jessica Interactive Error: {e}")
        # Soft recovery: Jessica asks the user to repeat rather than hanging up
        response.say("I'm sorry, I had a bit of trouble processing that. Could you repeat the last detail?")
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.2'))
        
    return HTMLResponse(content=str(response), media_type="application/xml")

# --- ADMIN PANEL & SETTINGS API ---
@app.post("/api/login")
async def admin_login(data: dict = Body(...)):
    """Handles secure access for the restaurant owner (Aditya)."""
    if data.get("username") == "Aditya" and data.get("password") == "092005":
        return {"status": "success"}
    raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/api/settings")
async def get_settings():
    """Returns the current operating schedule."""
    return db.settings.find_one({"type": "operating_hours"}, {"_id": 0})

@app.post("/api/settings")
async def update_settings(data: dict = Body(...)):
    """Updates the operating days and hours from the Admin Panel."""
    db.settings.update_one({"type": "operating_hours"}, {"$set": data})
    return {"status": "ok"}

# --- BOOKING & MENU MANAGEMENT ENDPOINTS ---
@app.get("/api/bookings")
async def fetch_bookings():
    """Returns all call logs and reservations, sorted by most recent."""
    bookings = list(db.bookings.find({}).sort("created_at", -1))
    for b in bookings:
        b["_id"] = str(b["_id"])
        # Fallback to prevent UI break if AI hasn't captured name yet
        if "name" not in b or not b["name"]:
            b["name"] = "Pending..."
    return bookings

@app.delete("/api/bookings/{id}")
async def remove_booking(id: str):
    """Deletes a specific booking from the database."""
    from bson import ObjectId
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

@app.post("/api/sync")
async def force_sync():
    """Manual sync button to push all confirmed database records to Google Sheets."""
    bookings = list(db.bookings.find({"status": "Confirmed"}))
    success_count = 0
    for b in bookings:
        if sync_to_sheets(b): success_count += 1
    return {"message": f"Successfully synced {success_count} records to Sheets."}

@app.get("/api/menu")
async def get_menu_items():
    """Fetches the menu list for the Admin and Customer views."""
    items = list(db.menu.find({}))
    for i in items:
        i["_id"] = str(i["_id"])
    return items

@app.post("/api/menu")
async def create_menu_item(name: str = Form(...), price: str = Form(...), photo: str = Form(...)):
    """Allows Admin to add new signature dishes via the dashboard."""
    db.menu.insert_one({"name": name, "price": price, "photo": photo})
    return HTMLResponse("<script>window.location.href='/admin'</script>")

@app.delete("/api/menu/{id}")
async def delete_menu_item(id: str):
    """Removes a dish from the menu."""
    from bson import ObjectId
    db.menu.delete_one({"_id": ObjectId(id)})
    return {"status": "ok"}

# --- FRONTEND ROUTING ---
@app.get("/")
async def home_page(): 
    """Serves the Customer-facing home page."""
    return HTMLResponse(open("home.html").read())

@app.get("/admin")
async def admin_page(): 
    """Serves the Admin Dashboard."""
    return HTMLResponse(open("index.html").read())

@app.get("/static/{file}")
async def serve_static(file: str): 
    """Serves generated audio files and static assets."""
    return FileResponse(f"static/{file}")

# --- SERVER LIFECYCLE ---
if __name__ == "__main__":
    import uvicorn
    # Ensure static directory exists for audio files
    if not os.path.exists("static"):
        os.makedirs("static")
    # Bind to port 8000 for Railway/Local deployment
    uvicorn.run(app, host="0.0.0.0", port=8000)