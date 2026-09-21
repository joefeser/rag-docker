FROM python:3.11-slim

WORKDIR /app

# requirements.txt is a generated lock; edit requirements.in instead.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# stdio transport: the client speaks JSON-RPC over stdin/stdout. Unbuffered so
# responses are not held in a pipe buffer.
ENV PYTHONUNBUFFERED=1

CMD ["python", "server.py"]
