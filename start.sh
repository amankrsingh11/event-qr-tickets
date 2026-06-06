#!/bin/bash

# Start the WhatsApp bot in the background
cd /app/bot
node bot.js &
BOT_PID=$!

# Start the Flask check-in server in the foreground
cd /app
/app/venv/bin/python checkin_server.py

# If Flask exits, also kill the bot
kill $BOT_PID 2>/dev/null
