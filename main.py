import os
import json
import uuid
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
    print(f"CRITICAL Resource Failure: {e}")

call_sessions = {}

def get_system_prompt():
    return {
        "role": "system", 
        "content": f"""You are Jessica, the professional concierge at 'The Velvet Bean Bistro'. 
        TODAY: {datetime.now().strftime('%A, %d %B %Y')}
        GOAL: Collect 1. Name, 2. Date (YYYY-MM-DD), 3. Day, 4. Time, 5. Guests.
        JSON ONLY FORMAT:
        {{
            "reply": "verbal response",
            "is_complete": false,
            "data": {{"name": "null", "date": "null", "day": "null", "time": "null", "guests": "null", "notes": "null"}}
        }}"""
    }

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
    except:
        return False

def get_ai_response(user_input, caller_number, session_id):
    if session_id not in call_sessions:
        call_sessions[session_id] = [get_system_prompt()]
    
    call_sessions[session_id].append({"role": "user", "content": user_input})
    
    try:
        completion = groq_client.chat.completions.create(
            messages=call_sessions[session_id],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"}
        )
        res = json.loads(completion.choices[0].message.content)
        call_sessions[session_id].append({"role": "assistant", "content": res['reply']})
        
        extracted = res.get("data", {})
        # Update current session row in DB
        db.bookings.update_one(
            {"session_id": session_id},
            {"$set": {**extracted, "contact": caller_number, "status": "Confirmed" if res.get("is_complete") else "In-Progress", "created_at": datetime.now()}},
            upsert=True
        )
        return res
    except:
        return {"reply": "I'm sorry, I'm having trouble. Can you repeat that?", "is_complete": False}

@app.get("/", response_class=HTMLResponse)
async def home_page():
    with open("home.html") as f: return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("index.html") as f: return f.read()

@app.post("/voice")
async def voice_start(request: Request):
    form_data = await request.form()
    caller = form_data.get("From", "unknown")
    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    
    # Create new entry for this specific call immediately
    db.bookings.insert_one({
        "session_id": session_id,
        "contact": caller,
        "name": "Unknown",
        "status": "In-Progress",
        "created_at": datetime.now()
    })
    
    call_sessions[session_id] = [get_system_prompt()]
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()
    msg = "Welcome to The Velvet Bean. I'm Jessica. How can I help you?"
    
    fid = f"start_{uuid.uuid4().hex[:8]}"
    if generate_audio(msg, fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        response.say(msg, voice='Polly.Aditi')
    
    gather = Gather(input='speech', action=f"{base_url}/respond?sid={session_id}", language='en-IN', speech_timeout='1.2', enhanced=True)
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/respond")
async def handle_response(request: Request, sid: str, SpeechResult: str = Form(None), From: str = Form(None)):
    base_url = str(request.base_url).replace("http://", "https://").rstrip("/")
    response = VoiceResponse()

    if not SpeechResult:
        response.say("I missed that. Could you repeat?", voice='Polly.Aditi')
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.2'))
        return HTMLResponse(content=str(response), media_type="application/xml")

    ai_decision = get_ai_response(SpeechResult, From, sid)
    fid = f"reply_{uuid.uuid4().hex[:8]}"
    
    if generate_audio(ai_decision['reply'], fid):
        response.play(f"{base_url}/static/{fid}.mp3")
    else:
        response.say(ai_decision['reply'], voice='Polly.Aditi')

    if not ai_decision.get("is_complete"):
        response.append(Gather(input='speech', action=f"{base_url}/respond?sid={sid}", language='en-IN', speech_timeout='1.2'))
    else:
        call_sessions.pop(sid, None)
        response.hangup()
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.get("/api/bookings")
async def get_bookings():
    # Return all bookings to let the frontend handle the "Top 10" sorting logic
    bookings = list(db.bookings.find({}).sort("created_at", -1))
    for b in bookings: 
        b["_id"] = str(b["_id"])
        if "created_at" in b: b["created_at"] = b["created_at"].isoformat()
    return bookings

@app.delete("/api/bookings/{id}")
async def delete_booking(id: str):
    from bson import ObjectId
    db.bookings.delete_one({"_id": ObjectId(id)})
    return {"status": "deleted"}

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
    return {"status": "deleted"}

@app.get("/static/{file_name}")
async def serve_static(file_name: str):
    return FileResponse(f"static/{file_name}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))