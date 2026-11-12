FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./app.py
COPY index.html ./index.html
ENV PORT=5000 YTDLP_NOCHECKCERT=1
EXPOSE 5000
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --timeout 600 app:app"]
