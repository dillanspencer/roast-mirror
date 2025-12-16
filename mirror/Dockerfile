FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg
RUN apt-get install gcc libportaudio2 libportaudiocpp0 portaudio19-dev libasound-dev libsndfile1-dev -y


COPY . /app
WORKDIR /app

# Install any dependencies you need
RUN pip3 install --no-cache-dir -r requirements.txt

CMD ["bash", "startup.sh"]
