import os
import subprocess
from openai import OpenAI
from pathlib import Path
import base64
import random

import cv2
from flask import Flask, render_template_string, send_from_directory
from dotenv import load_dotenv

from elevenlabs import ElevenLabs
from elevenlabs.play import play

load_dotenv()

HLS_DIR = Path("/app/hls")
HLS_DIR.mkdir(exist_ok=True)

# directory to store tts audio
AUDIO_DIR = Path("/app/audio")
AUDIO_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

FFMPEG_CMD = [
    "ffmpeg",
    "-f", "v4l2",
    "-framerate", "25",
    "-video_size", "1280x720",
    "-i", "/dev/video0",        # your camera device
    "-vcodec", "libx264",       # try "h264_omx" on Pi for hw accel if available
    "-preset", "veryfast",
    "-tune", "zerolatency",
    "-f", "hls",
    "-hls_time", "2",           # segment length in seconds
    "-hls_list_size", "5",      # keep last 5 segments
    "-hls_flags", "delete_segments+append_list",
    str(HLS_DIR / "stream.m3u8"),
]

ffmpeg_process = None

# ---------- OpenAI + ElevenLabs clients ----------

openai_client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
)

eleven_client = ElevenLabs(
    api_key=os.environ.get("ELEVENLABS_API_KEY"),
)

# Prompt templates
prompts = [
    "pmpt_6922564bdf248194a09e7beca13bb4e50169736a9f3529b8",  # hood slang
    "pmpt_69225843c1148194904d558b4c21e10b07f426335240a852",  # asian father
    "pmpt_69225a457d7081979a8952e7bf29f181060fa0efb31aac45",  # grandfather
]

# map prompt -> elevenlabs voice ID
voices = {
    "pmpt_6922564bdf248194a09e7beca13bb4e50169736a9f3529b8": "6OzrBCQf8cjERkYgzSg8",
    "pmpt_69225843c1148194904d558b4c21e10b07f426335240a852": "7DkaWvcqvBstUe3167oW",
    "pmpt_69225a457d7081979a8952e7bf29f181060fa0efb31aac45": "MKlLqCItoCkvdhrxgtLv",
}


def capture_frame_from_rtsp(stream_url="rtsp://localhost:8554/cam"):
    """
    Capture a single frame from the given RTSP stream using OpenCV
    and return it as a JPEG data URL (data:image/jpeg;base64,...)
    """
    # Open the RTSP stream
    cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        print(f"Failed to open RTSP stream: {stream_url}")
        return None

    # Optionally reduce buffering / get the most recent frame
    # cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # Read a few frames to let the stream "settle"
    frame = None
    for _ in range(5):
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame from RTSP stream")
            cap.release()
            return None

    cap.release()

    if frame is None:
        print("No frame captured from RTSP stream")
        return None

    # Encode frame as JPEG
    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        print("Failed to encode frame as JPEG")
        return None

    jpg_bytes = buffer.tobytes()
    b64 = base64.b64encode(jpg_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    return data_url

# ---------- ElevenLabs TTS helper ----------

def speak_with_elevenlabs(text: str, voice_id: str) -> None:
    """
    Use ElevenLabs to convert `text` to speech with the given `voice_id`,
    save it as an MP3 file, and play it with ffplay.
    """
    if not text:
        print("No text to speak.")
        return

    if not voice_id:
        print("No voice_id provided for ElevenLabs.")
        return

    print(f"Sending text to ElevenLabs (voice {voice_id})...")

    # Request TTS stream from ElevenLabs
    audio_stream = eleven_client.text_to_speech.convert(
        voice_id=voice_id,
        model_id="eleven_multilingual_v2",
        text=text,
        output_format="mp3_44100_128",
    )

    # Play the file using ffplay (no window, auto exit)
    try:
        play(audio_stream)
    except FileNotFoundError:
        print("ffplay not found. Make sure ffmpeg/ffplay is installed in the container.")


# ---------- Flask routes ----------

@app.route("/trigger_mirror", methods=["POST"])
def trigger_mirror():
    print("Mirror trigger received")

    # 1) Capture a still frame from the camera
    img_data_url = capture_frame_from_rtsp()
    if img_data_url is None:
        return "Failed to capture frame", 500

    # 2) Pick a random prompt template and matching voice
    selected_prompt = random.choice(prompts)
    voice_id = voices.get(selected_prompt)
    if voice_id is None:
        print(f"No ElevenLabs voice configured for prompt {selected_prompt}")
        voice_id = list(voices.values())[0]  # fallback to first voice

    # 3) Ask OpenAI for a response based on the image + prompt template
    response = openai_client.responses.create(
        prompt={"id": selected_prompt},
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": img_data_url},
                ],
            }
        ],
    )

    # Extract text from response robustly
    try:
        print("OpenAI response object:", response)

        assistant_message = None
        for item in response.output:
            # Messages have type == "message"
            if getattr(item, "type", None) == "message":
                assistant_message = item
                break

        if assistant_message is None:
            raise ValueError("No assistant message found in response.output")

        # Collect all text chunks from the assistant message
        text_chunks = []
        for c in assistant_message.content:
            # "output_text" is where your roast lives
            if getattr(c, "type", None) == "output_text":
                text_chunks.append(c.text)

        if not text_chunks:
            raise ValueError("No output_text content found in assistant message")

        mirror_text = " ".join(text_chunks)

    except Exception as e:
        print("Failed to parse OpenAI response:", e)
        return "Failed to parse OpenAI response", 500

    print("OpenAI response text:", mirror_text)

    # 4) Send the text to ElevenLabs and play the audio
    speak_with_elevenlabs(mirror_text, voice_id)

    return "200"


if __name__ == "__main__":
    # threaded=True so ffplay / ffmpeg subprocesses don't block Flask
    app.run(host="0.0.0.0", port=3000, threaded=True)
