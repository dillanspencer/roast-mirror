FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg

COPY . /app
WORKDIR /app

# Install any dependencies you need
RUN pip3 install --no-cache-dir -r requirements.txt

CMD ["python3", "mirror.py"]
