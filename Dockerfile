FROM python:3.10-slim

# Install system dependencies including curl, iptables, ca-certificates, and ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iptables \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy deno JS engine for yt-dlp JavaScript extraction support
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Install Tailscale binaries
RUN curl -fsSL https://tailscale.com/install.sh | sh

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose FastAPI port
EXPOSE 8080

# Command to run the application
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
