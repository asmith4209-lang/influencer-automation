FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py .
COPY thumbnail.py .
COPY youtube.py .

CMD ["python", "-u", "watcher.py"]
