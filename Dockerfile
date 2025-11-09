# Dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # install browsers into the image (not a cache dir)
    PLAYWRIGHT_BROWSERS_PATH=0

WORKDIR /app

# Minimal OS deps; --with-deps will bring the rest
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# If you want: upgrade pip first (helps with resolver issues)
RUN python -m pip install --upgrade pip

# Python deps (ensure requirements.txt includes "playwright==1.48.0")
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all Playwright deps into the image
RUN python -m playwright install --with-deps chromium

# App code
COPY . .

# Cloud Run listens on 8080
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8080"]
