#!/bin/bash

# Persist output data (registrations, used tickets) inside the WA session volume
# so they survive redeploys
PERSIST_DIR="/app/bot/wa_session/app_data"
mkdir -p "$PERSIST_DIR"

# Symlink JSON data files to persistent storage
for f in registrations.json used_tickets.json; do
  if [ -f "$PERSIST_DIR/$f" ] && [ ! -f "/app/output/$f" ]; then
    cp "$PERSIST_DIR/$f" "/app/output/$f"
  fi
done

# Start the WhatsApp bot in the background
cd /app/bot
node bot.js &
BOT_PID=$!

# Background sync: periodically copy data files to persistent volume
(while true; do
  sleep 30
  cp /app/output/registrations.json "$PERSIST_DIR/" 2>/dev/null
  cp /app/output/used_tickets.json "$PERSIST_DIR/" 2>/dev/null
done) &

# Start the Flask check-in server in the foreground
cd /app
/app/venv/bin/python checkin_server.py

# If Flask exits, also kill the bot
kill $BOT_PID 2>/dev/null
