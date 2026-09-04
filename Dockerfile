FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if required by pg/web3
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# Install the hash-locked dependency graph used by CI.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy application source code
COPY . .

# Expose port for FastAPI
EXPOSE 8000

# Run alembic migrations and start uvicorn
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
