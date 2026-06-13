FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install flask --no-cache-dir
RUN pip install yt-dlp --no-cache-dir

COPY app.py .
COPY templates/ templates/

EXPOSE 5000

CMD ["python", "app.py"]
