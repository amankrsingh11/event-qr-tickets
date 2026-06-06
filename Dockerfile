FROM node:20-slim

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY bot/package.json bot/package-lock.json ./bot/
RUN cd bot && npm install

COPY . .

RUN mkdir -p output/qr_images

EXPOSE 5000

RUN chmod +x start.sh
CMD ["./start.sh"]
