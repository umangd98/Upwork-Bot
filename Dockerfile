FROM python:3.11-slim

# Prevent .pyc files and enable unbuffered stdout/stderr for Docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies (layer cached separately from app code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create the data directory (will be overshadowed by volume mount at runtime)
RUN mkdir -p /app/data

# Volume for persistent data (tokens.json + jobs.db)
VOLUME ["/app/data"]

# Expose the auth server port (only needed during first-time OAuth)
EXPOSE 9876

ENTRYPOINT ["python", "entrypoint.py"]
