"""
Event Check-in Server
- Registration form for attendees (linked from WhatsApp bot)
- QR ticket assignment and delivery via WhatsApp
- Door check-in scanner — each ticket works only ONCE
"""

import os
import csv
import json
import qrcode
import urllib.request
import urllib.error
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect

app = Flask(__name__)

DATA_DIR = "output"
QR_IMAGES_DIR = os.path.join(DATA_DIR, "qr_images")
MANIFEST_FILE = os.path.join(DATA_DIR, "ticket_manifest.csv")
USED_FILE = os.path.join(DATA_DIR, "used_tickets.json")
REGISTRATIONS_FILE = os.path.join(DATA_DIR, "registrations.json")

BOT_API_URL = "http://localhost:3001"
TOTAL_CAPACITY = 200

valid_tickets = {}
used_tickets = {}
registrations = {}
assigned_serials = set()


def load_manifest():
    """Load all valid ticket IDs from the manifest CSV."""
    global valid_tickets
    with open(MANIFEST_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            valid_tickets[row["ticket_id"]] = int(row["serial"])
    print(f"Loaded {len(valid_tickets)} valid tickets")


def load_used():
    """Load previously used tickets (survives server restart)."""
    global used_tickets
    if os.path.exists(USED_FILE):
        with open(USED_FILE, "r") as f:
            used_tickets = json.load(f)
        print(f"Loaded {len(used_tickets)} already-used tickets")


def load_registrations():
    global registrations, assigned_serials
    if os.path.exists(REGISTRATIONS_FILE):
        with open(REGISTRATIONS_FILE, "r") as f:
            registrations = json.load(f)
        for r in registrations.values():
            for t in r.get("tickets", []):
                assigned_serials.add(t["serial"])
        print(f"Loaded {len(registrations)} registrations ({len(assigned_serials)} tickets assigned)")


def save_registrations():
    with open(REGISTRATIONS_FILE, "w") as f:
        json.dump(registrations, f, indent=2, ensure_ascii=False)


def save_used():
    with open(USED_FILE, "w") as f:
        json.dump(used_tickets, f, indent=2)


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
    background: #0f0f1a;
    color: #fff;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .header {
    padding: 20px;
    text-align: center;
    width: 100%;
    background: linear-gradient(135deg, #1a1a2e, #16213e);
    border-bottom: 3px solid #e94560;
  }
  .header h1 { font-size: 1.4rem; letter-spacing: 1px; }
  .stats {
    display: flex;
    gap: 20px;
    justify-content: center;
    margin-top: 10px;
    font-size: 0.85rem;
    color: #9ca3af;
  }
  .stats span { color: #e94560; font-weight: 700; font-size: 1.1rem; }

  #reader-container {
    width: 100%;
    max-width: 500px;
    margin: 20px auto;
    padding: 0 16px;
  }
  #reader { width: 100%; border-radius: 12px; overflow: hidden; }

  .result-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    z-index: 100;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 40px;
    text-align: center;
    animation: fadeIn 0.2s ease;
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
    margin-top: 30px;
    padding: 14px 40px;
    background: rgba(255,255,255,0.2);
    border: 2px solid #fff;
    color: #fff;
    font-size: 1.1rem;
    font-weight: 600;
    border-radius: 50px;
    cursor: pointer;
  }

  .log { width: 100%; max-width: 500px; padding: 16px; margin-top: 10px; }
  .log h3 { font-size: 0.9rem; color: #9ca3af; margin-bottom: 8px; }
  .log-entry {
    padding: 10px 14px;
    margin-bottom: 6px;
    border-radius: 8px;
    font-size: 0.85rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .log-entry.ok { background: rgba(16,185,129,0.15); border-left: 3px solid #10b981; }
  .log-entry.fail { background: rgba(239,68,68,0.15); border-left: 3px solid #ef4444; }

  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
</style>
</head>
<body>

<div class="header">
  <h1>EVENT CHECK-IN</h1>
  <div class="stats">
    Checked in: <span id="checkedIn">0</span> / <span id="totalTickets">0</span>
    &nbsp;&nbsp;|&nbsp;&nbsp;
    Remaining: <span id="remaining">0</span>
  </div>
</div>

<div id="reader-container">
  <div id="reader"></div>
</div>

<div class="result-overlay" id="resultOverlay">
  <div class="result-icon" id="resultIcon"></div>
  <div class="result-title" id="resultTitle"></div>
  <div class="result-detail" id="resultDetail"></div>
  <div class="result-ticket-id" id="resultTicketId"></div>
  <button class="result-dismiss" onclick="dismissResult()">SCAN NEXT</button>
</div>

<div class="log">
  <h3>Recent Scans</h3>
  <div id="logEntries"></div>
</div>

<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<script>
let scanner;
let scanning = true;

function initScanner() {
  scanner = new Html5Qrcode("reader");
  scanner.start(
    { facingMode: "environment" },
    { fps: 10, qrbox: { width: 250, height: 250 } },
    onScanSuccess,
    () => {}
  ).catch(err => {
    document.getElementById("reader").innerHTML =
      '<p style="padding:40px;text-align:center;color:#ef4444;">Camera access denied.<br>Please allow camera and reload.</p>';
  });
}

async function onScanSuccess(ticketId) {
  if (!scanning) return;
  scanning = false;
  scanner.pause(true);

  try {
    const resp = await fetch("/api/checkin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket_id: ticketId })
    });
    const data = await resp.json();
    showResult(data, ticketId);
    refreshStats();
  } catch (e) {
    showResult({ status: "error", message: "Network error" }, ticketId);
  }
}

function showResult(data, ticketId) {
  const overlay = document.getElementById("resultOverlay");
  const icon = document.getElementById("resultIcon");
  const title = document.getElementById("resultTitle");
  const detail = document.getElementById("resultDetail");
  const tid = document.getElementById("resultTicketId");

  overlay.className = "result-overlay show";
  tid.textContent = ticketId;

  if (data.status === "ok") {
    overlay.classList.add("valid");
    icon.textContent = "✓";
    title.textContent = "WELCOME!";
    detail.textContent = `Entry #${data.serial} — ${data.entry_number} of ${data.total}`;
    addLog(ticketId, true, data.serial);
  } else if (data.status === "already_used") {
    overlay.classList.add("invalid");
    icon.textContent = "✕";
    title.textContent = "ALREADY USED";
    detail.textContent = `This ticket was scanned at ${data.used_at}`;
    addLog(ticketId, false, "DUPLICATE");
  } else {
    overlay.classList.add("unknown");
    icon.textContent = "?";
    title.textContent = "INVALID TICKET";
    detail.textContent = "This QR code is not recognized.";
    addLog(ticketId, false, "INVALID");
  }
}

function dismissResult() {
  const overlay = document.getElementById("resultOverlay");
  overlay.className = "result-overlay";
  scanning = true;
  scanner.resume();
}

function addLog(ticketId, ok, info) {
  const container = document.getElementById("logEntries");
  const time = new Date().toLocaleTimeString();
  const div = document.createElement("div");
  div.className = "log-entry " + (ok ? "ok" : "fail");
  div.innerHTML = `<span>${ok ? "✓" : "✕"} ${ticketId.substring(0, 20)}...</span><span>${info} · ${time}</span>`;
  container.prepend(div);
  if (container.children.length > 20) container.lastChild.remove();
}

async function refreshStats() {
  const resp = await fetch("/api/stats");
  const data = await resp.json();
  document.getElementById("checkedIn").textContent = data.used;
  document.getElementById("totalTickets").textContent = data.total;
  document.getElementById("remaining").textContent = data.remaining;
}

document.addEventListener("DOMContentLoaded", () => {
  refreshStats();
  initScanner();
});
</script>

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
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .card {
    background: #fff;
    border-radius: 20px;
    padding: 36px 28px;
    width: 100%;
    max-width: 420px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  }
  .card-header {
    text-align: center;
    margin-bottom: 28px;
  }
  .card-header h1 {
    font-size: 1.5rem;
    color: #1a1a2e;
    margin-bottom: 6px;
  }
  .card-header p {
    color: #6b7280;
    font-size: 0.9rem;
  }
  .accent-bar {
    width: 50px;
    height: 4px;
    background: #e94560;
    border-radius: 2px;
    margin: 12px auto;
  }
  .form-group {
    margin-bottom: 18px;
  }
  .form-group label {
    display: block;
    font-size: 0.85rem;
    font-weight: 600;
    color: #374151;
    margin-bottom: 6px;
  }
  .form-group input, .form-group select, .form-group textarea {
    width: 100%;
    padding: 12px 14px;
    border: 2px solid #e5e7eb;
    border-radius: 10px;
    font-size: 1rem;
    color: #1a1a2e;
    background: #f9fafb;
    transition: border-color 0.2s;
    outline: none;
  }
  .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
    border-color: #e94560;
    background: #fff;
  }
  .form-group input[readonly] {
    background: #f3f4f6;
    color: #6b7280;
    cursor: not-allowed;
  }
  .form-group textarea { resize: vertical; min-height: 50px; }
  .submit-btn {
    width: 100%;
    padding: 14px;
    background: linear-gradient(135deg, #e94560, #c73a54);
    color: #fff;
    font-size: 1.05rem;
    font-weight: 700;
    border: none;
    border-radius: 12px;
    cursor: pointer;
    margin-top: 8px;
    transition: transform 0.15s, box-shadow 0.15s;
    letter-spacing: 0.5px;
  }
  .submit-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(233,69,96,0.4); }
  .submit-btn:active { transform: translateY(0); }
  .submit-btn:disabled { background: #9ca3af; cursor: not-allowed; transform: none; box-shadow: none; }
  .error-msg {
    background: #fef2f2;
    border: 1px solid #fecaca;
    color: #dc2626;
    padding: 12px;
    border-radius: 10px;
    font-size: 0.9rem;
    margin-bottom: 18px;
    text-align: center;
  }
  .spots-left {
    text-align: center;
    margin-top: 16px;
    font-size: 0.85rem;
    color: #6b7280;
  }
  .spots-left span { color: #e94560; font-weight: 700; }
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    <h1>Event Registration</h1>
    <div class="accent-bar"></div>
    <p>Fill in your details to receive your QR entry ticket</p>
  </div>

  {% if error %}
  <div class="error-msg">{{ error }}</div>
  {% endif %}

  <form method="POST" action="/register" id="regForm">
    <div class="form-group">
      <label>Full Name *</label>
      <input type="text" name="name" required placeholder="Your full name" value="{{ prev_name or '' }}">
    </div>
    <div class="form-group">
      <label>Phone Number</label>
      <input type="text" name="phone" value="{{ phone }}" readonly>
    </div>
    <div class="form-group">
      <label>Email *</label>
      <input type="email" name="email" required placeholder="your@email.com" value="{{ prev_email or '' }}">
    </div>
    <div class="form-group">
      <label>Number of Attendees *</label>
      <select name="attendees" required>
        <option value="">Select</option>
        <option value="1">1</option>
        <option value="2">2</option>
        <option value="3">3</option>
        <option value="4">4</option>
        <option value="5">5</option>
      </select>
    </div>
    <div class="form-group">
      <label>How did you hear about this event?</label>
      <textarea name="reference" placeholder="e.g. Instagram, friend, college notice..." rows="2">{{ prev_reference or '' }}</textarea>
    </div>
    <button type="submit" class="submit-btn" id="submitBtn">REGISTER & GET QR TICKET</button>
  </form>
  <div class="spots-left">
    <span>{{ spots_left }}</span> spots remaining out of {{ total }}
  </div>
</div>
<script>
document.getElementById('regForm').addEventListener('submit', function() {
  var btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'REGISTERING...';
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
<title>Registration Successful!</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #064e3b, #065f46, #047857);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .card {
    background: #fff;
    border-radius: 20px;
    padding: 36px 28px;
    width: 100%;
    max-width: 420px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    text-align: center;
  }
  .check-icon {
    width: 70px;
    height: 70px;
    background: #10b981;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 0 auto 20px;
    font-size: 2.2rem;
    color: #fff;
  }
  h1 { font-size: 1.5rem; color: #1a1a2e; margin-bottom: 8px; }
  .subtitle { color: #6b7280; font-size: 0.95rem; margin-bottom: 24px; }
  .qr-section { margin-bottom: 16px; }
  .qr-box {
    background: #f9fafb;
    border: 2px dashed #d1d5db;
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 12px;
  }
  .qr-box img { width: 180px; height: 180px; }
  .qr-label {
    font-size: 0.85rem;
    font-weight: 600;
    color: #374151;
    margin-bottom: 8px;
  }
  .ticket-id {
    font-family: monospace;
    background: #f3f4f6;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 0.8rem;
    color: #374151;
    margin-top: 8px;
    display: inline-block;
  }
  .ticket-info { margin-top: 16px; }
  .ticket-info .row {
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid #f3f4f6;
    font-size: 0.9rem;
  }
  .ticket-info .row:last-child { border-bottom: none; }
  .ticket-info .label { color: #6b7280; }
  .ticket-info .value { color: #1a1a2e; font-weight: 600; }
  .wa-notice {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-radius: 10px;
    padding: 12px;
    font-size: 0.85rem;
    color: #166534;
    margin-top: 20px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="check-icon">&#10003;</div>
  <h1>You're Registered!</h1>
  <p class="subtitle">{{ attendees }} QR ticket{{ 's' if attendees|int > 1 else '' }} generated</p>

  <div class="qr-section">
    {% for t in tickets %}
    <div class="qr-box">
      <div class="qr-label">Attendee {{ loop.index }} of {{ attendees }} &mdash; Ticket #{{ '%03d' % t.serial }}</div>
      <img src="/qr-image/{{ t.serial }}" alt="QR Ticket #{{ t.serial }}">
      <div class="ticket-id">{{ t.ticket_id }}</div>
    </div>
    {% endfor %}
  </div>

  <div class="ticket-info">
    <div class="row"><span class="label">Registered by</span><span class="value">{{ name }}</span></div>
    <div class="row"><span class="label">Total Attendees</span><span class="value">{{ attendees }}</span></div>
  </div>

  <div class="wa-notice">
    All {{ attendees }} QR code{{ 's' if attendees|int > 1 else '' }} have been sent to your WhatsApp. Each person shows their own QR at the door. One-time use only!
  </div>
</div>
</body>
</html>
"""


def get_next_available_tickets(count):
    """Find the next N unassigned tickets from the manifest."""
    serial_to_ticket = {v: k for k, v in valid_tickets.items()}
    tickets = []
    for serial in sorted(serial_to_ticket.keys()):
        if serial not in assigned_serials:
            tickets.append((serial, serial_to_ticket[serial]))
            if len(tickets) >= count:
                break
    return tickets


def generate_qr_image(ticket_id, serial):
    """Generate a QR code PNG for a specific ticket."""
    os.makedirs(QR_IMAGES_DIR, exist_ok=True)
    filepath = os.path.join(QR_IMAGES_DIR, f"ticket_{serial:03d}.png")
    if not os.path.exists(filepath):
        img = qrcode.make(ticket_id, box_size=10, border=2)
        img.save(filepath)
    return filepath


def notify_bot(phone, tickets_data, name):
    """Call the WhatsApp bot API to send QR images for all tickets."""
    for t in tickets_data:
        try:
            payload = json.dumps({
                "phone": phone,
                "qr_image_path": t["qr_path"],
                "ticket_id": t["ticket_id"],
                "serial": t["serial"],
                "name": name,
            }).encode()
            req = urllib.request.Request(
                f"{BOT_API_URL}/send-qr",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())
            print(f"  Bot delivery #{t['serial']:03d}: {result}")
        except (urllib.error.URLError, Exception) as e:
            print(f"  Bot delivery #{t['serial']:03d} failed (QR still shown on web): {e}")


WA_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WhatsApp Bot Login</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f1a;
    color: #fff;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .card {
    background: #1a1a2e;
    border-radius: 20px;
    padding: 40px;
    text-align: center;
    max-width: 400px;
    width: 100%;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  h1 { font-size: 1.3rem; margin-bottom: 8px; }
  .subtitle { color: #9ca3af; font-size: 0.9rem; margin-bottom: 24px; }
  #qr-canvas {
    background: #fff;
    border-radius: 12px;
    padding: 16px;
    display: inline-block;
    margin-bottom: 20px;
  }
  #qr-canvas canvas { display: block; width: 300px !important; height: 300px !important; }
  .status { font-size: 0.9rem; color: #10b981; }
  .status.waiting { color: #f59e0b; }
  .steps { text-align: left; margin-top: 20px; font-size: 0.85rem; color: #9ca3af; line-height: 1.8; }
  .steps b { color: #fff; }
</style>
</head>
<body>
<div class="card">
  <h1>Link WhatsApp to Bot</h1>
  <p class="subtitle">Scan this QR code with your phone</p>
  <div id="qr-canvas"></div>
  <div class="status waiting" id="status">Loading QR code...</div>
  <div class="steps">
    <b>1.</b> Open WhatsApp on your phone<br>
    <b>2.</b> Go to Settings &rarr; Linked Devices<br>
    <b>3.</b> Tap "Link a Device"<br>
    <b>4.</b> Scan the QR code above
  </div>
</div>
<script>
async function fetchQR() {
  try {
    const resp = await fetch('/api/wa-qr');
    const data = await resp.json();
    if (data.status === 'ok' && data.qr) {
      document.getElementById('qr-canvas').innerHTML = '';
      QRCode.toCanvas(data.qr, { width: 400, margin: 3 }, function(err, canvas) {
        if (!err) document.getElementById('qr-canvas').appendChild(canvas);
      });
      document.getElementById('status').textContent = 'Waiting for scan...';
      document.getElementById('status').className = 'status waiting';
      setTimeout(fetchQR, 20000);
    } else {
      document.getElementById('qr-canvas').innerHTML = '<div style="padding:40px;color:#10b981;font-size:3rem;">&#10003;</div>';
      document.getElementById('status').textContent = 'Bot is connected!';
      document.getElementById('status').className = 'status';
      setTimeout(fetchQR, 10000);
    }
  } catch (e) {
    document.getElementById('status').textContent = 'Cannot reach bot. Is it running?';
    setTimeout(fetchQR, 5000);
  }
}
fetchQR();
</script>
</body>
</html>
"""


@app.route("/wa-login")
def wa_login():
    return render_template_string(WA_LOGIN_HTML)


@app.route("/api/wa-qr")
def wa_qr_proxy():
    """Proxy the bot's QR endpoint so the browser can access it."""
    try:
        resp = urllib.request.urlopen(f"{BOT_API_URL}/wa-qr", timeout=5)
        data = json.loads(resp.read())
        return jsonify(data)
    except Exception:
        return jsonify({"status": "error", "message": "Bot not reachable"})


@app.route("/")
def index():
    return render_template_string(SCANNER_HTML)


def total_attendees_registered():
    return sum(r["attendees"] for r in registrations.values())


@app.route("/register", methods=["GET"])
def register_form():
    phone = request.args.get("phone", "").strip()
    if phone in registrations:
        reg = registrations[phone]
        return render_template_string(SUCCESS_HTML,
            name=reg["name"], tickets=reg["tickets"], attendees=reg["attendees"])

    spots_left = TOTAL_CAPACITY - total_attendees_registered()
    return render_template_string(REGISTER_HTML,
        phone=phone, spots_left=spots_left, total=TOTAL_CAPACITY,
        error=None, prev_name=None, prev_email=None, prev_reference=None)


@app.route("/register", methods=["POST"])
def register_submit():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    attendees = int(request.form.get("attendees", "1").strip())
    reference = request.form.get("reference", "").strip()
    spots_left = TOTAL_CAPACITY - total_attendees_registered()

    if phone in registrations:
        reg = registrations[phone]
        return render_template_string(SUCCESS_HTML,
            name=reg["name"], tickets=reg["tickets"], attendees=reg["attendees"])

    if not name or not email:
        return render_template_string(REGISTER_HTML,
            phone=phone, spots_left=spots_left, total=TOTAL_CAPACITY,
            error="Name and Email are required.",
            prev_name=name, prev_email=email, prev_reference=reference)

    if spots_left < attendees:
        return render_template_string(REGISTER_HTML,
            phone=phone, spots_left=spots_left, total=TOTAL_CAPACITY,
            error=f"Only {spots_left} spots left, but you requested {attendees}.",
            prev_name=name, prev_email=email, prev_reference=reference)

    available = get_next_available_tickets(attendees)
    if len(available) < attendees:
        return render_template_string(REGISTER_HTML,
            phone=phone, spots_left=spots_left, total=TOTAL_CAPACITY,
            error="Not enough tickets available.",
            prev_name=name, prev_email=email, prev_reference=reference)

    tickets_data = []
    for serial, ticket_id in available:
        qr_path = generate_qr_image(ticket_id, serial)
        assigned_serials.add(serial)
        tickets_data.append({
            "serial": serial,
            "ticket_id": ticket_id,
            "qr_path": qr_path,
        })

    registrations[phone] = {
        "name": name,
        "email": email,
        "attendees": attendees,
        "reference": reference,
        "tickets": tickets_data,
        "registered_at": datetime.now().strftime("%Y-%m-%d %I:%M %p"),
    }
    save_registrations()

    serials_str = ", ".join(f"#{t['serial']:03d}" for t in tickets_data)
    print(f"✔ Registered: {name} ({phone}) → {attendees} ticket(s): {serials_str}")

    notify_bot(phone, tickets_data, name)

    return render_template_string(SUCCESS_HTML,
        name=name, tickets=tickets_data, attendees=attendees)


@app.route("/qr-image/<int:serial>")
def serve_qr_image(serial):
    filepath = os.path.join(QR_IMAGES_DIR, f"ticket_{serial:03d}.png")
    if os.path.exists(filepath):
        with open(filepath, "rb") as f:
            img_data = f.read()
        return img_data, 200, {"Content-Type": "image/png"}
    return "Not found", 404


@app.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json()
    ticket_id = data.get("ticket_id", "").strip()

    if ticket_id not in valid_tickets:
        return jsonify({"status": "invalid", "message": "Ticket not recognized"})

    if ticket_id in used_tickets:
        return jsonify({
            "status": "already_used",
            "message": "This ticket has already been scanned",
            "used_at": used_tickets[ticket_id]["used_at"],
            "serial": valid_tickets[ticket_id]
        })

    now = datetime.now().strftime("%I:%M %p")
    used_tickets[ticket_id] = {
        "serial": valid_tickets[ticket_id],
        "used_at": now,
    }
    save_used()

    return jsonify({
        "status": "ok",
        "serial": valid_tickets[ticket_id],
        "entry_number": len(used_tickets),
        "total": len(valid_tickets),
    })


@app.route("/api/stats")
def stats():
    total_attendees = total_attendees_registered()
    return jsonify({
        "total": len(valid_tickets),
        "used": len(used_tickets),
        "remaining": len(valid_tickets) - len(used_tickets),
        "registered_people": total_attendees,
        "registered_groups": len(registrations),
        "spots_left": TOTAL_CAPACITY - total_attendees,
    })


@app.route("/api/registrations")
def api_registrations():
    return jsonify({
        "total_registered": len(registrations),
        "total_capacity": TOTAL_CAPACITY,
        "registrations": registrations,
    })


if __name__ == "__main__":
    load_manifest()
    load_used()
    load_registrations()
    print("\n" + "=" * 50)
    print("  EVENT CHECK-IN SERVER")
    print("=" * 50)
    print(f"  Scanner:      http://localhost:5000")
    print(f"  Registration: http://localhost:5000/register")
    print(f"  Registered:   {len(registrations)} / {TOTAL_CAPACITY}")
    print("=" * 50 + "\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
