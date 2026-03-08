FROM python:3.11-slim

# System deps:
# - ffmpeg: audio format conversion for Whisper
# - nodejs + npm: runs vite build for generated React projects
# - git: required for GitHub repo creation (OAuth phase)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      nodejs \
      npm \
      git \
      curl && \
    rm -rf /var/lib/apt/lists/*

# Install Vercel CLI globally
RUN npm install -g vercel

# Set up working directory
WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Railway injects $PORT at runtime
CMD uvicorn main:app --host 0.0.0.0 --port $PORT