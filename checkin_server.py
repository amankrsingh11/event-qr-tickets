"""
Event Check-in Server (Vercel-compatible)
- Daily registration form (8 AM – 2 PM IST only)
- Unique QR tickets per day, capacity resets at midnight IST
- One registration per phone number per day
- Google Sheets logging for registrations and check-ins
- Upstash Redis for persistence in serverless environments
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

TOTAL_CAPACITY = 200
TICKET_SECRET = os.environ.get("TICKET_SECRET", "event-qr-2026-secret")
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Time helpers (IST)
# ---------------------------------------------------------------------------

def now_ist():
    return datetime.now(IST)


def today_ist():
    return now_ist().strftime("%Y-%m-%d")


def is_registration_open():
    """Registration open 1:35 AM to 2:00 PM IST (testing window)."""
    t = now_ist()
    if t.hour == 1 and t.minute >= 35:
        return True
    return 2 <= t.hour < 14


# ---------------------------------------------------------------------------
# Daily ticket generation (deterministic per day + serial)
# ---------------------------------------------------------------------------

def generate_ticket_id(date_str, serial):
    raw = f"{date_str}-{serial:03d}-{TICKET_SECRET}"
    short_hash = hashlib.sha256(raw.encode()).hexdigest()[:10].upper()
    return f"EVT-{date_str.replace('-', '')}-{serial:03d}-{short_hash}"


def get_valid_tickets_for_date(date_str):
    """Generate the full set of valid ticket IDs for a given date."""
    tickets = {}
    for serial in range(1, TOTAL_CAPACITY + 1):
        tid = generate_ticket_id(date_str, serial)
        tickets[tid] = serial
    return tickets


# ---------------------------------------------------------------------------
# Storage backend — Redis (Vercel) or local JSON files
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
# Google Sheets integration (optional — works without credentials)
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


def sheet_append_registration(date_str, name, phone, attendees, invitee_name, ticket_serials):
    if not GOOGLE_SHEETS_ENABLED:
        return
    try:
        gc = _get_gspread()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet("Registrations")
        time_str = now_ist().strftime("%I:%M %p")
        serials_str = ", ".join(f"#{s:03d}" for s in ticket_serials)
        ws.append_row([date_str, time_str, name, phone, attendees, invitee_name, serials_str])
    except Exception as e:
        print(f"Google Sheets (registration) error: {e}", flush=True)


def sheet_append_checkin(date_str, serial, ticket_id, reg_name, reg_phone):
    if not GOOGLE_SHEETS_ENABLED:
        return
    try:
        gc = _get_gspread()
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet("Check-ins")
        time_str = now_ist().strftime("%I:%M %p")
        ws.append_row([date_str, time_str, f"#{serial:03d}", ticket_id, reg_name, reg_phone])
    except Exception as e:
        print(f"Google Sheets (checkin) error: {e}", flush=True)


# ---------------------------------------------------------------------------
# QR image generation (on-the-fly)
# ---------------------------------------------------------------------------

def generate_qr_bytes(ticket_id):
    img = qrcode.make(ticket_id, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helper functions
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
    """Look up who owns a ticket for the Google Sheets check-in log."""
    registrations = load_registrations(date_str)
    for phone, reg in registrations.items():
        for t in reg.get("tickets", []):
            if t["ticket_id"] == ticket_id:
                return reg["name"], phone
    return "Unknown", "Unknown"


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------

SCANNER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Event Check-in Scanner</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f1a; color: #fff;
    min-height: 100vh; display: flex; flex-direction: column; align-items: center;
  }
  .header {
    padding: 20px; text-align: center; width: 100%;
    background: linear-gradient(135deg, #1a1a2e, #16213e);
    border-bottom: 3px solid #e94560;
  }
  .header h1 { font-size: 1.4rem; letter-spacing: 1px; }
  .header .date-badge {
    display: inline-block; margin-top: 8px; padding: 4px 14px;
    background: #e94560; border-radius: 20px; font-size: 0.8rem; font-weight: 600;
  }
  .stats {
    display: flex; gap: 20px; justify-content: center;
    margin-top: 10px; font-size: 0.85rem; color: #9ca3af;
  }
  .stats span { color: #e94560; font-weight: 700; font-size: 1.1rem; }
  #reader-container { width: 100%; max-width: 500px; margin: 20px auto; padding: 0 16px; }
  #reader { width: 100%; border-radius: 12px; overflow: hidden; }
  .result-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    z-index: 100; flex-direction: column; align-items: center; justify-content: center;
    padding: 40px; text-align: center; animation: fadeIn 0.2s ease;
  }
  .result-overlay.show { display: flex; }
  .result-overlay.valid { background: rgba(16, 185, 129, 0.95); }
  .result-overlay.invalid { background: rgba(239, 68, 68, 0.95); }
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
  <h1>EVENT CHECK-IN</h1>
  <div class="date-badge" id="todayDate"></div>
  <div class="stats">
    Checked in: <span id="checkedIn">0</span> / <span id="totalTickets">0</span>
    &nbsp;&nbsp;|&nbsp;&nbsp;
    Remaining: <span id="remaining">0</span>
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
let scanner, scanning = true;
document.getElementById('todayDate').textContent = new Date().toLocaleDateString('en-IN', {weekday:'long', year:'numeric', month:'long', day:'numeric'});

function initScanner() {
  scanner = new Html5Qrcode("reader");
  scanner.start({facingMode:"environment"},{fps:10,qrbox:{width:250,height:250}},onScanSuccess,()=>{}).catch(()=>{
    document.getElementById("reader").innerHTML='<p style="padding:40px;text-align:center;color:#ef4444;">Camera access denied.<br>Please allow camera and reload.</p>';
  });
}
async function onScanSuccess(ticketId) {
  if(!scanning)return; scanning=false; scanner.pause(true);
  try {
    const resp=await fetch("/api/checkin",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket_id:ticketId})});
    const data=await resp.json(); showResult(data,ticketId); refreshStats();
  } catch(e) { showResult({status:"error",message:"Network error"},ticketId); }
}
function showResult(data,ticketId) {
  const o=document.getElementById("resultOverlay"),i=document.getElementById("resultIcon"),t=document.getElementById("resultTitle"),d=document.getElementById("resultDetail"),tid=document.getElementById("resultTicketId");
  o.className="result-overlay show"; tid.textContent=ticketId;
  if(data.status==="ok"){o.classList.add("valid");i.textContent="✓";t.textContent="WELCOME!";d.textContent="Entry #"+data.serial+" — "+data.entry_number+" of "+data.total;addLog(ticketId,true,data.serial);}
  else if(data.status==="already_used"){o.classList.add("invalid");i.textContent="✕";t.textContent="ALREADY USED";d.textContent="Scanned at "+data.used_at;addLog(ticketId,false,"DUPLICATE");}
  else if(data.status==="wrong_day"){o.classList.add("invalid");i.textContent="✕";t.textContent="WRONG DAY";d.textContent="This ticket is not for today.";addLog(ticketId,false,"WRONG DAY");}
  else{o.classList.add("unknown");i.textContent="?";t.textContent="INVALID TICKET";d.textContent="This QR code is not recognized.";addLog(ticketId,false,"INVALID");}
}
function dismissResult(){document.getElementById("resultOverlay").className="result-overlay";scanning=true;scanner.resume();}
function addLog(ticketId,ok,info){const c=document.getElementById("logEntries"),time=new Date().toLocaleTimeString(),div=document.createElement("div");div.className="log-entry "+(ok?"ok":"fail");div.innerHTML='<span>'+(ok?"✓":"✕")+' '+ticketId.substring(0,20)+'...</span><span>'+info+' · '+time+'</span>';c.prepend(div);if(c.children.length>20)c.lastChild.remove();}
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
<title>Registration Closed</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 40px 28px;
    width: 100%; max-width: 420px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); text-align: center;
  }
  .clock-icon {
    width: 80px; height: 80px; background: #f59e0b; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 20px; font-size: 2.5rem; color: #fff;
  }
  h1 { font-size: 1.5rem; color: #1a1a2e; margin-bottom: 10px; }
  p { color: #6b7280; font-size: 0.95rem; line-height: 1.6; }
  .time-badge {
    display: inline-block; margin-top: 16px; padding: 10px 24px;
    background: #fef3c7; border: 2px solid #f59e0b; border-radius: 12px;
    font-weight: 700; color: #92400e; font-size: 1rem;
  }
  .current-time { margin-top: 20px; font-size: 0.85rem; color: #9ca3af; }
</style>
</head>
<body>
<div class="card">
  <div class="clock-icon">&#9200;</div>
  <h1>Registration Closed</h1>
  <p>Same-day registration is only available between</p>
  <div class="time-badge">8:00 AM &ndash; 2:00 PM IST</div>
  <p style="margin-top:16px">Please come back during registration hours tomorrow.</p>
  <div class="current-time">Current IST time: {{ current_time }}</div>
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
<title>Event Registration</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 36px 28px;
    width: 100%; max-width: 420px; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  }
  .card-header { text-align: center; margin-bottom: 28px; }
  .card-header h1 { font-size: 1.5rem; color: #1a1a2e; margin-bottom: 6px; }
  .card-header p { color: #6b7280; font-size: 0.9rem; }
  .accent-bar { width: 50px; height: 4px; background: #e94560; border-radius: 2px; margin: 12px auto; }
  .date-pill {
    display: inline-block; padding: 6px 16px; background: #eef2ff; color: #4338ca;
    border-radius: 20px; font-size: 0.8rem; font-weight: 600; margin-bottom: 8px;
  }
  .form-group { margin-bottom: 18px; }
  .form-group label { display: block; font-size: 0.85rem; font-weight: 600; color: #374151; margin-bottom: 6px; }
  .form-group input, .form-group select {
    width: 100%; padding: 12px 14px; border: 2px solid #e5e7eb; border-radius: 10px;
    font-size: 1rem; color: #1a1a2e; background: #f9fafb; transition: border-color 0.2s; outline: none;
  }
  .form-group input:focus, .form-group select:focus { border-color: #e94560; background: #fff; }
  .submit-btn {
    width: 100%; padding: 14px; background: linear-gradient(135deg, #e94560, #c73a54);
    color: #fff; font-size: 1.05rem; font-weight: 700; border: none; border-radius: 12px;
    cursor: pointer; margin-top: 8px; transition: transform 0.15s, box-shadow 0.15s; letter-spacing: 0.5px;
  }
  .submit-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(233,69,96,0.4); }
  .submit-btn:active { transform: translateY(0); }
  .submit-btn:disabled { background: #9ca3af; cursor: not-allowed; transform: none; box-shadow: none; }
  .error-msg {
    background: #fef2f2; border: 1px solid #fecaca; color: #dc2626;
    padding: 12px; border-radius: 10px; font-size: 0.9rem; margin-bottom: 18px; text-align: center;
  }
  .spots-left { text-align: center; margin-top: 16px; font-size: 0.85rem; color: #6b7280; }
  .spots-left span { color: #e94560; font-weight: 700; }
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <div class="date-pill">{{ date_display }}</div>
    <h1>Event Registration</h1>
    <div class="accent-bar"></div>
    <p>Fill in your details to get your QR entry pass</p>
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
      <label>Number of Attendees *</label>
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
      <input type="text" name="invitee_name" required placeholder="Who invited you?" value="{{ prev.invitee_name or '' }}">
    </div>
    <button type="submit" class="submit-btn" id="submitBtn">REGISTER & GET QR PASS</button>
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
<title>Registration Successful!</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #064e3b, #065f46, #047857);
    min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: #fff; border-radius: 20px; padding: 36px 28px;
    width: 100%; max-width: 420px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); text-align: center;
  }
  .check-icon {
    width: 70px; height: 70px; background: #10b981; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 20px; font-size: 2.2rem; color: #fff;
  }
  h1 { font-size: 1.5rem; color: #1a1a2e; margin-bottom: 8px; }
  .subtitle { color: #6b7280; font-size: 0.95rem; margin-bottom: 8px; }
  .date-pill {
    display: inline-block; padding: 5px 14px; background: #ecfdf5; color: #065f46;
    border-radius: 20px; font-size: 0.8rem; font-weight: 600; margin-bottom: 16px;
  }
  .qr-section { margin-bottom: 16px; }
  .qr-box {
    background: #f9fafb; border: 2px dashed #d1d5db; border-radius: 16px;
    padding: 20px; margin-bottom: 12px;
  }
  .qr-box img { width: 180px; height: 180px; }
  .qr-label { font-size: 0.85rem; font-weight: 600; color: #374151; margin-bottom: 8px; }
  .ticket-id {
    font-family: monospace; background: #f3f4f6; padding: 6px 12px;
    border-radius: 6px; font-size: 0.8rem; color: #374151; margin-top: 8px; display: inline-block;
  }
  .ticket-info { margin-top: 16px; }
  .ticket-info .row {
    display: flex; justify-content: space-between; padding: 8px 0;
    border-bottom: 1px solid #f3f4f6; font-size: 0.9rem;
  }
  .ticket-info .row:last-child { border-bottom: none; }
  .ticket-info .label { color: #6b7280; }
  .ticket-info .value { color: #1a1a2e; font-weight: 600; }
  .notice {
    background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px;
    padding: 12px; font-size: 0.85rem; color: #166534; margin-top: 20px;
  }
  .btn-row {
    display: flex; gap: 10px; margin-top: 10px; justify-content: center; flex-wrap: wrap;
  }
  .btn-download, .btn-share {
    padding: 10px 20px; border-radius: 10px; font-size: 0.85rem; font-weight: 600;
    border: none; cursor: pointer; text-decoration: none;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-download { background: #1a1a2e; color: #fff; }
  .btn-share { background: #25D366; color: #fff; }
  .btn-all-row {
    display: flex; gap: 10px; justify-content: center; flex-wrap: wrap;
    margin-top: 18px; padding-top: 18px; border-top: 2px solid #e5e7eb;
  }
  .btn-all {
    padding: 12px 24px; border-radius: 12px; font-size: 0.9rem; font-weight: 700;
    border: none; cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
  }
  .btn-all.download { background: linear-gradient(135deg, #1a1a2e, #374151); color: #fff; }
  .btn-all.share { background: linear-gradient(135deg, #25D366, #128C7E); color: #fff; }
  .btn-all:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
</head>
<body>
<div class="card">
  <div class="check-icon">&#10003;</div>
  <h1>You're Registered!</h1>
  <p class="subtitle">{{ attendees }} QR pass{{ 'es' if attendees|int > 1 else '' }} for today</p>
  <div class="date-pill">{{ date_display }}</div>

  <div class="qr-section">
    {% for t in tickets %}
    <div class="qr-box">
      <div class="qr-label">Attendee {{ loop.index }} of {{ attendees }} &mdash; Pass #{{ '%03d' % t.serial }}</div>
      <img src="/qr-image/{{ date_str }}/{{ t.serial }}" alt="QR Pass #{{ t.serial }}" id="qr-{{ t.serial }}" crossorigin="anonymous">
      <div class="ticket-id">{{ t.ticket_id }}</div>
      <div class="btn-row">
        <a class="btn-download" href="/qr-image/{{ date_str }}/{{ t.serial }}" download="pass_{{ '%03d' % t.serial }}_{{ date_str }}.png">&#11015; Download</a>
        <button class="btn-share" onclick="shareOne({{ t.serial }},'{{ '%03d' % t.serial }}','{{ date_str }}')">&#9993; Share</button>
      </div>
    </div>
    {% endfor %}
  </div>

  {% if attendees|int > 1 %}
  <div class="btn-all-row">
    <button class="btn-all download" id="btnDownloadAll" onclick="downloadAll()">&#11015; Download All QR Codes</button>
    <button class="btn-all share" id="btnShareAll" onclick="shareAll()">&#9993; Share All</button>
  </div>
  {% endif %}

  <div class="ticket-info">
    <div class="row"><span class="label">Registered by</span><span class="value">{{ name }}</span></div>
    <div class="row"><span class="label">Total Attendees</span><span class="value">{{ attendees }}</span></div>
    <div class="row"><span class="label">Invited by</span><span class="value">{{ invitee_name }}</span></div>
    <div class="row"><span class="label">Valid for</span><span class="value">{{ date_display }}</span></div>
  </div>

  <div class="notice">
    Download or share your QR {{ 'codes' if attendees|int > 1 else 'code' }}. Each person shows their own QR at the door. <strong>Valid today only.</strong> One-time use!
  </div>
</div>
<script>
const TICKETS = {{ tickets_json | safe }};
const DATE_STR = '{{ date_str }}';

async function fetchBlob(serial) {
  const r = await fetch('/qr-image/' + DATE_STR + '/' + serial);
  return await r.blob();
}

async function shareOne(serial, serialStr, dateStr) {
  try {
    const blob = await fetchBlob(serial);
    const file = new File([blob], 'pass_' + serialStr + '_' + dateStr + '.png', {type:'image/png'});
    if (navigator.share && navigator.canShare({files:[file]})) {
      await navigator.share({title:'Event Pass #'+serialStr, text:'Event entry pass for '+dateStr+'. Show this QR at the door!', files:[file]});
    } else {
      window.open('https://wa.me/?text='+encodeURIComponent('Event pass #'+serialStr+' for '+dateStr+': '+window.location.origin+'/qr-image/'+dateStr+'/'+serial),'_blank');
    }
  } catch(e) {
    window.open('https://wa.me/?text='+encodeURIComponent('Event pass #'+serialStr+': '+window.location.origin+'/qr-image/'+dateStr+'/'+serial),'_blank');
  }
}

async function downloadAll() {
  const btn = document.getElementById('btnDownloadAll');
  btn.disabled = true; btn.textContent = 'Preparing...';
  try {
    const zip = new JSZip();
    for (const t of TICKETS) {
      const blob = await fetchBlob(t.serial);
      const s = String(t.serial).padStart(3,'0');
      zip.file('pass_' + s + '_' + DATE_STR + '.png', blob);
    }
    const content = await zip.generateAsync({type:'blob'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(content);
    a.download = 'all_passes_' + DATE_STR + '.zip';
    a.click(); URL.revokeObjectURL(a.href);
  } catch(e) { alert('Download failed. Please download individually.'); }
  btn.disabled = false; btn.innerHTML = '&#11015; Download All QR Codes';
}

async function shareAll() {
  const btn = document.getElementById('btnShareAll');
  btn.disabled = true; btn.textContent = 'Preparing...';
  try {
    const files = [];
    for (const t of TICKETS) {
      const blob = await fetchBlob(t.serial);
      const s = String(t.serial).padStart(3,'0');
      files.push(new File([blob], 'pass_'+s+'_'+DATE_STR+'.png', {type:'image/png'}));
    }
    if (navigator.share && navigator.canShare({files})) {
      await navigator.share({title:'Event Passes', text:'Event entry passes for '+DATE_STR+'. Show at the door!', files});
    } else {
      alert('Sharing not supported on this device. Please use Download All instead.');
    }
  } catch(e) { if(e.name !== 'AbortError') alert('Share failed. Please use Download All.'); }
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
        tickets_json = json.dumps(reg["tickets"])
        return render_template_string(SUCCESS_HTML,
            name=reg["name"], tickets=reg["tickets"], attendees=reg["attendees"],
            invitee_name=reg["invitee_name"], date_str=date_str, date_display=date_display,
            tickets_json=tickets_json)

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
        tickets_json = json.dumps(reg["tickets"])
        return render_template_string(SUCCESS_HTML,
            name=reg["name"], tickets=reg["tickets"], attendees=reg["attendees"],
            invitee_name=reg["invitee_name"], date_str=date_str, date_display=date_display,
            tickets_json=tickets_json)

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
    serials_str = ", ".join(f"#{s:03d}" for s in serials)
    print(f"Registered: {name} ({phone}) -> {attendees} pass(es): {serials_str} [{date_str}]", flush=True)

    sheet_append_registration(date_str, name, phone, attendees, invitee_name, serials)

    tickets_json = json.dumps(tickets_data)
    return render_template_string(SUCCESS_HTML,
        name=name, tickets=tickets_data, attendees=attendees,
        invitee_name=invitee_name, date_str=date_str, date_display=date_display,
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
        if ticket_id.startswith("EVT-"):
            return jsonify({"status": "wrong_day", "message": "This ticket is not for today"})
        return jsonify({"status": "invalid", "message": "Ticket not recognized"})

    used_tickets = load_used_tickets(date_str)

    if ticket_id in used_tickets:
        return jsonify({
            "status": "already_used",
            "message": "This ticket has already been scanned",
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
        "status": "ok",
        "serial": serial,
        "entry_number": len(used_tickets),
        "total": len(valid_tickets),
    })


@app.route("/api/stats")
def stats():
    date_str = today_ist()
    registrations = load_registrations(date_str)
    used_tickets = load_used_tickets(date_str)
    total_att = total_attendees_registered(registrations)
    return jsonify({
        "total": TOTAL_CAPACITY,
        "used": len(used_tickets),
        "remaining": TOTAL_CAPACITY - len(used_tickets),
        "registered_people": total_att,
        "registered_groups": len(registrations),
        "spots_left": TOTAL_CAPACITY - total_att,
        "date": date_str,
    })


@app.route("/api/registrations")
def api_registrations():
    date_str = request.args.get("date", today_ist())
    registrations = load_registrations(date_str)
    return jsonify({
        "date": date_str,
        "total_registered": len(registrations),
        "total_capacity": TOTAL_CAPACITY,
        "registrations": registrations,
    })


if __name__ == "__main__":
    date_str = today_ist()
    regs = load_registrations(date_str)
    print("\n" + "=" * 50)
    print("  EVENT CHECK-IN SERVER")
    print("=" * 50)
    print(f"  Today:        {date_str}")
    print(f"  Scanner:      http://localhost:5000")
    print(f"  Registration: http://localhost:5000/register")
    print(f"  Storage:      {'Redis (Upstash)' if USE_REDIS else 'Local JSON files'}")
    print(f"  Google Sheets:{'Connected' if GOOGLE_SHEETS_ENABLED else 'Not configured'}")
    print(f"  Registered:   {len(regs)} / {TOTAL_CAPACITY}")
    print(f"  Reg open:     {'YES (8 AM - 2 PM IST)' if is_registration_open() else 'NO'}")
    print("=" * 50 + "\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
