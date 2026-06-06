"""
Shrimad Bhagwat Katha — Registration & Check-in Server
- Daily registration (time-gated)
- Unique QR passes per day, capacity resets at midnight IST
- One registration per phone number per day
- Google Sheets logging
- Upstash Redis for persistence
"""

import os
import io
import json
import hashlib
import base64
import qrcode
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, render_template_string, redirect, make_response

app = Flask(__name__)

TOTAL_CAPACITY = 50
TICKET_SECRET = os.environ.get("TICKET_SECRET", "katha-qr-2026-secret")
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Time helpers (IST)
# ---------------------------------------------------------------------------

def now_ist():
    return datetime.now(IST)

def today_ist():
    return now_ist().strftime("%Y-%m-%d")

def is_registration_open():
    return True

# ---------------------------------------------------------------------------
# Daily ticket generation (deterministic per day + serial)
# ---------------------------------------------------------------------------

def generate_ticket_id(date_str, serial):
    raw = f"{date_str}-{serial:03d}-{TICKET_SECRET}"
    short_hash = hashlib.sha256(raw.encode()).hexdigest()[:10].upper()
    return f"SBK-{date_str.replace('-', '')}-{serial:03d}-{short_hash}"

def get_valid_tickets_for_date(date_str):
    tickets = {}
    for serial in range(1, TOTAL_CAPACITY + 1):
        tid = generate_ticket_id(date_str, serial)
        tickets[tid] = serial
    return tickets

# ---------------------------------------------------------------------------
# Storage backend
# ---------------------------------------------------------------------------

USE_REDIS = bool(os.environ.get("KV_REST_API_URL"))
IS_VERCEL = bool(os.environ.get("VERCEL"))
LOCAL_DATA_DIR = "/tmp/app_data" if IS_VERCEL else "output"

_redis_client = None

def _get_redis():
    global _redis_client
    if _redis_client is None:
        from upstash_redis import Redis
        _redis_client = Redis(
            url=os.environ["KV_REST_API_URL"],
            token=os.environ["KV_REST_API_TOKEN"],
        )
    return _redis_client

def _load_json_file(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def _save_json_file(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_registrations(date_str):
    key = f"registrations:{date_str}"
    if USE_REDIS:
        raw = _get_redis().get(key)
        return json.loads(raw) if raw else {}
    return _load_json_file(os.path.join(LOCAL_DATA_DIR, f"registrations_{date_str}.json"))

def save_registrations(date_str, registrations):
    key = f"registrations:{date_str}"
    if USE_REDIS:
        _get_redis().set(key, json.dumps(registrations, ensure_ascii=False))
    else:
        _save_json_file(os.path.join(LOCAL_DATA_DIR, f"registrations_{date_str}.json"), registrations)

def load_used_tickets(date_str):
    key = f"used_tickets:{date_str}"
    if USE_REDIS:
        raw = _get_redis().get(key)
        return json.loads(raw) if raw else {}
    return _load_json_file(os.path.join(LOCAL_DATA_DIR, f"used_tickets_{date_str}.json"))

def save_used_tickets(date_str, used_tickets):
    key = f"used_tickets:{date_str}"
    if USE_REDIS:
        _get_redis().set(key, json.dumps(used_tickets))
    else:
        _save_json_file(os.path.join(LOCAL_DATA_DIR, f"used_tickets_{date_str}.json"), used_tickets)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

GOOGLE_SHEETS_ENABLED = bool(os.environ.get("GOOGLE_SHEETS_CREDS"))
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
_gspread_client = None

def _get_gspread():
    global _gspread_client
    if _gspread_client is None:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_json = json.loads(base64.b64decode(os.environ["GOOGLE_SHEETS_CREDS"]))
        creds = Credentials.from_service_account_info(creds_json, scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
        ])
        _gspread_client = gspread.authorize(creds)
    return _gspread_client

def _get_or_create_worksheet(sh, title, headers):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

def sheet_append_registration(date_str, name, phone, attendees, invitee_name, ticket_serials):
    if not GOOGLE_SHEETS_ENABLED:
        return
    try:
        gc = _get_gspread()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = _get_or_create_worksheet(sh, "Registrations",
            ["Date", "Time", "Name", "Phone", "Attendees", "Invitee Name", "Tickets"])
        time_str = now_ist().strftime("%I:%M %p")
        serials_str = ", ".join(f"#{s:03d}" for s in ticket_serials)
        ws.append_row([date_str, time_str, name, phone, str(attendees), invitee_name, serials_str])
    except Exception as e:
        print(f"Google Sheets (registration) error: {e}", flush=True)

def sheet_append_update(date_str, name, phone, old_attendees, new_attendees, invitee_name, ticket_serials):
    if not GOOGLE_SHEETS_ENABLED:
        return
    try:
        gc = _get_gspread()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = _get_or_create_worksheet(sh, "Registrations",
            ["Date", "Time", "Name", "Phone", "Attendees", "Invitee Name", "Tickets"])
        time_str = now_ist().strftime("%I:%M %p")
        serials_str = ", ".join(f"#{s:03d}" for s in ticket_serials)
        ws.append_row([date_str, time_str + " (UPDATE)", name, phone, f"{old_attendees} -> {new_attendees}", invitee_name, serials_str])
    except Exception as e:
        print(f"Google Sheets (update) error: {e}", flush=True)

def sheet_append_checkin(date_str, serial, ticket_id, reg_name, reg_phone):
    if not GOOGLE_SHEETS_ENABLED:
        return
    try:
        gc = _get_gspread()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = _get_or_create_worksheet(sh, "Check-ins",
            ["Date", "Time", "Ticket #", "Ticket ID", "Name", "Phone"])
        time_str = now_ist().strftime("%I:%M %p")
        ws.append_row([date_str, time_str, f"#{serial:03d}", ticket_id, reg_name, reg_phone])
    except Exception as e:
        print(f"Google Sheets (checkin) error: {e}", flush=True)

# ---------------------------------------------------------------------------
# QR image generation
# ---------------------------------------------------------------------------

def generate_qr_bytes(ticket_id):
    img = qrcode.make(ticket_id, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_assigned_serials(registrations):
    serials = set()
    for r in registrations.values():
        for t in r.get("tickets", []):
            serials.add(t["serial"])
    return serials

def total_attendees_registered(registrations):
    return sum(int(r["attendees"]) for r in registrations.values())

def get_next_available_tickets(count, date_str, registrations):
    assigned = get_assigned_serials(registrations)
    tickets = []
    for serial in range(1, TOTAL_CAPACITY + 1):
        if serial not in assigned:
            tid = generate_ticket_id(date_str, serial)
            tickets.append((serial, tid))
            if len(tickets) >= count:
                break
    return tickets

def find_registration_by_ticket(date_str, ticket_id):
    registrations = load_registrations(date_str)
    for phone, reg in registrations.items():
        for t in reg.get("tickets", []):
            if t["ticket_id"] == ticket_id:
                return reg["name"], phone
    return "Unknown", "Unknown"


def _append_scan_log(date_str, serial, name, time_str, ok):
    """Store recent scan in Redis/local for cross-device visibility."""
    key = f"scan_log:{date_str}"
    entry = {"serial": serial, "name": name, "time": time_str, "ok": ok}
    if USE_REDIS:
        raw = _get_redis().get(key)
        scans = json.loads(raw) if raw else []
        scans.insert(0, entry)
        scans = scans[:30]
        _get_redis().set(key, json.dumps(scans))
    else:
        path = os.path.join(LOCAL_DATA_DIR, f"scan_log_{date_str}.json")
        scans = _load_json_file(path) if os.path.exists(path) else []
        if not isinstance(scans, list):
            scans = []
        scans.insert(0, entry)
        scans = scans[:30]
        _save_json_file(path, scans)


def _load_scan_log(date_str):
    key = f"scan_log:{date_str}"
    if USE_REDIS:
        raw = _get_redis().get(key)
        return json.loads(raw) if raw else []
    path = os.path.join(LOCAL_DATA_DIR, f"scan_log_{date_str}.json")
    data = _load_json_file(path)
    return data if isinstance(data, list) else []

# ---------------------------------------------------------------------------
# Shared CSS for religious theme
# ---------------------------------------------------------------------------

THEME_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&display=swap');
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .hindi { font-family: 'Tiro Devanagari Hindi', serif; }
"""

# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------

SCANNER_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scanner Login | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&display=swap" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #1a0a00; color: #fff;
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: linear-gradient(135deg, #2a1a0a, #1a0a00);
    border: 2px solid #CC5500; border-radius: 20px; padding: 40px 28px;
    width: 100%; max-width: 380px; text-align: center;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  .lock { font-size: 3rem; margin-bottom: 16px; }
  h1 { font-size: 1.3rem; color: #FFD700; margin-bottom: 6px; }
  p { color: rgba(255,255,255,0.6); font-size: 0.85rem; margin-bottom: 24px; }
  input[type=password] {
    width: 100%; padding: 14px; border: 2px solid #CC5500; border-radius: 10px;
    background: rgba(255,255,255,0.05); color: #fff; font-size: 1rem;
    outline: none; text-align: center; letter-spacing: 2px;
  }
  input[type=password]:focus { border-color: #FFD700; }
  button {
    width: 100%; padding: 14px; margin-top: 16px;
    background: linear-gradient(135deg, #CC5500, #8B1A1A); color: #fff;
    font-size: 1rem; font-weight: 700; border: none; border-radius: 10px; cursor: pointer;
  }
  .error { color: #ff6b6b; font-size: 0.85rem; margin-top: 12px; }
</style>
</head>
<body>
<div class="card">
  <div class="lock">&#x1F512;</div>
  <h1>Scanner Login</h1>
  <p>Enter the admin password to access the check-in scanner</p>
  <form method="POST" action="/scanner">
    <input type="password" name="password" placeholder="Password" required autofocus>
    <button type="submit">UNLOCK SCANNER</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div>
</body>
</html>
"""


SCANNER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Katha Check-in Scanner</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #1a0a00; color: #fff;
    min-height: 100vh; display: flex; flex-direction: column; align-items: center;
  }
  .header {
    padding: 20px; text-align: center; width: 100%;
    background: linear-gradient(135deg, #8B1A1A, #CC5500);
    border-bottom: 3px solid #FFD700;
  }
  .header h1 { font-size: 1.3rem; letter-spacing: 1px; color: #FFD700; }
  .header .subtitle { font-size: 0.85rem; color: rgba(255,255,255,0.8); margin-top: 4px; }
  .header .date-badge {
    display: inline-block; margin-top: 8px; padding: 4px 14px;
    background: #FFD700; color: #8B1A1A; border-radius: 20px; font-size: 0.8rem; font-weight: 600;
  }
  .stats {
    display: flex; gap: 20px; justify-content: center;
    margin-top: 10px; font-size: 0.85rem; color: rgba(255,255,255,0.7);
  }
  .stats span { color: #FFD700; font-weight: 700; font-size: 1.1rem; }
  #reader-container { width: 100%; max-width: 500px; margin: 20px auto; padding: 0 16px; }
  #reader { width: 100%; border-radius: 12px; overflow: hidden; }
  .result-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    z-index: 100; flex-direction: column; align-items: center; justify-content: center;
    padding: 40px; text-align: center; animation: fadeIn 0.2s ease;
  }
  .result-overlay.show { display: flex; }
  .result-overlay.valid { background: rgba(16, 185, 129, 0.95); }
  .result-overlay.invalid { background: rgba(200, 30, 30, 0.95); }
  .result-overlay.unknown { background: rgba(107, 114, 128, 0.95); }
  .result-icon { font-size: 5rem; margin-bottom: 20px; }
  .result-title { font-size: 2rem; font-weight: 800; margin-bottom: 10px; }
  .result-detail { font-size: 1.1rem; opacity: 0.9; margin-bottom: 8px; }
  .result-ticket-id { font-family: monospace; font-size: 0.9rem; opacity: 0.7; margin-top: 5px; }
  .result-dismiss {
    margin-top: 30px; padding: 14px 40px;
    background: rgba(255,255,255,0.2); border: 2px solid #fff; color: #fff;
    font-size: 1.1rem; font-weight: 600; border-radius: 50px; cursor: pointer;
  }
  .log { width: 100%; max-width: 500px; padding: 16px; margin-top: 10px; }
  .log h3 { font-size: 0.9rem; color: #9ca3af; margin-bottom: 8px; }
  .log-entry {
    padding: 10px 14px; margin-bottom: 6px; border-radius: 8px; font-size: 0.85rem;
    display: flex; justify-content: space-between; align-items: center;
  }
  .log-entry.ok { background: rgba(16,185,129,0.15); border-left: 3px solid #10b981; }
  .log-entry.fail { background: rgba(239,68,68,0.15); border-left: 3px solid #ef4444; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
</style>
</head>
<body>
<div class="header">
  <h1>SHRIMAD BHAGWAT KATHA</h1>
  <div class="subtitle">Check-in Scanner</div>
  <div class="date-badge" id="todayDate"></div>
  <div class="stats">
    Checked in: <span id="checkedIn">0</span> / <span id="totalTickets">0</span>
    &nbsp;|&nbsp; Remaining: <span id="remaining">0</span>
  </div>
</div>
<div id="reader-container"><div id="reader"></div></div>
<div class="result-overlay" id="resultOverlay">
  <div class="result-icon" id="resultIcon"></div>
  <div class="result-title" id="resultTitle"></div>
  <div class="result-detail" id="resultDetail"></div>
  <div class="result-ticket-id" id="resultTicketId"></div>
  <button class="result-dismiss" onclick="dismissResult()">SCAN NEXT</button>
</div>
<div class="log"><h3>Recent Scans</h3><div id="logEntries"></div></div>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<script>
let scanner,scanning=true;
document.getElementById('todayDate').textContent=new Date().toLocaleDateString('en-IN',{weekday:'long',year:'numeric',month:'long',day:'numeric'});
function initScanner(){scanner=new Html5Qrcode("reader");scanner.start({facingMode:"environment"},{fps:10,qrbox:{width:250,height:250}},onScanSuccess,()=>{}).catch(()=>{document.getElementById("reader").innerHTML='<p style="padding:40px;text-align:center;color:#ef4444;">Camera access denied.</p>';});}
async function onScanSuccess(t){if(!scanning)return;scanning=false;scanner.pause(true);try{const r=await fetch("/api/checkin",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket_id:t})});const d=await r.json();showResult(d,t);refreshStats();refreshLog();}catch(e){showResult({status:"error"},t);}}
function showResult(d,t){const o=document.getElementById("resultOverlay"),i=document.getElementById("resultIcon"),tt=document.getElementById("resultTitle"),dd=document.getElementById("resultDetail"),tid=document.getElementById("resultTicketId");o.className="result-overlay show";tid.textContent=t;if(d.status==="ok"){o.classList.add("valid");i.textContent="\\u2713";tt.textContent="WELCOME!";dd.textContent="Entry #"+d.serial+" \\u2014 "+d.entry_number+" of "+d.total;}else if(d.status==="already_used"){o.classList.add("invalid");i.textContent="\\u2717";tt.textContent="ALREADY USED";dd.textContent="Scanned at "+d.used_at;}else if(d.status==="wrong_day"){o.classList.add("invalid");i.textContent="\\u2717";tt.textContent="WRONG DAY";dd.textContent="Not valid today.";}else{o.classList.add("unknown");i.textContent="?";tt.textContent="INVALID";dd.textContent="QR not recognized.";}}
function dismissResult(){document.getElementById("resultOverlay").className="result-overlay";scanning=true;scanner.resume();}
async function refreshStats(){try{const r=await fetch("/api/stats"),d=await r.json();document.getElementById("checkedIn").textContent=d.used;document.getElementById("totalTickets").textContent=d.total;document.getElementById("remaining").textContent=d.remaining;}catch(e){}}
async function refreshLog(){try{const r=await fetch("/api/recent-scans"),d=await r.json();const c=document.getElementById("logEntries");c.innerHTML='';d.scans.forEach(s=>{const div=document.createElement("div");div.className="log-entry "+(s.ok?"ok":"fail");div.innerHTML='<span>'+(s.ok?"\\u2713":"\\u2717")+' #'+String(s.serial).padStart(3,'0')+' '+s.name+'</span><span>'+s.time+'</span>';c.appendChild(div);});}catch(e){}}
document.addEventListener("DOMContentLoaded",()=>{refreshStats();refreshLog();initScanner();setInterval(()=>{refreshStats();refreshLog();},5000);});
</script>
</body>
</html>
"""


CLOSED_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Registration Closed | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&display=swap" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #FFF8E1, #FFE0B2, #FFCC80);
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 40px 28px;
    width: 100%; max-width: 420px; box-shadow: 0 20px 60px rgba(139,26,26,0.15); text-align: center;
    border-top: 5px solid #CC5500;
  }
  .om { font-size: 3rem; color: #CC5500; margin-bottom: 10px; font-family: 'Tiro Devanagari Hindi', serif; }
  h1 { font-size: 1.4rem; color: #8B1A1A; margin-bottom: 10px; }
  p { color: #6b4c3b; font-size: 0.95rem; line-height: 1.6; }
  .time-badge {
    display: inline-block; margin-top: 16px; padding: 10px 24px;
    background: #FFF3E0; border: 2px solid #CC5500; border-radius: 12px;
    font-weight: 700; color: #BF360C; font-size: 1rem;
  }
  .venue { margin-top: 16px; padding: 12px; background: #FFF8E1; border-radius: 10px; font-size: 0.85rem; color: #6b4c3b; }
  .current-time { margin-top: 20px; font-size: 0.85rem; color: #9ca3af; }
</style>
</head>
<body>
<div class="card">
  <div class="om">&#x1F539;</div>
  <h1>Registration Closed</h1>
  <p>Same-day registration is only available between</p>
  <div class="time-badge">8:00 AM &ndash; 2:00 PM IST</div>
  <div class="venue">
    <strong>Katha Timing:</strong> 4:00 PM to 7:00 PM<br>
    <strong>Venue:</strong> Gate No 3, Shri Adya Katyayani Shakti Peeth Mandir, Chhatarpur, New Delhi
  </div>
  <p style="margin-top:16px">Please come back during registration hours.</p>
  <div class="current-time">Current IST time: {{ current_time }}</div>
</div>
</body>
</html>
"""


ALREADY_REGISTERED_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Already Registered | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&display=swap" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #FFF8E1, #FFE0B2, #FFCC80);
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 36px 28px;
    width: 100%; max-width: 420px; box-shadow: 0 20px 60px rgba(139,26,26,0.15); text-align: center;
    border-top: 5px solid #CC5500;
  }
  .icon { font-size: 3rem; margin-bottom: 12px; }
  h1 { font-size: 1.3rem; color: #8B1A1A; margin-bottom: 8px; }
  .msg {
    background: #FFF3E0; border: 1px solid #FFCC80; border-radius: 10px;
    padding: 14px; font-size: 0.9rem; color: #BF360C; margin-bottom: 20px;
  }
  .msg strong { display: block; margin-bottom: 4px; }
  .link-btn {
    display: inline-block; padding: 12px 28px; background: linear-gradient(135deg, #CC5500, #8B1A1A);
    color: #fff; text-decoration: none; border-radius: 12px; font-weight: 700; font-size: 0.95rem;
  }
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#x1F64F;</div>
  <h1>Already Registered!</h1>
  <div class="msg">
    <strong>{{ name }}, this phone number is already registered for today's Katha.</strong>
    You have {{ attendees }} pass{{ 'es' if attendees|int > 1 else '' }} assigned.
  </div>
  <a class="link-btn" href="/my-passes?phone={{ phone }}&date={{ date_str }}">View My Passes</a>
</div>
</body>
</html>
"""


REGISTER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Registration | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#1a0a00;min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:20px;position:relative;overflow-x:hidden;
}
body::before{
  content:'';position:fixed;top:0;left:0;right:0;bottom:0;
  background:radial-gradient(ellipse at 50% 20%,rgba(255,140,0,0.12) 0%,transparent 60%),
             radial-gradient(ellipse at 80% 80%,rgba(139,26,26,0.1) 0%,transparent 50%);
  pointer-events:none;
}
.page{position:relative;z-index:1;width:100%;max-width:560px;}
.lang-toggle{display:flex;justify-content:flex-end;margin-bottom:10px;}
.lang-btn{
  padding:6px 16px;border:1.5px solid rgba(255,165,0,0.4);font-size:0.75rem;
  font-weight:600;cursor:pointer;background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.6);
  transition:all 0.2s;backdrop-filter:blur(8px);
}
.lang-btn.active{background:rgba(255,165,0,0.2);color:#FFD700;border-color:#FF8C00;}
.lang-btn:first-child{border-radius:20px 0 0 20px;}
.lang-btn:last-child{border-radius:0 20px 20px 0;}
.card{
  background:linear-gradient(145deg,rgba(42,26,10,0.9),rgba(26,10,0,0.95));
  border-radius:24px;padding:32px 24px;
  box-shadow:0 24px 64px rgba(0,0,0,0.6),inset 0 1px 0 rgba(255,165,0,0.1);
  border:1px solid rgba(255,165,0,0.12);position:relative;overflow:hidden;
}
.card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:4px;
  background:linear-gradient(90deg,#8B1A1A,#CC5500,#FF8C00,#FFD700,#FF8C00,#CC5500,#8B1A1A);
}
.card-header{text-align:center;margin-bottom:20px;}
.om-symbol{font-size:2.2rem;margin-bottom:4px;filter:drop-shadow(0 0 12px rgba(255,140,0,0.4));}
.greeting{
  font-family:'Tiro Devanagari Hindi',serif;font-size:1.5rem;
  color:#FF8C00;font-weight:700;margin-bottom:2px;
}
.card-header h1{
  font-family:'Playfair Display',serif;font-size:1.35rem;color:#FFD700;
  margin-bottom:2px;letter-spacing:0.5px;
}
.card-header .sub{font-size:0.88rem;color:rgba(255,215,0,0.7);font-weight:600;letter-spacing:1px;text-transform:uppercase;}
.divider{
  width:80px;height:2px;margin:10px auto 12px;
  background:linear-gradient(90deg,transparent,#FF8C00,#FFD700,#FF8C00,transparent);
}
.date-pill{
  display:inline-block;padding:5px 16px;background:rgba(255,165,0,0.1);color:#FFD700;
  border-radius:20px;font-size:0.78rem;font-weight:600;
  border:1px solid rgba(255,165,0,0.2);
}
.info-strip{
  display:flex;gap:0;margin:16px 0;border-radius:14px;overflow:hidden;
  border:1px solid rgba(255,165,0,0.15);
}
.info-strip .info-item{
  flex:1;padding:12px 8px;text-align:center;
  background:rgba(255,165,0,0.05);
  border-right:1px solid rgba(255,165,0,0.1);
}
.info-strip .info-item:last-child{border-right:none;}
.info-strip .info-icon{font-size:1.1rem;margin-bottom:3px;}
.info-strip .info-label{font-size:0.62rem;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px;}
.info-strip .info-val{font-size:0.78rem;color:#FFD700;font-weight:600;line-height:1.3;}
.capacity-bar{margin:0 0 20px;padding:0 2px;}
.capacity-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}
.capacity-header .cap-label{font-size:0.75rem;color:rgba(255,255,255,0.5);}
.capacity-header .cap-count{font-size:0.85rem;color:#FFD700;font-weight:700;}
.bar-track{height:8px;background:rgba(255,255,255,0.08);border-radius:4px;overflow:hidden;}
.bar-fill{height:100%;border-radius:4px;transition:width 0.6s ease;
  background:linear-gradient(90deg,#2E7D32,#66BB6A);}
.bar-fill.warning{background:linear-gradient(90deg,#FF8C00,#FFB74D);}
.bar-fill.critical{background:linear-gradient(90deg,#c62828,#ef5350);}
.bar-fill.full{background:linear-gradient(90deg,#b71c1c,#e53935);}
.error-msg{
  background:rgba(200,30,30,0.12);border:1px solid rgba(255,100,100,0.25);color:#ff8a8a;
  padding:12px 16px;border-radius:12px;font-size:0.88rem;margin-bottom:16px;text-align:center;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.housefull{
  background:linear-gradient(135deg,rgba(183,28,28,0.2),rgba(198,40,40,0.1));
  border:2px solid rgba(255,80,80,0.3);border-radius:16px;
  padding:24px;text-align:center;margin-bottom:20px;
}
.housefull .hf-icon{font-size:2.8rem;margin-bottom:8px;}
.housefull .hf-title{font-size:1.3rem;font-weight:800;color:#ff6b6b;margin-bottom:6px;letter-spacing:2px;}
.housefull .hf-sub{font-size:0.85rem;color:rgba(255,255,255,0.55);line-height:1.5;}
.form-section{margin-top:4px;}
.form-group{margin-bottom:14px;position:relative;}
.form-group label{
  display:flex;align-items:center;gap:6px;
  font-size:0.82rem;font-weight:600;color:rgba(255,255,255,0.7);margin-bottom:6px;
}
.form-group label .field-icon{font-size:0.9rem;opacity:0.7;}
.form-group input,.form-group select{
  width:100%;padding:13px 14px;border:1.5px solid rgba(255,165,0,0.2);border-radius:12px;
  font-size:0.95rem;color:#fff;background:rgba(255,255,255,0.04);
  transition:all 0.25s ease;outline:none;
}
.form-group input::placeholder{color:rgba(255,255,255,0.25);}
.form-group select{color:rgba(255,255,255,0.8);-webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%23FFD700' viewBox='0 0 16 16'%3E%3Cpath d='M1.5 5.5l6.5 6.5 6.5-6.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 14px center;padding-right:36px;
}
.form-group select option{background:#2a1a0a;color:#fff;}
.form-group input:focus,.form-group select:focus{
  border-color:#FF8C00;background:rgba(255,255,255,0.07);
  box-shadow:0 0 0 3px rgba(255,140,0,0.1);
}
.form-group.disabled label{color:rgba(255,255,255,0.2);}
.form-group.disabled input,.form-group.disabled select{
  background:rgba(255,255,255,0.02);color:rgba(255,255,255,0.15);
  border-color:rgba(255,165,0,0.06);pointer-events:none;
}
.submit-btn{
  width:100%;padding:15px;margin-top:6px;
  background:linear-gradient(135deg,#FF8C00,#CC5500,#8B1A1A);
  color:#fff;font-size:1.05rem;font-weight:700;border:none;border-radius:14px;
  cursor:pointer;letter-spacing:0.5px;position:relative;overflow:hidden;
  box-shadow:0 6px 24px rgba(255,140,0,0.3);transition:all 0.2s ease;
}
.submit-btn::after{
  content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.1),transparent);
  transition:left 0.5s ease;
}
.submit-btn:hover::after{left:100%;}
.submit-btn:hover{transform:translateY(-2px);box-shadow:0 10px 32px rgba(255,140,0,0.45);}
.submit-btn:active{transform:translateY(0);}
.submit-btn:disabled{
  background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.3);
  cursor:not-allowed;transform:none;box-shadow:none;
}
.submit-btn:disabled::after{display:none;}
.footer-links{
  margin-top:20px;padding-top:16px;
  border-top:1px solid rgba(255,165,0,0.1);
  text-align:center;
}
.footer-links a{
  display:inline-flex;align-items:center;gap:6px;
  color:rgba(255,215,0,0.7);font-size:0.84rem;text-decoration:none;
  font-weight:500;padding:8px 16px;border-radius:10px;
  transition:all 0.2s;
}
.footer-links a:hover{color:#FFD700;background:rgba(255,165,0,0.08);}
@media(max-width:480px){
  .card{padding:24px 18px;border-radius:20px;}
  .greeting{font-size:1.3rem;}
  .card-header h1{font-size:1.15rem;}
  .info-strip{flex-direction:column;}
  .info-strip .info-item{border-right:none;border-bottom:1px solid rgba(255,165,0,0.1);padding:10px;}
  .info-strip .info-item:last-child{border-bottom:none;}
  .submit-btn{font-size:0.95rem;padding:14px;}
}
</style>
</head>
<body>
<div class="page">
<div class="lang-toggle">
  <button class="lang-btn active" onclick="setLang('en')">English</button>
  <button class="lang-btn" onclick="setLang('hi')">हिन्दी</button>
</div>
<div class="card">
  <div class="card-header">
    <div class="om-symbol">&#x1F549;</div>
    <div class="greeting"><span data-en="JAI MATA DI" data-hi="जय माता दी">JAI MATA DI</span></div>
    <h1 data-en="Shrimad Bhagwat Katha" data-hi="श्रीमद् भागवत कथा">Shrimad Bhagwat Katha</h1>
    <div class="sub" data-en="Registration" data-hi="पंजीकरण">Registration</div>
    <div class="divider"></div>
    <div class="date-pill">{{ date_display }}</div>
  </div>

  <div class="info-strip">
    <div class="info-item">
      <div class="info-icon">&#x1F552;</div>
      <div class="info-label" data-en="Timing" data-hi="समय">Timing</div>
      <div class="info-val" data-en="4:00 - 7:00 PM" data-hi="शाम 4 - 7 बजे">4:00 - 7:00 PM</div>
    </div>
    <div class="info-item">
      <div class="info-icon">&#x1F4CD;</div>
      <div class="info-label" data-en="Venue" data-hi="स्थान">Venue</div>
      <div class="info-val" data-en="Gate 3, Katyayani Mandir<br>Chhatarpur, Delhi" data-hi="गेट 3, कात्यायनी मंदिर<br>छतरपुर, दिल्ली">Gate 3, Katyayani Mandir<br>Chhatarpur, Delhi</div>
    </div>
    <div class="info-item">
      <div class="info-icon">&#x23F0;</div>
      <div class="info-label" data-en="Arrive" data-hi="पहुँचें">Arrive</div>
      <div class="info-val" data-en="15-30 min<br>earlier" data-hi="15-30 मिनट<br>पहले">15-30 min<br>earlier</div>
    </div>
  </div>

  <div class="capacity-bar">
    <div class="capacity-header">
      <span class="cap-label" data-en="Availability" data-hi="उपलब्धता">Availability</span>
      <span class="cap-count"><span id="spotsNum">{{ spots_left }}</span> / {{ total }}</span>
    </div>
    <div class="bar-track">
      <div class="bar-fill {% if spots_left <= 0 %}full{% elif spots_left <= total // 5 %}critical{% elif spots_left <= total // 2 %}warning{% endif %}" style="width:{{ ((total - spots_left) * 100 // total) if total > 0 else 100 }}%"></div>
    </div>
  </div>

  {% if error %}<div class="error-msg">&#x26A0; {{ error }}</div>{% endif %}

  {% if spots_left <= 0 %}
  <div class="housefull">
    <div class="hf-icon">&#x1F6AB;</div>
    <div class="hf-title" data-en="HOUSEFULL" data-hi="हाउसफुल">HOUSEFULL</div>
    <div class="hf-sub" data-en="All seats for today's Katha have been booked.<br>Please try again tomorrow." data-hi="आज की कथा की सभी सीटें बुक हो चुकी हैं।<br>कृपया कल पुनः प्रयास करें।">All seats for today's Katha have been booked.<br>Please try again tomorrow.</div>
  </div>
  {% endif %}

  <div class="form-section">
  <form method="POST" action="/register" id="regForm">
    <div class="form-group {{ 'disabled' if spots_left <= 0 }}">
      <label><span class="field-icon">&#x1F464;</span> <span data-en="Full Name" data-hi="पूरा नाम">Full Name</span></label>
      <input type="text" name="name" {{ 'disabled' if spots_left <= 0 }} required data-ph-en="Enter your full name" data-ph-hi="अपना पूरा नाम लिखें" placeholder="Enter your full name" value="{{ prev.name or '' }}">
    </div>
    <div class="form-group {{ 'disabled' if spots_left <= 0 }}">
      <label><span class="field-icon">&#x1F4F1;</span> <span data-en="Phone Number" data-hi="फ़ोन नंबर">Phone Number</span></label>
      <input type="tel" name="phone" {{ 'disabled' if spots_left <= 0 }} required data-ph-en="10-digit mobile number" data-ph-hi="10 अंकों का मोबाइल नंबर" placeholder="10-digit mobile number" pattern="[0-9]{10}" title="Enter 10-digit phone number" value="{{ prev.phone or '' }}">
    </div>
    <div class="form-group {{ 'disabled' if spots_left <= 0 }}">
      <label><span class="field-icon">&#x1F465;</span> <span data-en="Number of Attendees (max 5)" data-hi="उपस्थित लोगों की संख्या (अधिकतम 5)">Number of Attendees (max 5)</span></label>
      <select name="attendees" {{ 'disabled' if spots_left <= 0 }} required>
        <option value="" data-en="-- Select --" data-hi="-- चुनें --">-- Select --</option>
        <option value="1" {{ 'selected' if prev.attendees == '1' }}>1</option>
        <option value="2" {{ 'selected' if prev.attendees == '2' }}>2</option>
        <option value="3" {{ 'selected' if prev.attendees == '3' }}>3</option>
        <option value="4" {{ 'selected' if prev.attendees == '4' }}>4</option>
        <option value="5" {{ 'selected' if prev.attendees == '5' }}>5</option>
      </select>
    </div>
    <div class="form-group {{ 'disabled' if spots_left <= 0 }}">
      <label><span class="field-icon">&#x1F64F;</span> <span data-en="Invitee Name" data-hi="निमंत्रणकर्ता का नाम">Invitee Name</span></label>
      <select name="invitee_name" {{ 'disabled' if spots_left <= 0 }} required>
        <option value="" data-en="-- Select --" data-hi="-- चुनें --">-- Select --</option>
        <option value="Arun Gupta Ji" {{ 'selected' if prev.invitee_name == 'Arun Gupta Ji' }}>Arun Gupta Ji</option>
        <option value="Sheena Aron Ji" {{ 'selected' if prev.invitee_name == 'Sheena Aron Ji' }}>Sheena Aron Ji</option>
        <option value="Ankit Ji" {{ 'selected' if prev.invitee_name == 'Ankit Ji' }}>Ankit Ji</option>
        <option value="Sanjay Ji" {{ 'selected' if prev.invitee_name == 'Sanjay Ji' }}>Sanjay Ji</option>
        <option value="Rama Shankar Ji" {{ 'selected' if prev.invitee_name == 'Rama Shankar Ji' }}>Rama Shankar Ji</option>
      </select>
    </div>
    <button type="submit" class="submit-btn" id="submitBtn" {{ 'disabled' if spots_left <= 0 }} data-en="&#x1F64F; REGISTER &amp; GET QR PASS" data-hi="&#x1F64F; पंजीकरण करें और QR पास पाएं">&#x1F64F; REGISTER &amp; GET QR PASS</button>
  </form>
  </div>

  <div class="footer-links">
    <a href="/my-passes" data-en="&#x1F4F1; Already registered? View your passes" data-hi="&#x1F4F1; पहले से पंजीकृत? अपने पास देखें">&#x1F4F1; Already registered? View your passes</a>
  </div>
</div>
</div>
<script>
function setLang(lang){
  localStorage.setItem('katha_lang',lang);
  document.querySelectorAll('[data-en]').forEach(function(el){
    el.innerHTML=el.getAttribute('data-'+lang)||el.getAttribute('data-en');
  });
  document.querySelectorAll('[data-ph-en]').forEach(function(el){
    el.placeholder=el.getAttribute('data-ph-'+lang)||el.getAttribute('data-ph-en');
  });
  document.querySelectorAll('.lang-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelector('.lang-btn[onclick="setLang(\\''+lang+'\\')"]').classList.add('active');
}
document.addEventListener('DOMContentLoaded',function(){
  var lang=localStorage.getItem('katha_lang')||'en';setLang(lang);
});
document.getElementById('regForm').addEventListener('submit',function(){
  var b=document.getElementById('submitBtn');b.disabled=true;b.textContent='REGISTERING...';
});
</script>
</body>
</html>
"""


SUCCESS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Registration Successful | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #E8F5E9, #C8E6C9, #A5D6A7);
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 36px 28px;
    width: 100%; max-width: 440px; box-shadow: 0 20px 60px rgba(0,0,0,0.15); text-align: center;
    border-top: 5px solid #2E7D32;
  }
  .check-icon {
    width: 70px; height: 70px; background: #2E7D32; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 16px; font-size: 2.2rem; color: #fff;
  }
  .greeting { font-family: 'Tiro Devanagari Hindi', serif; font-size: 1.4rem; color: #CC5500; margin-bottom: 6px; }
  h1 { font-size: 1.4rem; color: #1B5E20; margin-bottom: 6px; }
  .subtitle { color: #558B2F; font-size: 0.95rem; margin-bottom: 8px; }
  .date-pill {
    display: inline-block; padding: 5px 14px; background: #E8F5E9; color: #2E7D32;
    border-radius: 20px; font-size: 0.8rem; font-weight: 600; margin-bottom: 16px;
  }
  .venue-info {
    background: #FFF8E1; border: 1px solid #FFCC80; border-radius: 10px;
    padding: 10px; font-size: 0.8rem; color: #6b4c3b; margin-bottom: 16px; line-height: 1.5;
  }
  .venue-info strong { color: #8B1A1A; }
  .qr-section { margin-bottom: 16px; }
  .qr-box {
    background: #FFFDE7; border: 2px dashed #FFCC80; border-radius: 16px;
    padding: 20px; margin-bottom: 12px;
  }
  .qr-box img { width: 180px; height: 180px; }
  .qr-label { font-size: 0.85rem; font-weight: 600; color: #5D4037; margin-bottom: 8px; }
  .ticket-id {
    font-family: monospace; background: #FFF3E0; padding: 6px 12px;
    border-radius: 6px; font-size: 0.75rem; color: #5D4037; margin-top: 8px; display: inline-block;
  }
  .ticket-info { margin-top: 16px; }
  .ticket-info .row {
    display: flex; justify-content: space-between; padding: 8px 0;
    border-bottom: 1px solid #E8F5E9; font-size: 0.9rem;
  }
  .ticket-info .row:last-child { border-bottom: none; }
  .ticket-info .label { color: #6b4c3b; }
  .ticket-info .value { color: #3E2723; font-weight: 600; }
  .notice {
    background: #FFF8E1; border: 1px solid #FFCC80; border-radius: 10px;
    padding: 12px; font-size: 0.85rem; color: #6b4c3b; margin-top: 20px;
  }
  .notice strong { color: #BF360C; }
  .update-link {
    display: block; text-align: center; margin-top: 16px; padding: 12px;
    color: #2E7D32; font-size: 0.88rem; font-weight: 600; text-decoration: none;
    border: 2px solid #A5D6A7; border-radius: 12px; transition: all 0.2s;
  }
  .update-link:hover { background: #E8F5E9; }
  .btn-row {
    display: flex; gap: 10px; margin-top: 10px; justify-content: center; flex-wrap: wrap;
  }
  .btn-download, .btn-share {
    padding: 10px 18px; border-radius: 10px; font-size: 0.82rem; font-weight: 600;
    border: none; cursor: pointer; text-decoration: none;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-download { background: #5D4037; color: #fff; }
  .btn-share { background: #25D366; color: #fff; }
  .btn-all-row {
    display: flex; gap: 10px; justify-content: center; flex-wrap: wrap;
    margin-top: 18px; padding-top: 18px; border-top: 2px solid #E8F5E9;
  }
  .btn-all {
    padding: 12px 22px; border-radius: 12px; font-size: 0.88rem; font-weight: 700;
    border: none; cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
  }
  .btn-all.download-pdf { background: linear-gradient(135deg, #8B1A1A, #CC5500); color: #fff; }
  .btn-all.share { background: linear-gradient(135deg, #25D366, #128C7E); color: #fff; }
  .btn-all:disabled { opacity: 0.5; cursor: not-allowed; }
  .lang-toggle {
    display: flex; justify-content: flex-end; margin-bottom: 8px;
  }
  .lang-btn {
    padding: 5px 14px; border: 2px solid #2E7D32; border-radius: 20px; font-size: 0.78rem;
    font-weight: 600; cursor: pointer; background: #fff; color: #2E7D32; transition: all 0.2s;
  }
  .lang-btn.active { background: #2E7D32; color: #fff; }
  .lang-btn:first-child { border-radius: 20px 0 0 20px; }
  .lang-btn:last-child { border-radius: 0 20px 20px 0; }
</style>
</head>
<body>
<div class="card">
  <div class="lang-toggle">
    <button class="lang-btn active" onclick="setLang('en')">English</button>
    <button class="lang-btn" onclick="setLang('hi')">हिन्दी</button>
  </div>
  <div class="check-icon">&#10003;</div>
  <div class="greeting">&#x1F64F; <span data-en="JAI MATA DI" data-hi="जय माता दी">JAI MATA DI</span> &#x1F64F;</div>
  <h1 data-en="You're Registered!" data-hi="आपका पंजीकरण हो गया!">You're Registered!</h1>
  <p class="subtitle"><span data-en="{{ attendees }} QR pass{{ 'es' if attendees|int > 1 else '' }} for today's Katha" data-hi="आज की कथा के लिए {{ attendees }} QR पास">{{ attendees }} QR pass{{ 'es' if attendees|int > 1 else '' }} for today's Katha</span></p>
  <div class="date-pill">{{ date_display }}</div>

  <div class="venue-info">
    <span data-en="<strong>Timing:</strong> 4:00 PM to 7:00 PM &mdash; Reach 15-30 min earlier" data-hi="<strong>समय:</strong> शाम 4:00 से 7:00 बजे तक &mdash; कृपया 15-30 मिनट पहले पहुँचें"></span><br>
    <span data-en="<strong>Venue:</strong> Gate No 3, Shri Adya Katyayani Shakti Peeth Mandir, Chhatarpur, New Delhi" data-hi="<strong>स्थान:</strong> गेट नं. 3, श्री आद्य कात्यायनी शक्ति पीठ मंदिर, छतरपुर, नई दिल्ली"></span>
  </div>

  <div class="qr-section">
    {% for t in tickets %}
    <div class="qr-box">
      <div class="qr-label"><span data-en="Attendee {{ loop.index }} of {{ attendees }}" data-hi="उपस्थित {{ loop.index }} / {{ attendees }}">Attendee {{ loop.index }} of {{ attendees }}</span> &mdash; Pass #{{ '%03d' % t.serial }}</div>
      <img src="/qr-image/{{ date_str }}/{{ t.serial }}" alt="QR #{{ t.serial }}" id="qr-{{ t.serial }}" crossorigin="anonymous">
      <div class="ticket-id">{{ t.ticket_id }}</div>
      <div class="btn-row">
        <button class="btn-share" onclick="shareOne({{ t.serial }},'{{ '%03d' % t.serial }}')"><span data-en="&#9993; Share" data-hi="&#9993; शेयर करें">&#9993; Share</span></button>
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="btn-all-row">
    <button class="btn-all download-pdf" id="btnPdf" onclick="downloadPDF()"><span data-en="&#128196; Download All as PDF" data-hi="&#128196; सभी PDF डाउनलोड करें">&#128196; Download All as PDF</span></button>
    <button class="btn-all share" id="btnShareAll" onclick="shareAll()"><span data-en="&#9993; Share All" data-hi="&#9993; सभी शेयर करें">&#9993; Share All</span></button>
  </div>

  <div class="ticket-info">
    <div class="row"><span class="label" data-en="Registered by" data-hi="पंजीकृत">Registered by</span><span class="value">{{ name }}</span></div>
    <div class="row"><span class="label" data-en="Total Attendees" data-hi="कुल उपस्थित">Total Attendees</span><span class="value">{{ attendees }}</span></div>
    <div class="row"><span class="label" data-en="Invited by" data-hi="निमंत्रणकर्ता">Invited by</span><span class="value">{{ invitee_name }}</span></div>
    <div class="row"><span class="label" data-en="Valid for" data-hi="वैध तिथि">Valid for</span><span class="value">{{ date_display }}</span></div>
  </div>

  <div class="notice" data-en="Each person must show their own QR at the gate. <strong>Valid today only. One-time use.</strong>" data-hi="प्रत्येक व्यक्ति को गेट पर अपना QR दिखाना होगा। <strong>केवल आज के लिए वैध। एक बार उपयोग।</strong>">
    Each person must show their own QR at the gate. <strong>Valid today only. One-time use.</strong>
  </div>

  <a href="/update-registration?phone={{ phone }}" class="update-link" data-en="&#x270F; Update Registration (change attendees / invitee)" data-hi="&#x270F; पंजीकरण अपडेट करें (उपस्थित / निमंत्रणकर्ता बदलें)">&#x270F; Update Registration (change attendees / invitee)</a>
</div>
<script>
const TICKETS = {{ tickets_json | safe }};
const DATE_STR = '{{ date_str }}';
const REG_NAME = '{{ name }}';
const DATE_DISPLAY = '{{ date_display }}';

async function fetchBlob(serial) {
  const r = await fetch('/qr-image/' + DATE_STR + '/' + serial);
  return await r.blob();
}

function blobToDataURL(blob) {
  return new Promise(r => { const rd = new FileReader(); rd.onload = () => r(rd.result); rd.readAsDataURL(blob); });
}

async function shareOne(serial, serialStr) {
  try {
    const blob = await fetchBlob(serial);
    const file = new File([blob], 'katha_pass_' + serialStr + '.png', {type:'image/png'});
    if (navigator.share && navigator.canShare({files:[file]})) {
      await navigator.share({title:'Katha Pass #'+serialStr, text:'Shrimad Bhagwat Katha entry pass for '+DATE_DISPLAY+'. Gate No 3, Katyayani Mandir, Chhatarpur. Timing: 4-7 PM. Show QR at gate!', files:[file]});
    } else {
      window.open('https://wa.me/?text='+encodeURIComponent('Shrimad Bhagwat Katha Pass #'+serialStr+' ('+DATE_DISPLAY+'). Venue: Gate No 3, Katyayani Mandir Chhatarpur. 4-7 PM. QR: '+window.location.origin+'/qr-image/'+DATE_STR+'/'+serial),'_blank');
    }
  } catch(e) {}
}

async function downloadPDF() {
  const btn = document.getElementById('btnPdf');
  btn.disabled = true; btn.textContent = 'Preparing PDF...';
  try {
    const { jsPDF } = window.jspdf;
    const doc = new jsPDF({orientation:'portrait', unit:'mm', format:'a4'});
    for (let i = 0; i < TICKETS.length; i++) {
      if (i > 0) doc.addPage();
      const t = TICKETS[i];
      const s = String(t.serial).padStart(3, '0');
      const blob = await fetchBlob(t.serial);
      const dataUrl = await blobToDataURL(blob);

      doc.setFillColor(255, 248, 225);
      doc.rect(0, 0, 210, 297, 'F');

      doc.setFontSize(22);
      doc.setTextColor(139, 26, 26);
      doc.text('Shrimad Bhagwat Katha', 105, 35, {align:'center'});

      doc.setFontSize(14);
      doc.setTextColor(204, 85, 0);
      doc.text('JAI MATA DI', 105, 48, {align:'center'});

      doc.setFontSize(11);
      doc.setTextColor(93, 64, 55);
      doc.text(DATE_DISPLAY, 105, 60, {align:'center'});

      doc.addImage(dataUrl, 'PNG', 55, 72, 100, 100);

      doc.setFontSize(14);
      doc.setTextColor(62, 39, 35);
      doc.text('Pass #' + s + ' — Attendee ' + (i+1) + ' of ' + TICKETS.length, 105, 185, {align:'center'});

      doc.setFontSize(9);
      doc.setTextColor(120, 100, 90);
      doc.text(t.ticket_id, 105, 195, {align:'center'});

      doc.setFontSize(10);
      doc.setTextColor(93, 64, 55);
      doc.text('Registered by: ' + REG_NAME, 105, 212, {align:'center'});

      doc.setDrawColor(204, 85, 0);
      doc.setLineWidth(0.5);
      doc.line(30, 222, 180, 222);

      doc.setFontSize(11);
      doc.setTextColor(139, 26, 26);
      doc.text('Timing: 4:00 PM to 7:00 PM', 105, 232, {align:'center'});
      doc.text('Kindly reach 15-30 min earlier', 105, 240, {align:'center'});

      doc.setFontSize(9);
      doc.setTextColor(93, 64, 55);
      doc.text('Gate No 3, Shri Adya Katyayani Shakti Peeth Mandir', 105, 252, {align:'center'});
      doc.text('Chhatarpur, New Delhi', 105, 259, {align:'center'});

      doc.setFontSize(8);
      doc.setTextColor(150, 130, 120);
      doc.text('Each person must show their own QR at the gate. Valid today only.', 105, 275, {align:'center'});
    }
    doc.save('katha_passes_' + DATE_STR + '.pdf');
  } catch(e) { alert('PDF generation failed. Please screenshot your QR codes.'); console.error(e); }
  btn.disabled = false; btn.innerHTML = '&#128196; Download All as PDF';
}

async function shareAll() {
  const btn = document.getElementById('btnShareAll');
  btn.disabled = true; btn.textContent = 'Preparing...';
  try {
    const files = [];
    for (const t of TICKETS) {
      const blob = await fetchBlob(t.serial);
      const s = String(t.serial).padStart(3,'0');
      files.push(new File([blob], 'katha_pass_'+s+'.png', {type:'image/png'}));
    }
    if (navigator.share && navigator.canShare({files})) {
      await navigator.share({title:'Katha Passes', text:'Shrimad Bhagwat Katha passes for '+DATE_DISPLAY+'. Gate No 3, Katyayani Mandir, Chhatarpur. 4-7 PM.', files});
    } else {
      alert('Sharing not supported. Please use Download PDF.');
    }
  } catch(e) { if(e.name !== 'AbortError') alert('Share failed.'); }
  btn.disabled = false; btn.innerHTML = '&#9993; Share All';
}

function setLang(lang) {
  localStorage.setItem('katha_lang', lang);
  document.querySelectorAll('[data-en]').forEach(function(el) {
    el.innerHTML = el.getAttribute('data-' + lang) || el.getAttribute('data-en');
  });
  document.querySelectorAll('.lang-btn').forEach(function(b) { b.classList.remove('active'); });
  document.querySelector('.lang-btn[onclick="setLang(\\''+lang+'\\')"]').classList.add('active');
}
(function(){ var lang = localStorage.getItem('katha_lang') || 'en'; setLang(lang); })();
</script>
</body>
</html>
"""


HOME_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi:wght@400&family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  min-height:100vh;overflow-x:hidden;
  background:#1a0a00;color:#fff;
}
.hero{
  position:relative;min-height:100vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;padding:40px 20px;
  background:radial-gradient(ellipse at 50% 30%,#3a1a00 0%,#1a0a00 70%);
  overflow:hidden;
}
.hero::before{
  content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;
  background:radial-gradient(circle at 50% 50%,rgba(255,165,0,0.06) 0%,transparent 50%);
  animation:glow 8s ease-in-out infinite alternate;
}
@keyframes glow{0%{transform:scale(1);opacity:0.5;}100%{transform:scale(1.2);opacity:1;}}
.diya-row{display:flex;gap:20px;margin-bottom:20px;position:relative;z-index:1;}
.diya{font-size:2rem;animation:flicker 2s ease-in-out infinite alternate;}
.diya:nth-child(2){animation-delay:0.5s;}
.diya:nth-child(3){animation-delay:1s;}
@keyframes flicker{0%{opacity:0.7;transform:scale(1);}100%{opacity:1;transform:scale(1.1);}}
.om{
  font-family:'Tiro Devanagari Hindi',serif;font-size:4rem;color:#FF8C00;
  text-shadow:0 0 40px rgba(255,140,0,0.5),0 0 80px rgba(255,140,0,0.2);
  margin-bottom:8px;position:relative;z-index:1;
}
.hindi-title{
  font-family:'Tiro Devanagari Hindi',serif;font-size:2.4rem;
  color:#FFD700;line-height:1.3;margin-bottom:4px;position:relative;z-index:1;
  text-shadow:0 2px 20px rgba(255,215,0,0.3);
}
.eng-title{
  font-family:'Playfair Display',serif;font-size:1.6rem;color:rgba(255,255,255,0.85);
  letter-spacing:3px;text-transform:uppercase;margin-bottom:20px;position:relative;z-index:1;
}
.divider{
  width:120px;height:2px;margin:0 auto 24px;position:relative;z-index:1;
  background:linear-gradient(90deg,transparent,#FF8C00,#FFD700,#FF8C00,transparent);
}
.verse{
  font-family:'Tiro Devanagari Hindi',serif;font-size:1.15rem;
  color:rgba(255,215,0,0.7);max-width:500px;line-height:1.8;
  margin-bottom:28px;position:relative;z-index:1;font-style:italic;
}
.info-cards{
  display:flex;flex-wrap:wrap;gap:14px;justify-content:center;
  margin-bottom:32px;position:relative;z-index:1;max-width:520px;
}
.info-card{
  background:rgba(255,255,255,0.06);border:1px solid rgba(255,165,0,0.25);
  border-radius:14px;padding:16px 20px;flex:1;min-width:140px;
  backdrop-filter:blur(8px);
}
.info-card .icon{font-size:1.6rem;margin-bottom:6px;}
.info-card .label{font-size:0.72rem;color:rgba(255,255,255,0.5);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
.info-card .value{font-size:0.95rem;color:#FFD700;font-weight:600;line-height:1.4;}
.register-btn{
  display:inline-flex;align-items:center;gap:10px;
  padding:18px 48px;font-size:1.15rem;font-weight:700;
  color:#fff;border:none;border-radius:50px;cursor:pointer;
  background:linear-gradient(135deg,#FF8C00,#CC5500,#8B1A1A);
  box-shadow:0 8px 32px rgba(255,140,0,0.4),inset 0 1px 0 rgba(255,255,255,0.15);
  text-decoration:none;position:relative;z-index:1;
  transition:transform 0.2s,box-shadow 0.2s;letter-spacing:1px;
}
.register-btn:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(255,140,0,0.5);}
.register-btn .arrow{font-size:1.3rem;transition:transform 0.2s;}
.register-btn:hover .arrow{transform:translateX(4px);}
.footer-text{
  margin-top:36px;font-size:0.78rem;color:rgba(255,255,255,0.3);
  position:relative;z-index:1;letter-spacing:0.5px;
}
.lang-toggle{
  position:absolute;top:16px;right:16px;z-index:10;display:flex;
}
.lang-btn{
  padding:5px 14px;border:2px solid rgba(255,165,0,0.5);font-size:0.78rem;
  font-weight:600;cursor:pointer;background:transparent;color:rgba(255,255,255,0.7);transition:all 0.2s;
}
.lang-btn.active{background:rgba(255,165,0,0.25);color:#FFD700;border-color:#FF8C00;}
.lang-btn:first-child{border-radius:20px 0 0 20px;}
.lang-btn:last-child{border-radius:0 20px 20px 0;}
@media(max-width:768px){
  .hero{padding:60px 16px 40px;}
  .hindi-title{font-size:2rem;}
  .eng-title{font-size:1.3rem;letter-spacing:2px;}
  .om{font-size:3.2rem;}
  .verse{font-size:1rem;max-width:90vw;padding:0 8px;}
  .info-cards{gap:10px;max-width:95vw;padding:0 4px;}
  .info-card{min-width:120px;padding:14px 14px;}
  .info-card .value{font-size:0.85rem;}
  .info-card .label{font-size:0.68rem;}
  .register-btn{padding:16px 36px;font-size:1.05rem;}
  .footer-text{font-size:0.75rem;padding:0 16px;}
}
@media(max-width:480px){
  .hero{padding:56px 12px 32px;}
  .hindi-title{font-size:1.7rem;}
  .eng-title{font-size:1.1rem;letter-spacing:1.5px;margin-bottom:14px;}
  .om{font-size:2.8rem;}
  .diya{font-size:1.6rem;}
  .diya-row{gap:14px;margin-bottom:14px;}
  .verse{font-size:0.9rem;line-height:1.6;margin-bottom:20px;}
  .divider{width:80px;margin-bottom:18px;}
  .info-cards{flex-direction:column;align-items:stretch;gap:10px;width:100%;}
  .info-card{min-width:unset;display:flex;align-items:center;gap:12px;text-align:left;padding:12px 16px;}
  .info-card .icon{font-size:1.4rem;margin-bottom:0;flex-shrink:0;}
  .info-card .label{margin-bottom:2px;}
  .register-btn{padding:15px 28px;font-size:0.95rem;width:100%;justify-content:center;max-width:320px;}
  .footer-text{font-size:0.72rem;margin-top:24px;padding:0 12px;}
  .lang-toggle{top:12px;right:12px;}
  .lang-btn{padding:4px 10px;font-size:0.72rem;}
}
@media(max-width:360px){
  .hindi-title{font-size:1.5rem;}
  .eng-title{font-size:1rem;}
  .om{font-size:2.4rem;}
  .verse{font-size:0.82rem;}
  .register-btn{padding:14px 20px;font-size:0.88rem;}
  .info-card .value{font-size:0.8rem;}
}
</style>
</head>
<body>
<div class="hero">
  <div class="lang-toggle">
    <button class="lang-btn active" onclick="setLang('en')">English</button>
    <button class="lang-btn" onclick="setLang('hi')">हिन्दी</button>
  </div>

  <div class="diya-row">
    <span class="diya">&#x1F6D5;</span>
    <span class="diya">&#x1F52F;</span>
    <span class="diya">&#x1F6D5;</span>
  </div>

  <div class="om">&#x1F549;</div>
  <div class="hindi-title">श्रीमद् भागवत कथा</div>
  <div class="eng-title">Shrimad Bhagwat Katha</div>
  <div class="divider"></div>

  <div class="verse" data-en="Surrender unto the Lord with all your heart,<br>and He shall guide your path with His divine grace." data-hi="सर्वधर्मान्परित्यज्य मामेकं शरणं व्रज।<br>अहं त्वां सर्वपापेभ्यो मोक्षयिष्यामि मा शुचः॥">
    Surrender unto the Lord with all your heart,<br>and He shall guide your path with His divine grace.
  </div>

  <div class="info-cards">
    <div class="info-card">
      <div class="icon">&#x1F4C5;</div>
      <div class="label" data-en="Timing" data-hi="समय">Timing</div>
      <div class="value" data-en="4:00 PM &ndash; 7:00 PM<br>Daily" data-hi="शाम 4:00 &ndash; 7:00 बजे<br>प्रतिदिन">4:00 PM &ndash; 7:00 PM<br>Daily</div>
    </div>
    <div class="info-card">
      <div class="icon">&#x1F4CD;</div>
      <div class="label" data-en="Venue" data-hi="स्थान">Venue</div>
      <div class="value" data-en="Gate No 3<br>Shri Adya Katyayani<br>Shakti Peeth Mandir<br>Chhatarpur, New Delhi" data-hi="गेट नं. 3<br>श्री आद्य कात्यायनी<br>शक्ति पीठ मंदिर<br>छतरपुर, नई दिल्ली">Gate No 3<br>Shri Adya Katyayani<br>Shakti Peeth Mandir<br>Chhatarpur, New Delhi</div>
    </div>
    <div class="info-card">
      <div class="icon">&#x1F64F;</div>
      <div class="label" data-en="Entry" data-hi="प्रवेश">Entry</div>
      <div class="value" data-en="Free Entry<br>with QR Pass" data-hi="निःशुल्क प्रवेश<br>QR पास के साथ">Free Entry<br>with QR Pass</div>
    </div>
  </div>

  <a href="/register" class="register-btn" data-en="&#x1F64F; Register for Entry Pass <span class='arrow'>&rarr;</span>" data-hi="&#x1F64F; प्रवेश पास के लिए पंजीकरण करें <span class='arrow'>&rarr;</span>">
    &#x1F64F; Register for Entry Pass <span class="arrow">&rarr;</span>
  </a>

  <div class="footer-text" data-en="Kindly reach 15-30 minutes before the Katha begins" data-hi="कृपया कथा शुरू होने से 15-30 मिनट पहले पहुँचें">
    Kindly reach 15-30 minutes before the Katha begins
  </div>
</div>
<script>
function setLang(lang){
  localStorage.setItem('katha_lang',lang);
  document.querySelectorAll('[data-en]').forEach(function(el){
    el.innerHTML=el.getAttribute('data-'+lang)||el.getAttribute('data-en');
  });
  document.querySelectorAll('.lang-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelector('.lang-btn[onclick="setLang(\\''+lang+'\\')"]').classList.add('active');
}
(function(){var l=localStorage.getItem('katha_lang')||'en';setLang(l);})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

SCANNER_PASSWORD = os.environ.get("SCANNER_PASSWORD", "Admin")


@app.route("/")
def index():
    return render_template_string(HOME_HTML)


@app.route("/scanner", methods=["GET", "POST"])
def scanner_page():
    if request.method == "POST":
        if request.form.get("password") == SCANNER_PASSWORD:
            resp = make_response(render_template_string(SCANNER_HTML))
            resp.set_cookie("scanner_auth", hashlib.sha256(SCANNER_PASSWORD.encode()).hexdigest(), max_age=86400)
            return resp
        return render_template_string(SCANNER_LOGIN_HTML, error="Incorrect password")

    cookie = request.cookies.get("scanner_auth", "")
    if cookie == hashlib.sha256(SCANNER_PASSWORD.encode()).hexdigest():
        return render_template_string(SCANNER_HTML)

    return render_template_string(SCANNER_LOGIN_HTML, error=None)


@app.route("/register", methods=["GET"])
def register_form():
    date_str = today_ist()
    date_display = now_ist().strftime("%A, %d %B %Y")

    if not is_registration_open():
        return render_template_string(CLOSED_HTML,
            current_time=now_ist().strftime("%I:%M %p IST"))

    phone = request.args.get("phone", "").strip()
    registrations = load_registrations(date_str)

    if phone and phone in registrations:
        reg = registrations[phone]
        return render_template_string(ALREADY_REGISTERED_HTML,
            name=reg["name"], attendees=reg["attendees"], phone=phone, date_str=date_str)

    spots_left = max(0, TOTAL_CAPACITY - total_attendees_registered(registrations))
    return render_template_string(REGISTER_HTML,
        date_display=date_display, spots_left=spots_left, total=TOTAL_CAPACITY,
        error=None, prev={"name": "", "phone": phone, "attendees": "", "invitee_name": ""})


@app.route("/register", methods=["POST"])
def register_submit():
    date_str = today_ist()
    date_display = now_ist().strftime("%A, %d %B %Y")

    if not is_registration_open():
        return render_template_string(CLOSED_HTML,
            current_time=now_ist().strftime("%I:%M %p IST"))

    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    attendees = int(request.form.get("attendees", "1").strip())
    invitee_name = request.form.get("invitee_name", "").strip()

    prev = {"name": name, "phone": phone, "attendees": str(attendees), "invitee_name": invitee_name}
    registrations = load_registrations(date_str)
    spots_left = max(0, TOTAL_CAPACITY - total_attendees_registered(registrations))

    if phone in registrations:
        reg = registrations[phone]
        return render_template_string(ALREADY_REGISTERED_HTML,
            name=reg["name"], attendees=reg["attendees"], phone=phone, date_str=date_str)

    if not name or not phone or not invitee_name:
        return render_template_string(REGISTER_HTML,
            date_display=date_display, spots_left=spots_left, total=TOTAL_CAPACITY,
            error="All fields are mandatory.", prev=prev)

    if len(phone) != 10 or not phone.isdigit():
        return render_template_string(REGISTER_HTML,
            date_display=date_display, spots_left=spots_left, total=TOTAL_CAPACITY,
            error="Please enter a valid 10-digit phone number.", prev=prev)

    if attendees < 1 or attendees > 5:
        return render_template_string(REGISTER_HTML,
            date_display=date_display, spots_left=spots_left, total=TOTAL_CAPACITY,
            error="Number of attendees must be between 1 and 5.", prev=prev)

    if spots_left < attendees:
        return render_template_string(REGISTER_HTML,
            date_display=date_display, spots_left=spots_left, total=TOTAL_CAPACITY,
            error=f"Only {spots_left} spots left, but you requested {attendees}.", prev=prev)

    available = get_next_available_tickets(attendees, date_str, registrations)
    if len(available) < attendees:
        return render_template_string(REGISTER_HTML,
            date_display=date_display, spots_left=spots_left, total=TOTAL_CAPACITY,
            error="Not enough passes available.", prev=prev)

    tickets_data = []
    for serial, ticket_id in available:
        tickets_data.append({"serial": serial, "ticket_id": ticket_id})

    registrations[phone] = {
        "name": name,
        "attendees": attendees,
        "invitee_name": invitee_name,
        "tickets": tickets_data,
        "registered_at": now_ist().strftime("%Y-%m-%d %I:%M %p"),
    }
    save_registrations(date_str, registrations)

    serials = [t["serial"] for t in tickets_data]
    print(f"Registered: {name} ({phone}) -> {attendees} pass(es) [{date_str}]", flush=True)
    sheet_append_registration(date_str, name, phone, attendees, invitee_name, serials)

    tickets_json = json.dumps(tickets_data)
    return render_template_string(SUCCESS_HTML,
        name=name, phone=phone, tickets=tickets_data, attendees=attendees,
        invitee_name=invitee_name, date_str=date_str, date_display=date_display,
        tickets_json=tickets_json)


MY_PASSES_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Passes | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#1a0a00;background:radial-gradient(ellipse at 50% 30%,#3a1a00 0%,#1a0a00 70%);
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
}
.card{
  background:rgba(255,255,255,0.07);border-radius:20px;padding:36px 28px;
  width:100%;max-width:420px;box-shadow:0 20px 60px rgba(0,0,0,0.5);
  border-top:5px solid #FF8C00;backdrop-filter:blur(12px);
  border:1px solid rgba(255,165,0,0.15);text-align:center;
}
.lang-toggle{display:flex;justify-content:flex-end;margin-bottom:12px;}
.lang-btn{
  padding:5px 14px;border:2px solid rgba(255,165,0,0.5);font-size:0.78rem;
  font-weight:600;cursor:pointer;background:transparent;color:rgba(255,255,255,0.7);transition:all 0.2s;
}
.lang-btn.active{background:rgba(255,165,0,0.25);color:#FFD700;border-color:#FF8C00;}
.lang-btn:first-child{border-radius:20px 0 0 20px;}
.lang-btn:last-child{border-radius:0 20px 20px 0;}
.icon{font-size:3rem;margin-bottom:12px;}
h1{font-size:1.3rem;color:#FFD700;margin-bottom:6px;}
.sub{font-size:0.85rem;color:rgba(255,255,255,0.5);margin-bottom:24px;}
.form-group{margin-bottom:16px;text-align:left;}
.form-group label{display:block;font-size:0.85rem;font-weight:600;color:rgba(255,255,255,0.75);margin-bottom:6px;}
.form-group input{
  width:100%;padding:14px;border:2px solid rgba(255,165,0,0.3);border-radius:10px;
  font-size:1.1rem;color:#fff;background:rgba(255,255,255,0.06);outline:none;
  text-align:center;letter-spacing:2px;
}
.form-group input::placeholder{color:rgba(255,255,255,0.3);letter-spacing:0;}
.form-group input:focus{border-color:#FF8C00;background:rgba(255,255,255,0.1);}
.submit-btn{
  width:100%;padding:14px;background:linear-gradient(135deg,#FF8C00,#CC5500,#8B1A1A);
  color:#fff;font-size:1rem;font-weight:700;border:none;border-radius:12px;cursor:pointer;
  box-shadow:0 4px 20px rgba(255,140,0,0.3);transition:transform 0.15s;
}
.submit-btn:hover{transform:translateY(-1px);}
.error-msg{
  background:rgba(200,30,30,0.15);border:1px solid rgba(255,100,100,0.3);color:#ff8a8a;
  padding:12px;border-radius:10px;font-size:0.9rem;margin-bottom:18px;
}
.back-link{display:block;margin-top:20px;color:#FFD700;font-size:0.85rem;text-decoration:none;}
.back-link:hover{text-decoration:underline;}
</style>
</head>
<body>
<div class="card">
  <div class="lang-toggle">
    <button class="lang-btn active" onclick="setLang('en')">English</button>
    <button class="lang-btn" onclick="setLang('hi')">हिन्दी</button>
  </div>
  <div class="icon">&#x1F4F1;</div>
  <h1 data-en="View My Passes" data-hi="मेरे पास देखें">View My Passes</h1>
  <div class="sub" data-en="Enter your registered phone number to view your QR passes" data-hi="अपने QR पास देखने के लिए पंजीकृत फ़ोन नंबर दर्ज करें">Enter your registered phone number to view your QR passes</div>
  {% if error %}<div class="error-msg">{{ error }}</div>{% endif %}
  <form method="GET" action="/my-passes">
    <div class="form-group">
      <label data-en="Phone Number" data-hi="फ़ोन नंबर">Phone Number</label>
      <input type="tel" name="phone" required placeholder="e.g. 9876543210" pattern="[0-9]{10}" value="{{ phone or '' }}">
    </div>
    <button type="submit" class="submit-btn" data-en="&#x1F50D; View Passes" data-hi="&#x1F50D; पास देखें">&#x1F50D; View Passes</button>
  </form>
  <a href="/register" class="back-link" data-en="&larr; Back to Registration" data-hi="&larr; पंजीकरण पर वापस जाएं">&larr; Back to Registration</a>
</div>
<script>
function setLang(lang){
  localStorage.setItem('katha_lang',lang);
  document.querySelectorAll('[data-en]').forEach(function(el){el.innerHTML=el.getAttribute('data-'+lang)||el.getAttribute('data-en');});
  document.querySelectorAll('.lang-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelector('.lang-btn[onclick="setLang(\\''+lang+'\\')"]').classList.add('active');
}
(function(){var l=localStorage.getItem('katha_lang')||'en';setLang(l);})();
</script>
</body>
</html>
"""

UPDATE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Update Registration | Shrimad Bhagwat Katha</title>
<link href="https://fonts.googleapis.com/css2?family=Tiro+Devanagari+Hindi&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#1a0a00;min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:20px;position:relative;overflow-x:hidden;
}
body::before{
  content:'';position:fixed;top:0;left:0;right:0;bottom:0;
  background:radial-gradient(ellipse at 50% 20%,rgba(255,140,0,0.12) 0%,transparent 60%),
             radial-gradient(ellipse at 80% 80%,rgba(139,26,26,0.1) 0%,transparent 50%);
  pointer-events:none;
}
.page{position:relative;z-index:1;width:100%;max-width:560px;}
.lang-toggle{display:flex;justify-content:flex-end;margin-bottom:10px;}
.lang-btn{
  padding:6px 16px;border:1.5px solid rgba(255,165,0,0.4);font-size:0.75rem;
  font-weight:600;cursor:pointer;background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.6);
  transition:all 0.2s;backdrop-filter:blur(8px);
}
.lang-btn.active{background:rgba(255,165,0,0.2);color:#FFD700;border-color:#FF8C00;}
.lang-btn:first-child{border-radius:20px 0 0 20px;}
.lang-btn:last-child{border-radius:0 20px 20px 0;}
.card{
  background:linear-gradient(145deg,rgba(42,26,10,0.9),rgba(26,10,0,0.95));
  border-radius:24px;padding:32px 24px;
  box-shadow:0 24px 64px rgba(0,0,0,0.6),inset 0 1px 0 rgba(255,165,0,0.1);
  border:1px solid rgba(255,165,0,0.12);position:relative;overflow:hidden;
}
.card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:4px;
  background:linear-gradient(90deg,#8B1A1A,#CC5500,#FF8C00,#FFD700,#FF8C00,#CC5500,#8B1A1A);
}
.card-header{text-align:center;margin-bottom:20px;}
.card-header .edit-icon{font-size:2rem;margin-bottom:6px;filter:drop-shadow(0 0 10px rgba(255,140,0,0.3));}
.card-header h1{font-family:'Playfair Display',serif;font-size:1.3rem;color:#FFD700;margin-bottom:4px;}
.card-header .sub{font-size:0.82rem;color:rgba(255,255,255,0.45);letter-spacing:0.5px;}
.divider{width:60px;height:2px;margin:10px auto;background:linear-gradient(90deg,transparent,#FF8C00,transparent);}
.current-info{
  background:rgba(255,165,0,0.06);border:1px solid rgba(255,165,0,0.15);border-radius:14px;
  padding:16px;margin-bottom:20px;
}
.current-info .info-title{
  font-size:0.7rem;text-transform:uppercase;letter-spacing:1px;
  color:rgba(255,255,255,0.35);margin-bottom:10px;
}
.current-info .row{
  display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;border-bottom:1px solid rgba(255,165,0,0.06);
}
.current-info .row:last-child{border-bottom:none;}
.current-info .label{
  font-size:0.82rem;color:rgba(255,255,255,0.5);display:flex;align-items:center;gap:6px;
}
.current-info .label .ic{font-size:0.85rem;opacity:0.6;}
.current-info .value{font-size:0.88rem;color:#FFD700;font-weight:600;}
.error-msg{
  background:rgba(200,30,30,0.12);border:1px solid rgba(255,100,100,0.25);color:#ff8a8a;
  padding:12px 16px;border-radius:12px;font-size:0.88rem;margin-bottom:16px;text-align:center;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.success-msg{
  background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.25);color:#6ee7b7;
  padding:12px 16px;border-radius:12px;font-size:0.88rem;margin-bottom:16px;text-align:center;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.form-group{margin-bottom:14px;}
.form-group label{
  display:flex;align-items:center;gap:6px;
  font-size:0.82rem;font-weight:600;color:rgba(255,255,255,0.7);margin-bottom:6px;
}
.form-group label .field-icon{font-size:0.9rem;opacity:0.7;}
.form-group input,.form-group select{
  width:100%;padding:13px 14px;border:1.5px solid rgba(255,165,0,0.2);border-radius:12px;
  font-size:0.95rem;color:#fff;background:rgba(255,255,255,0.04);
  transition:all 0.25s ease;outline:none;
}
.form-group select{color:rgba(255,255,255,0.8);-webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%23FFD700' viewBox='0 0 16 16'%3E%3Cpath d='M1.5 5.5l6.5 6.5 6.5-6.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 14px center;padding-right:36px;
}
.form-group select option{background:#2a1a0a;color:#fff;}
.form-group input:focus,.form-group select:focus{
  border-color:#FF8C00;background:rgba(255,255,255,0.07);
  box-shadow:0 0 0 3px rgba(255,140,0,0.1);
}
.submit-btn{
  width:100%;padding:15px;margin-top:6px;
  background:linear-gradient(135deg,#FF8C00,#CC5500,#8B1A1A);
  color:#fff;font-size:1rem;font-weight:700;border:none;border-radius:14px;
  cursor:pointer;position:relative;overflow:hidden;
  box-shadow:0 6px 24px rgba(255,140,0,0.3);transition:all 0.2s ease;
}
.submit-btn::after{
  content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,0.1),transparent);
  transition:left 0.5s ease;
}
.submit-btn:hover::after{left:100%;}
.submit-btn:hover{transform:translateY(-2px);box-shadow:0 10px 32px rgba(255,140,0,0.45);}
.note{
  font-size:0.75rem;color:rgba(255,255,255,0.3);text-align:center;margin-top:14px;
  padding:10px;background:rgba(255,255,255,0.02);border-radius:10px;line-height:1.5;
}
.back-link{
  display:flex;align-items:center;justify-content:center;gap:6px;
  margin-top:16px;padding:10px;color:rgba(255,215,0,0.7);font-size:0.84rem;
  text-decoration:none;font-weight:500;border-radius:10px;transition:all 0.2s;
}
.back-link:hover{color:#FFD700;background:rgba(255,165,0,0.08);}
@media(max-width:480px){
  .card{padding:24px 18px;border-radius:20px;}
  .card-header h1{font-size:1.15rem;}
}
</style>
</head>
<body>
<div class="page">
<div class="lang-toggle">
  <button class="lang-btn active" onclick="setLang('en')">English</button>
  <button class="lang-btn" onclick="setLang('hi')">हिन्दी</button>
</div>
<div class="card">
  <div class="card-header">
    <div class="edit-icon">&#x270F;&#xFE0F;</div>
    <h1 data-en="Update Registration" data-hi="पंजीकरण अपडेट करें">Update Registration</h1>
    <div class="sub" data-en="Modify your details below" data-hi="नीचे अपना विवरण बदलें">Modify your details below</div>
    <div class="divider"></div>
  </div>

  {% if error %}<div class="error-msg">&#x26A0; {{ error }}</div>{% endif %}
  {% if success %}<div class="success-msg">&#x2714; {{ success }}</div>{% endif %}

  <div class="current-info">
    <div class="info-title" data-en="Current Registration" data-hi="वर्तमान पंजीकरण">Current Registration</div>
    <div class="row"><span class="label"><span class="ic">&#x1F464;</span> <span data-en="Name" data-hi="नाम">Name</span></span><span class="value">{{ reg.name }}</span></div>
    <div class="row"><span class="label"><span class="ic">&#x1F4F1;</span> <span data-en="Phone" data-hi="फ़ोन">Phone</span></span><span class="value">{{ phone }}</span></div>
    <div class="row"><span class="label"><span class="ic">&#x1F465;</span> <span data-en="Attendees" data-hi="उपस्थित">Attendees</span></span><span class="value">{{ reg.attendees }}</span></div>
    <div class="row"><span class="label"><span class="ic">&#x1F64F;</span> <span data-en="Invitee" data-hi="निमंत्रणकर्ता">Invitee</span></span><span class="value">{{ reg.invitee_name }}</span></div>
  </div>

  <form method="POST" action="/update-registration">
    <input type="hidden" name="phone" value="{{ phone }}">
    <div class="form-group">
      <label><span class="field-icon">&#x1F464;</span> <span data-en="Full Name" data-hi="पूरा नाम">Full Name</span></label>
      <input type="text" name="name" required value="{{ reg.name }}">
    </div>
    <div class="form-group">
      <label><span class="field-icon">&#x1F465;</span> <span data-en="Number of Attendees (max 5)" data-hi="उपस्थित लोगों की संख्या (अधिकतम 5)">Number of Attendees (max 5)</span></label>
      <select name="attendees" required>
        <option value="1" {{ 'selected' if reg.attendees|int == 1 }}>1</option>
        <option value="2" {{ 'selected' if reg.attendees|int == 2 }}>2</option>
        <option value="3" {{ 'selected' if reg.attendees|int == 3 }}>3</option>
        <option value="4" {{ 'selected' if reg.attendees|int == 4 }}>4</option>
        <option value="5" {{ 'selected' if reg.attendees|int == 5 }}>5</option>
      </select>
    </div>
    <div class="form-group">
      <label><span class="field-icon">&#x1F64F;</span> <span data-en="Invitee Name" data-hi="निमंत्रणकर्ता का नाम">Invitee Name</span></label>
      <select name="invitee_name" required>
        <option value="Arun Gupta Ji" {{ 'selected' if reg.invitee_name == 'Arun Gupta Ji' }}>Arun Gupta Ji</option>
        <option value="Sheena Aron Ji" {{ 'selected' if reg.invitee_name == 'Sheena Aron Ji' }}>Sheena Aron Ji</option>
        <option value="Ankit Ji" {{ 'selected' if reg.invitee_name == 'Ankit Ji' }}>Ankit Ji</option>
        <option value="Sanjay Ji" {{ 'selected' if reg.invitee_name == 'Sanjay Ji' }}>Sanjay Ji</option>
        <option value="Rama Shankar Ji" {{ 'selected' if reg.invitee_name == 'Rama Shankar Ji' }}>Rama Shankar Ji</option>
      </select>
    </div>
    <button type="submit" class="submit-btn" data-en="&#x2714; Update Registration" data-hi="&#x2714; पंजीकरण अपडेट करें">&#x2714; Update Registration</button>
  </form>
  <div class="note" data-en="Reducing attendees will cancel extra passes. Increasing will assign new ones if seats are available." data-hi="उपस्थित कम करने से अतिरिक्त पास रद्द होंगे। बढ़ाने से नए पास मिलेंगे यदि सीटें उपलब्ध हैं।">Reducing attendees will cancel extra passes. Increasing will assign new ones if seats are available.</div>
  <a href="/my-passes?phone={{ phone }}" class="back-link" data-en="&larr; Back to My Passes" data-hi="&larr; मेरे पास पर वापस जाएं">&larr; Back to My Passes</a>
</div>
</div>
<script>
function setLang(lang){
  localStorage.setItem('katha_lang',lang);
  document.querySelectorAll('[data-en]').forEach(function(el){el.innerHTML=el.getAttribute('data-'+lang)||el.getAttribute('data-en');});
  document.querySelectorAll('.lang-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelector('.lang-btn[onclick="setLang(\\''+lang+'\\')"]').classList.add('active');
}
(function(){var l=localStorage.getItem('katha_lang')||'en';setLang(l);})();
</script>
</body>
</html>
"""


@app.route("/my-passes")
def my_passes():
    phone = request.args.get("phone", "").strip()
    date_str = request.args.get("date", today_ist())

    if not phone:
        return render_template_string(MY_PASSES_HTML, error=None, phone="")

    if len(phone) != 10 or not phone.isdigit():
        return render_template_string(MY_PASSES_HTML, error="Please enter a valid 10-digit phone number.", phone=phone)

    registrations = load_registrations(date_str)
    if phone not in registrations:
        return render_template_string(MY_PASSES_HTML, error="No registration found for this phone number today.", phone=phone)

    reg = registrations[phone]
    date_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %d %B %Y")
    tickets_json = json.dumps(reg["tickets"])
    return render_template_string(SUCCESS_HTML,
        name=reg["name"], phone=phone, tickets=reg["tickets"], attendees=reg["attendees"],
        invitee_name=reg["invitee_name"], date_str=date_str, date_display=date_display,
        tickets_json=tickets_json)


@app.route("/update-registration", methods=["GET", "POST"])
def update_registration():
    date_str = today_ist()
    registrations = load_registrations(date_str)

    if request.method == "GET":
        phone = request.args.get("phone", "").strip()
        if not phone or phone not in registrations:
            return redirect("/my-passes")
        reg = registrations[phone]
        return render_template_string(UPDATE_HTML, reg=reg, phone=phone, error=None, success=None)

    phone = request.form.get("phone", "").strip()
    if phone not in registrations:
        return redirect("/my-passes")

    reg = registrations[phone]
    old_attendees = int(reg["attendees"])

    new_name = request.form.get("name", "").strip()
    new_attendees = int(request.form.get("attendees", str(old_attendees)).strip())
    new_invitee = request.form.get("invitee_name", reg["invitee_name"]).strip()

    if new_attendees < 1 or new_attendees > 5:
        return render_template_string(UPDATE_HTML, reg=reg, phone=phone,
            error="Attendees must be between 1 and 5.", success=None)

    if new_attendees > old_attendees:
        extra_needed = new_attendees - old_attendees
        spots_left = TOTAL_CAPACITY - total_attendees_registered(registrations)
        if spots_left < extra_needed:
            return render_template_string(UPDATE_HTML, reg=reg, phone=phone,
                error=f"Only {spots_left} spots left. Cannot add {extra_needed} more.", success=None)
        available = get_next_available_tickets(extra_needed, date_str, registrations)
        if len(available) < extra_needed:
            return render_template_string(UPDATE_HTML, reg=reg, phone=phone,
                error="Not enough passes available.", success=None)
        for serial, ticket_id in available:
            reg["tickets"].append({"serial": serial, "ticket_id": ticket_id})
    elif new_attendees < old_attendees:
        reg["tickets"] = reg["tickets"][:new_attendees]

    if new_name:
        reg["name"] = new_name
    reg["attendees"] = new_attendees
    reg["invitee_name"] = new_invitee
    registrations[phone] = reg
    save_registrations(date_str, registrations)
    print(f"Updated registration: {reg['name']} ({phone}) -> {new_attendees} attendees [{date_str}]", flush=True)
    ticket_serials = [t["serial"] for t in reg["tickets"]]
    sheet_append_update(date_str, reg["name"], phone, old_attendees, new_attendees, reg["invitee_name"], ticket_serials)

    return render_template_string(UPDATE_HTML, reg=reg, phone=phone,
        error=None, success="Registration updated successfully!")


@app.route("/qr-image/<date_str>/<int:serial>")
def serve_qr_image(date_str, serial):
    if serial < 1 or serial > TOTAL_CAPACITY:
        return "Not found", 404
    ticket_id = generate_ticket_id(date_str, serial)
    img_bytes = generate_qr_bytes(ticket_id)
    return img_bytes, 200, {
        "Content-Type": "image/png",
        "Cache-Control": "public, max-age=86400",
    }


@app.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json()
    ticket_id = data.get("ticket_id", "").strip()
    date_str = today_ist()
    valid_tickets = get_valid_tickets_for_date(date_str)

    if ticket_id not in valid_tickets:
        if ticket_id.startswith("SBK-") or ticket_id.startswith("EVT-"):
            return jsonify({"status": "wrong_day", "message": "This ticket is not for today"})
        return jsonify({"status": "invalid", "message": "Ticket not recognized"})

    used_tickets = load_used_tickets(date_str)
    if ticket_id in used_tickets:
        return jsonify({
            "status": "already_used",
            "used_at": used_tickets[ticket_id]["used_at"],
            "serial": valid_tickets[ticket_id]
        })

    serial = valid_tickets[ticket_id]
    now_str = now_ist().strftime("%I:%M %p")
    used_tickets[ticket_id] = {"serial": serial, "used_at": now_str}
    save_used_tickets(date_str, used_tickets)

    reg_name, reg_phone = find_registration_by_ticket(date_str, ticket_id)
    sheet_append_checkin(date_str, serial, ticket_id, reg_name, reg_phone)
    _append_scan_log(date_str, serial, reg_name, now_str, True)

    return jsonify({
        "status": "ok", "serial": serial,
        "entry_number": len(used_tickets), "total": len(valid_tickets),
    })


@app.route("/api/recent-scans")
def recent_scans():
    date_str = today_ist()
    scans = _load_scan_log(date_str)
    return jsonify({"scans": scans[:20]})


@app.route("/api/stats")
def stats():
    date_str = today_ist()
    registrations = load_registrations(date_str)
    used_tickets = load_used_tickets(date_str)
    total_att = total_attendees_registered(registrations)
    return jsonify({
        "total": TOTAL_CAPACITY, "used": len(used_tickets),
        "remaining": TOTAL_CAPACITY - len(used_tickets),
        "registered_people": total_att, "registered_groups": len(registrations),
        "spots_left": TOTAL_CAPACITY - total_att, "date": date_str,
    })


@app.route("/api/registrations")
def api_registrations():
    date_str = request.args.get("date", today_ist())
    registrations = load_registrations(date_str)
    return jsonify({
        "date": date_str, "total_registered": len(registrations),
        "total_capacity": TOTAL_CAPACITY, "registrations": registrations,
    })


if __name__ == "__main__":
    date_str = today_ist()
    regs = load_registrations(date_str)
    print("\n" + "=" * 50)
    print("  SHRIMAD BHAGWAT KATHA — CHECK-IN SERVER")
    print("=" * 50)
    print(f"  Today:        {date_str}")
    print(f"  Scanner:      http://localhost:5000")
    print(f"  Registration: http://localhost:5000/register")
    print(f"  Storage:      {'Redis' if USE_REDIS else 'Local JSON'}")
    print(f"  Google Sheets:{'Yes' if GOOGLE_SHEETS_ENABLED else 'No'}")
    print(f"  Registered:   {len(regs)} / {TOTAL_CAPACITY}")
    print("=" * 50 + "\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
