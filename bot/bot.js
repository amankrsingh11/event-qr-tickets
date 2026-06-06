const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const path = require("path");
const fs = require("fs");

// ── Config ────────────────────────────────────────────────────
const FLASK_PORT = 5000;
const BOT_API_PORT = 3001;
const SERVER_HOST = process.env.SERVER_HOST || "localhost";

const GREETINGS = ["hi", "hello", "hey", "register", "start", "ticket"];

// ── WhatsApp Client ───────────────────────────────────────────
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "./wa_session" }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  console.log("\n╔══════════════════════════════════════════════╗");
  console.log("║  Scan this QR code with your WhatsApp phone  ║");
  console.log("╚══════════════════════════════════════════════╝\n");
  qrcode.generate(qr, { small: true });
});

client.on("ready", () => {
  console.log("\n✔  WhatsApp Bot is READY and connected!");
  console.log(`   Bot API running on http://localhost:${BOT_API_PORT}`);
  console.log("   Waiting for messages...\n");
});

client.on("authenticated", () => {
  console.log("✔  Authenticated with WhatsApp");
});

client.on("auth_failure", (msg) => {
  console.error("✕  Authentication failed:", msg);
});

client.on("message", async (msg) => {
  const body = msg.body.trim().toLowerCase();

  if (!GREETINGS.some((g) => body.includes(g))) return;

  // Preserve the full chat ID (handles both @c.us and @lid formats)
  const chatId = msg.from;
  const registrationUrl = `http://${SERVER_HOST}:${FLASK_PORT}/register?phone=${encodeURIComponent(chatId)}`;

  const replyText =
    `Hey there! 👋 Welcome to the event!\n\n` +
    `Please register using the link below to get your entry QR ticket:\n\n` +
    `🔗 ${registrationUrl}\n\n` +
    `Once you register, your QR code will be sent here automatically. ` +
    `Show it at the door for entry!`;

  await msg.reply(replyText);
  console.log(`→ Sent registration link to ${chatId}`);
});

// ── Express API (Flask calls this to send QR images) ──────────
const api = express();
api.use(express.json());

api.post("/send-qr", async (req, res) => {
  const { phone, qr_image_path, ticket_id, serial, name } = req.body;

  if (!phone || !qr_image_path) {
    return res.status(400).json({ error: "phone and qr_image_path required" });
  }

  try {
    // Use the chat ID as-is if it already has a suffix (@c.us or @lid)
    const chatId = phone.includes("@") ? phone : `${phone}@c.us`;
    const absolutePath = path.resolve(__dirname, "..", qr_image_path);

    if (!fs.existsSync(absolutePath)) {
      return res.status(404).json({ error: `QR image not found: ${absolutePath}` });
    }

    const media = MessageMedia.fromFilePath(absolutePath);

    const caption =
      `✅ *Registration Successful!*\n\n` +
      `👤 *Name:* ${name || "Guest"}\n` +
      `🎫 *Ticket:* #${String(serial).padStart(3, "0")}\n` +
      `🆔 *ID:* ${ticket_id}\n\n` +
      `Show this QR code at the event entrance.\n` +
      `⚠️ One-time use only — this ticket becomes invalid after scanning.`;

    await client.sendMessage(chatId, media, { caption });
    console.log(`✔ QR sent to ${phone} (Ticket #${serial})`);
    res.json({ status: "ok", message: `QR sent to ${phone}` });
  } catch (err) {
    console.error(`✕ Failed to send QR to ${phone}:`, err.message);
    res.status(500).json({ error: err.message });
  }
});

api.get("/health", (req, res) => {
  const state = client.info ? "connected" : "disconnected";
  res.json({ status: "ok", whatsapp: state });
});

// ── Start everything ──────────────────────────────────────────
api.listen(BOT_API_PORT, () => {
  console.log(`\n📡 Bot API server listening on port ${BOT_API_PORT}`);
});

console.log("⏳ Initializing WhatsApp client...");
client.initialize();
