FROM python:3.10-slim

# Install system dependencies including curl, iptables, and tailscale prerequisites
RUN apt-get update && apt-get install -y \
    curl \
    iptables \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Tailscale binaries
RUN curl -fsSL https://tailscale.com/install.sh | sh

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUNC pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose FastAPI port
EXPOSE 8080

# Command to run the application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
