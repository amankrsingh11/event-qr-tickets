#!/bin/bash

# Persist data inside the volume so it survives redeploys
PERSIST_DIR="/app/bot/wa_session/app_data"
mkdir -p "$PERSIST_DIR"

for f in registrations.json used_tickets.json; do
  if [ -f "$PERSIST_DIR/$f" ] && [ ! -f "/app/output/$f" ]; then
    cp "$PERSIST_DIR/$f" "/app/output/$f"
  fi
done

# Background sync: periodically copy data files to persistent volume
(while true; do
  sleep 30
  cp /app/output/registrations.json "$PERSIST_DIR/" 2>/dev/null
  cp /app/output/used_tickets.json "$PERSIST_DIR/" 2>/dev/null
done) &

# Start the Flask check-in server
cd /app
/app/venv/bin/python checkin_server.py
