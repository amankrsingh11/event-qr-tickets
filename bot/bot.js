const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  makeCacheableSignalKeyStore,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const qrcodeTerminal = require("qrcode-terminal");
const QRCode = require("qrcode");
const express = require("express");
const path = require("path");
const fs = require("fs");

// ── Config ────────────────────────────────────────────────────
const FLASK_PORT = process.env.PORT || 5000;
const BOT_API_PORT = 3001;

const PUBLIC_DOMAIN = process.env.RAILWAY_PUBLIC_DOMAIN;
const BASE_URL = PUBLIC_DOMAIN
  ? `https://${PUBLIC_DOMAIN}`
  : `http://${process.env.SERVER_HOST || "localhost"}:${FLASK_PORT}`;

// Use a versioned auth dir so we can force a fresh session by bumping the version
const AUTH_VERSION = process.env.WA_AUTH_VERSION || "v2";
const AUTH_DIR = path.resolve(__dirname, "wa_session", `auth_${AUTH_VERSION}`);
const logger = pino({ level: "warn" });

console.log(`Auth dir: ${AUTH_DIR}`);

// ── State ─────────────────────────────────────────────────────
let sock = null;
let latestQr = null;
let latestQrPng = null;
let botStatus = "starting"; // starting, waiting_qr, connected, disconnected

// ── Connect to WhatsApp ───────────────────────────────────────
async function startBot() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  sock = makeWASocket({
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    printQRInTerminal: false,
    logger,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      latestQr = qr;
      botStatus = "waiting_qr";
      try {
        latestQrPng = await QRCode.toBuffer(qr, { width: 400, margin: 3 });
      } catch (e) {
        latestQrPng = null;
      }
      console.log("\n╔══════════════════════════════════════════════╗");
      console.log("║  Scan QR with WhatsApp → Linked Devices       ║");
      console.log("║  Or open /wa-login on the server               ║");
      console.log("╚══════════════════════════════════════════════╝\n");
      qrcodeTerminal.generate(qr, { small: true });
    }

    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      botStatus = "disconnected";
      latestQr = null;
      latestQrPng = null;
      console.log(`⚠ Connection closed (code ${code}). Reconnect: ${shouldReconnect}`);
      if (shouldReconnect) {
        setTimeout(startBot, 5000);
      }
    }

    if (connection === "open") {
      latestQr = null;
      latestQrPng = null;
      botStatus = "connected";
      console.log("\n✔  WhatsApp Bot is CONNECTED!");
      console.log(`   Base URL: ${BASE_URL}`);
      console.log("   Waiting for messages...\n");
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    for (const msg of messages) {
      if (msg.key.fromMe) continue;
      if (!msg.key.remoteJid) continue;
      if (msg.key.remoteJid.includes("@g.us")) continue;
      if (msg.key.remoteJid === "status@broadcast") continue;

      const body =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        "";
      const chatId = msg.key.remoteJid;
      console.log(`📩 Message from ${chatId}: "${body || "(media/empty)"}"`);

      const registrationUrl = `${BASE_URL}/register?phone=${encodeURIComponent(chatId)}`;

      const replyText =
        `Hey there! 👋 Welcome to the event!\n\n` +
        `Please register using the link below to get your entry QR ticket:\n\n` +
        `🔗 ${registrationUrl}\n\n` +
        `Once you register, your QR code will be sent here automatically. ` +
        `Show it at the door for entry!`;

      try {
        await sock.sendMessage(chatId, { text: replyText });
        console.log(`→ Sent registration link to ${chatId}`);
      } catch (err) {
        console.error(`✕ Failed to reply to ${chatId}:`, err.message);
      }
    }
  });
}

// ── Express API ───────────────────────────────────────────────
const api = express();
api.use(express.json());

api.post("/send-qr", async (req, res) => {
  const { phone, qr_image_path, ticket_id, serial, name } = req.body;

  if (!phone || !qr_image_path) {
    return res.status(400).json({ error: "phone and qr_image_path required" });
  }

  try {
    const chatId = phone.includes("@") ? phone : `${phone}@s.whatsapp.net`;
    const absolutePath = path.resolve(__dirname, "..", qr_image_path);

    if (!fs.existsSync(absolutePath)) {
      return res.status(404).json({ error: `QR image not found: ${absolutePath}` });
    }

    const imgBuffer = fs.readFileSync(absolutePath);

    const caption =
      `✅ *Registration Successful!*\n\n` +
      `👤 *Name:* ${name || "Guest"}\n` +
      `🎫 *Ticket:* #${String(serial).padStart(3, "0")}\n` +
      `🆔 *ID:* ${ticket_id}\n\n` +
      `Show this QR code at the event entrance.\n` +
      `⚠️ One-time use only — this ticket becomes invalid after scanning.`;

    await sock.sendMessage(chatId, { image: imgBuffer, caption });
    console.log(`✔ QR sent to ${phone} (Ticket #${serial})`);
    res.json({ status: "ok", message: `QR sent to ${phone}` });
  } catch (err) {
    console.error(`✕ Failed to send QR to ${phone}:`, err.message);
    res.status(500).json({ error: err.message });
  }
});

api.get("/health", (req, res) => {
  res.json({ status: "ok", whatsapp: botStatus });
});

api.get("/wa-qr", (req, res) => {
  if (latestQr) {
    return res.json({ status: "ok", qr: latestQr, botStatus });
  }
  res.json({ status: "no_qr", botStatus });
});

api.get("/wa-qr-png", (req, res) => {
  if (latestQrPng) {
    return res.set("Content-Type", "image/png").send(latestQrPng);
  }
  res.json({ status: botStatus })
});

// ── Start ─────────────────────────────────────────────────────
api.listen(BOT_API_PORT, () => {
  console.log(`\n📡 Bot API server listening on port ${BOT_API_PORT}`);
});

console.log("⏳ Starting WhatsApp bot (Baileys)...");
startBot().catch((err) => console.error("Fatal:", err));
