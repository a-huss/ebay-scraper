FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Railway will inject PORT, default to 8000
ENV PORT=8000

EXPOSE 8000

# Start FastAPI (update main:app if your entrypoint is different)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
