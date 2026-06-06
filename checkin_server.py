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
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

TOTAL_CAPACITY = 250
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
    t = now_ist()
    if t.hour >= 2:
        return True
    return False

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
    return sum(r["attendees"] for r in registrations.values())

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
async function onScanSuccess(t){if(!scanning)return;scanning=false;scanner.pause(true);try{const r=await fetch("/api/checkin",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket_id:t})});const d=await r.json();showResult(d,t);refreshStats();}catch(e){showResult({status:"error"},t);}}
function showResult(d,t){const o=document.getElementById("resultOverlay"),i=document.getElementById("resultIcon"),tt=document.getElementById("resultTitle"),dd=document.getElementById("resultDetail"),tid=document.getElementById("resultTicketId");o.className="result-overlay show";tid.textContent=t;if(d.status==="ok"){o.classList.add("valid");i.textContent="\\u2713";tt.textContent="WELCOME!";dd.textContent="Entry #"+d.serial+" \\u2014 "+d.entry_number+" of "+d.total;addLog(t,true,d.serial);}else if(d.status==="already_used"){o.classList.add("invalid");i.textContent="\\u2717";tt.textContent="ALREADY USED";dd.textContent="Scanned at "+d.used_at;addLog(t,false,"DUP");}else if(d.status==="wrong_day"){o.classList.add("invalid");i.textContent="\\u2717";tt.textContent="WRONG DAY";dd.textContent="Not valid today.";addLog(t,false,"WRONG DAY");}else{o.classList.add("unknown");i.textContent="?";tt.textContent="INVALID";dd.textContent="QR not recognized.";addLog(t,false,"INVALID");}}
function dismissResult(){document.getElementById("resultOverlay").className="result-overlay";scanning=true;scanner.resume();}
function addLog(t,ok,info){const c=document.getElementById("logEntries"),time=new Date().toLocaleTimeString(),div=document.createElement("div");div.className="log-entry "+(ok?"ok":"fail");div.innerHTML='<span>'+(ok?"\\u2713":"\\u2717")+' '+t.substring(0,20)+'...</span><span>'+info+' \\u00b7 '+time+'</span>';c.prepend(div);if(c.children.length>20)c.lastChild.remove();}
async function refreshStats(){const r=await fetch("/api/stats"),d=await r.json();document.getElementById("checkedIn").textContent=d.used;document.getElementById("totalTickets").textContent=d.total;document.getElementById("remaining").textContent=d.remaining;}
document.addEventListener("DOMContentLoaded",()=>{refreshStats();initScanner();});
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
    width: 100%; max-width: 440px; box-shadow: 0 20px 60px rgba(139,26,26,0.15);
    border-top: 5px solid #CC5500;
  }
  .card-header { text-align: center; margin-bottom: 24px; }
  .greeting {
    font-family: 'Tiro Devanagari Hindi', serif;
    font-size: 1.8rem; color: #CC5500; font-weight: 700; margin-bottom: 4px;
  }
  .card-header h1 { font-size: 1.4rem; color: #8B1A1A; margin-bottom: 4px; }
  .card-header .sub { font-size: 0.95rem; color: #8B1A1A; font-weight: 600; }
  .accent-bar { width: 60px; height: 3px; background: linear-gradient(90deg, #CC5500, #FFD700); border-radius: 2px; margin: 10px auto; }
  .venue-info {
    background: #FFF8E1; border: 1px solid #FFCC80; border-radius: 12px;
    padding: 12px; margin-bottom: 20px; font-size: 0.82rem; color: #6b4c3b; text-align: center; line-height: 1.6;
  }
  .venue-info strong { color: #8B1A1A; }
  .date-pill {
    display: inline-block; padding: 5px 14px; background: #FFF3E0; color: #BF360C;
    border-radius: 20px; font-size: 0.8rem; font-weight: 600; margin-bottom: 8px;
  }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; font-size: 0.85rem; font-weight: 600; color: #5D4037; margin-bottom: 6px; }
  .form-group input, .form-group select {
    width: 100%; padding: 12px 14px; border: 2px solid #FFCC80; border-radius: 10px;
    font-size: 1rem; color: #3E2723; background: #FFFDE7; transition: border-color 0.2s; outline: none;
  }
  .form-group input:focus, .form-group select:focus { border-color: #CC5500; background: #fff; }
  .submit-btn {
    width: 100%; padding: 14px; background: linear-gradient(135deg, #CC5500, #8B1A1A);
    color: #fff; font-size: 1.05rem; font-weight: 700; border: none; border-radius: 12px;
    cursor: pointer; margin-top: 8px; transition: transform 0.15s, box-shadow 0.15s; letter-spacing: 0.5px;
  }
  .submit-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(204,85,0,0.4); }
  .submit-btn:disabled { background: #bcaaa4; cursor: not-allowed; transform: none; box-shadow: none; }
  .error-msg {
    background: #FBE9E7; border: 1px solid #FFAB91; color: #BF360C;
    padding: 12px; border-radius: 10px; font-size: 0.9rem; margin-bottom: 18px; text-align: center;
  }
  .spots-left { text-align: center; margin-top: 16px; font-size: 0.85rem; color: #8D6E63; }
  .spots-left span { color: #CC5500; font-weight: 700; }
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <div class="greeting">&#x1F64F; JAI MATA DI &#x1F64F;</div>
    <h1>Shrimad Bhagwat Katha</h1>
    <div class="sub">Registration</div>
    <div class="accent-bar"></div>
    <div class="date-pill">{{ date_display }}</div>
  </div>

  <div class="venue-info">
    <strong>Katha Timing:</strong> 4:00 PM to 7:00 PM &mdash; Kindly reach 15-30 min earlier<br>
    <strong>Venue:</strong> Gate No 3, Shri Adya Katyayani Shakti Peeth Mandir, Chhatarpur, New Delhi
  </div>

  {% if error %}<div class="error-msg">{{ error }}</div>{% endif %}

  <form method="POST" action="/register" id="regForm">
    <div class="form-group">
      <label>Full Name *</label>
      <input type="text" name="name" required placeholder="Your full name" value="{{ prev.name or '' }}">
    </div>
    <div class="form-group">
      <label>Phone Number *</label>
      <input type="tel" name="phone" required placeholder="e.g. 9876543210" pattern="[0-9]{10}" title="Enter 10-digit phone number" value="{{ prev.phone or '' }}">
    </div>
    <div class="form-group">
      <label>Number of Attendees * (max 5)</label>
      <select name="attendees" required>
        <option value="">Select</option>
        <option value="1" {{ 'selected' if prev.attendees == '1' }}>1</option>
        <option value="2" {{ 'selected' if prev.attendees == '2' }}>2</option>
        <option value="3" {{ 'selected' if prev.attendees == '3' }}>3</option>
        <option value="4" {{ 'selected' if prev.attendees == '4' }}>4</option>
        <option value="5" {{ 'selected' if prev.attendees == '5' }}>5</option>
      </select>
    </div>
    <div class="form-group">
      <label>Invitee Name *</label>
      <select name="invitee_name" required>
        <option value="">Select</option>
        <option value="Arun Gupta Ji" {{ 'selected' if prev.invitee_name == 'Arun Gupta Ji' }}>Arun Gupta Ji</option>
        <option value="Sheena Aron Ji" {{ 'selected' if prev.invitee_name == 'Sheena Aron Ji' }}>Sheena Aron Ji</option>
        <option value="Ankit Ji" {{ 'selected' if prev.invitee_name == 'Ankit Ji' }}>Ankit Ji</option>
        <option value="Sanjay Ji" {{ 'selected' if prev.invitee_name == 'Sanjay Ji' }}>Sanjay Ji</option>
        <option value="Rama Shankar Ji" {{ 'selected' if prev.invitee_name == 'Rama Shankar Ji' }}>Rama Shankar Ji</option>
      </select>
    </div>
    <button type="submit" class="submit-btn" id="submitBtn">&#x1F64F; REGISTER & GET QR PASS</button>
  </form>
  <div class="spots-left"><span>{{ spots_left }}</span> spots remaining out of {{ total }}</div>
</div>
<script>
document.getElementById('regForm').addEventListener('submit',function(){var b=document.getElementById('submitBtn');b.disabled=true;b.textContent='REGISTERING...';});
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
</style>
</head>
<body>
<div class="card">
  <div class="check-icon">&#10003;</div>
  <div class="greeting">&#x1F64F; JAI MATA DI &#x1F64F;</div>
  <h1>You're Registered!</h1>
  <p class="subtitle">{{ attendees }} QR pass{{ 'es' if attendees|int > 1 else '' }} for today's Katha</p>
  <div class="date-pill">{{ date_display }}</div>

  <div class="venue-info">
    <strong>Timing:</strong> 4:00 PM to 7:00 PM &mdash; Reach 15-30 min earlier<br>
    <strong>Venue:</strong> Gate No 3, Shri Adya Katyayani Shakti Peeth Mandir, Chhatarpur, New Delhi
  </div>

  <div class="qr-section">
    {% for t in tickets %}
    <div class="qr-box">
      <div class="qr-label">Attendee {{ loop.index }} of {{ attendees }} &mdash; Pass #{{ '%03d' % t.serial }}</div>
      <img src="/qr-image/{{ date_str }}/{{ t.serial }}" alt="QR #{{ t.serial }}" id="qr-{{ t.serial }}" crossorigin="anonymous">
      <div class="ticket-id">{{ t.ticket_id }}</div>
      <div class="btn-row">
        <button class="btn-share" onclick="shareOne({{ t.serial }},'{{ '%03d' % t.serial }}')">&#9993; Share</button>
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="btn-all-row">
    <button class="btn-all download-pdf" id="btnPdf" onclick="downloadPDF()">&#128196; Download All as PDF</button>
    <button class="btn-all share" id="btnShareAll" onclick="shareAll()">&#9993; Share All</button>
  </div>

  <div class="ticket-info">
    <div class="row"><span class="label">Registered by</span><span class="value">{{ name }}</span></div>
    <div class="row"><span class="label">Total Attendees</span><span class="value">{{ attendees }}</span></div>
    <div class="row"><span class="label">Invited by</span><span class="value">{{ invitee_name }}</span></div>
    <div class="row"><span class="label">Valid for</span><span class="value">{{ date_display }}</span></div>
  </div>

  <div class="notice">
    Each person must show their own QR at the gate. <strong>Valid today only. One-time use.</strong>
  </div>
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
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(SCANNER_HTML)


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

    spots_left = TOTAL_CAPACITY - total_attendees_registered(registrations)
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
    spots_left = TOTAL_CAPACITY - total_attendees_registered(registrations)

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
        name=name, tickets=tickets_data, attendees=attendees,
        invitee_name=invitee_name, date_str=date_str, date_display=date_display,
        tickets_json=tickets_json)


@app.route("/my-passes")
def my_passes():
    phone = request.args.get("phone", "").strip()
    date_str = request.args.get("date", today_ist())
    date_display_dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_display = date_display_dt.strftime("%A, %d %B %Y")

    registrations = load_registrations(date_str)
    if phone not in registrations:
        return render_template_string(REGISTER_HTML,
            date_display=date_display, spots_left=TOTAL_CAPACITY - total_attendees_registered(registrations),
            total=TOTAL_CAPACITY, error="No registration found for this phone number today.",
            prev={"name": "", "phone": phone, "attendees": "", "invitee_name": ""})

    reg = registrations[phone]
    tickets_json = json.dumps(reg["tickets"])
    return render_template_string(SUCCESS_HTML,
        name=reg["name"], tickets=reg["tickets"], attendees=reg["attendees"],
        invitee_name=reg["invitee_name"], date_str=date_str, date_display=date_display,
        tickets_json=tickets_json)


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

    return jsonify({
        "status": "ok", "serial": serial,
        "entry_number": len(used_tickets), "total": len(valid_tickets),
    })


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
