import os
import json
import subprocess
from pathlib import Path
import base64
import random
from queue import Queue

import cv2
from flask import (
    Flask,
    render_template_string,
    jsonify,
    Response,
    stream_with_context,
    send_from_directory,
)
from dotenv import load_dotenv

from openai import OpenAI
from elevenlabs import ElevenLabs

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

# ---------- GLOBAL STATE ----------

CURRENT_MIRROR_TEXT = ""
CURRENT_AUDIO_FILENAME = None  # last generated audio file name (in AUDIO_DIR)
subscribers = []  # list of Queue objects for SSE clients


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
    "pmpt_692f84b4dcc48196a034c3395b36f8700bd94338843c6f1e",  # jamacian auntie
]

# map prompt -> elevenlabs voice ID
voices = {
    "pmpt_6922564bdf248194a09e7beca13bb4e50169736a9f3529b8": "6OzrBCQf8cjERkYgzSg8",
    "pmpt_69225843c1148194904d558b4c21e10b07f426335240a852": "7DkaWvcqvBstUe3167oW",
    "pmpt_69225a457d7081979a8952e7bf29f181060fa0efb31aac45": "MKlLqCItoCkvdhrxgtLv",
    "pmpt_692f84b4dcc48196a034c3395b36f8700bd94338843c6f1e": "mrDMz4sYNCz18XYFpmyV",
}


def capture_frame_from_rtsp(stream_url="rtsp://localhost:8554/cam"):
    """
    Capture a single frame from the given RTSP stream using OpenCV
    and return it as a JPEG data URL (data:image/jpeg;base64,...)
    """
    cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        print(f"Failed to open RTSP stream: {stream_url}")
        return None

    frame = None
    for _ in range(2):
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame from RTSP stream")
            cap.release()
            return None

    cap.release()

    if frame is None:
        print("No frame captured from RTSP stream")
        return None

    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        print("Failed to encode frame as JPEG")
        return None

    jpg_bytes = buffer.tobytes()
    b64 = base64.b64encode(jpg_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    return data_url


# ---------- ElevenLabs TTS helper ----------

def generate_elevenlabs_audio(text: str, voice_id: str) -> str | None:
    """
    Use ElevenLabs to convert `text` to speech with the given `voice_id`,
    save it as an MP3 file under AUDIO_DIR, and return the filename.

    The audio is then streamed to browsers via /audio/<filename>.
    """
    if not text:
        print("No text to speak.")
        return None

    if not voice_id:
        print("No voice_id provided for ElevenLabs.")
        return None

    print(f"Sending text to ElevenLabs (voice {voice_id})...")

    # Voice settings
    voice_settings = {
        "speed": 0.9,
        "stability": 0.5,
        "similarity": 0.75,
        "style_exaggeration": 1,  # <- expressive voice
        "speaker_boost": True       # <- louder, clearer
    }

    audio_stream = eleven_client.text_to_speech.convert(
        voice_id=voice_id,
        model_id="eleven_multilingual_v2",
        text=text,
        output_format="mp3_44100_128",
        voice_settings=voice_settings,
    )

    filename = f"mirror_{int(random.random() * 1e9)}.mp3"
    out_path = AUDIO_DIR / filename

    # Write streaming chunks to file
    with open(out_path, "wb") as f:
        for chunk in audio_stream:
            if chunk:
                f.write(chunk)

    print(f"Saved TTS audio to {out_path}")

    try:
        subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", str(out_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("ffplay not found. Make sure ffmpeg/ffplay is installed in the container.")

    return filename


# ---------- SSE (Server-Sent Events) ----------

@app.route("/events")
def events():
    """
    SSE endpoint. Each connected client gets its own queue.
    We push JSON strings with {text, audio_url} whenever /trigger_mirror runs.
    """
    global CURRENT_MIRROR_TEXT, CURRENT_AUDIO_FILENAME

    q = Queue()
    subscribers.append(q)

    def gen():
        # Send current state immediately
        initial_payload = json.dumps({
            "text": CURRENT_MIRROR_TEXT,
            "audio_url": f"/audio/{CURRENT_AUDIO_FILENAME}" if CURRENT_AUDIO_FILENAME else None,
        })
        yield f"data: {initial_payload}\n\n"

        try:
            while True:
                data = q.get()  # Block until new update
                yield f"data: {data}\n\n"
        except GeneratorExit:
            # Client disconnected
            if q in subscribers:
                subscribers.remove(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


def broadcast_update(text: str, audio_filename: str | None):
    """
    Push a JSON payload to all subscribers via their queues.
    """
    payload = json.dumps({
        "text": text,
        "audio_url": f"/audio/{audio_filename}" if audio_filename else None,
    })
    for q in list(subscribers):
        try:
            q.put(payload)
        except Exception as e:
            print("Failed to push to subscriber:", e)


# ---------- Flask routes ----------

@app.route("/")
def index():
    """
    Main page:
    - shows an iframe with 10.0.0.42:8889/cam
    - shows the current text
    - listens to /events via SSE for text + audio updates
    """
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <title>Dilly's Mirror</title>
        <style>
          body {
            font-family: sans-serif;
            background: #111;
            color: #eee;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            padding: 20px;
            gap: 20px;
          }
          iframe {
            width: 80vw;
            height: 60vh;
            border-radius: 8px;
            border: none;
            background: #000;
          }
          #mirror-text {
            max-width: 90vw;
            padding: 10px;
            background: #222;
            border-radius: 6px;
            min-height: 2em;
            white-space: pre-wrap;
            font-size: 1.4rem;
            text-align: center;
          }
          #status {
            font-size: 14px;
            color: #aaa;
          }
        </style>
      </head>
      <body>
        <h1>Dilly's Mirror</h1>

        <!-- iframe instead of video tag -->
        <iframe
          src="http://10.0.0.42:8889/cam"
          id="cam-frame"
        ></iframe>

        <div id="status">Connecting...</div>
        <div id="mirror-text"></div>

        <!-- Audio element for playing the mirror voice -->
        <audio id="mirror-audio" controls></audio>

        <script>
          const statusEl = document.getElementById('status');
          const textEl = document.getElementById('mirror-text');
          const audioEl = document.getElementById('mirror-audio');

          const evtSource = new EventSource('/events');

          evtSource.onopen = () => {
            statusEl.textContent = 'Connected to mirror stream';
          };

          evtSource.onmessage = (event) => {
            if (!event.data) return;
            let payload;
            try {
              payload = JSON.parse(event.data);
            } catch (e) {
              console.error('Failed to parse SSE payload', e, event.data);
              return;
            }

            if (payload.text !== undefined && payload.text !== null) {
              textEl.textContent = payload.text;
            }

            if (payload.audio_url) {
              console.log('Playing audio from', payload.audio_url);
              audioEl.src = payload.audio_url;
              // Try to play the audio (may require prior user interaction)
              audioEl.play().catch(err => {
                console.warn('Autoplay failed (browser policy):', err);
              });
            }
          };

          evtSource.onerror = (err) => {
            console.error('SSE error', err);
            statusEl.textContent = 'Disconnected from mirror stream';
          };
        </script>
      </body>
    </html>
    """
    return render_template_string(html)


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    """
    Serve the generated mp3 files so the browser can play them.
    """
    return send_from_directory(AUDIO_DIR, filename, as_attachment=False)


@app.route("/trigger_mirror", methods=["POST"])
def trigger_mirror():
    """
    This endpoint is meant to be called by something else (not the webpage).
    It:
      - captures a frame
      - gets OpenAI text
      - generates ElevenLabs audio (mp3)
      - updates globals
      - broadcasts {text, audio_url} to the webpage via SSE
    """
    global CURRENT_MIRROR_TEXT, CURRENT_AUDIO_FILENAME

    print("Mirror trigger received")

    # 1) Capture a still frame from the camera
    img_data_url = capture_frame_from_rtsp()
    if img_data_url is None:
        return jsonify({"error": "Failed to capture frame"}), 500

    # 2) Pick a random prompt template and matching voice
    selected_prompt = random.choice(prompts)
    voice_id = voices.get(selected_prompt)
    if voice_id is None:
        print(f"No ElevenLabs voice configured for prompt {selected_prompt}")
        voice_id = list(voices.values())[0]  # fallback

    # 3) Ask OpenAI for a response based on the image + prompt template
    try:
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

        print("OpenAI response object:", response)

        assistant_message = None
        for item in response.output:
            if getattr(item, "type", None) == "message":
                assistant_message = item
                break

        if assistant_message is None:
            raise ValueError("No assistant message found in response.output")

        text_chunks = []
        for c in assistant_message.content:
            if getattr(c, "type", None) == "output_text":
                text_chunks.append(c.text)

        if not text_chunks:
            raise ValueError("No output_text content found in assistant message")

        mirror_text = " ".join(text_chunks)

    except Exception as e:
        print("Failed to parse OpenAI response:", e)
        return jsonify({"error": "Failed to parse OpenAI response"}), 500

    print("OpenAI response text:", mirror_text)

    # 4) Generate ElevenLabs audio and get filename
    audio_filename = generate_elevenlabs_audio(mirror_text, voice_id)

    # 5) Update global state
    CURRENT_MIRROR_TEXT = mirror_text
    CURRENT_AUDIO_FILENAME = audio_filename

    # 6) Broadcast to all connected SSE clients
    broadcast_update(CURRENT_MIRROR_TEXT, CURRENT_AUDIO_FILENAME)

    # 7) Respond to whoever called /trigger_mirror
    return jsonify({
        "status": "ok",
        "text": mirror_text,
        "audio_file": audio_filename,
        "audio_url": f"/audio/{audio_filename}" if audio_filename else None,
    })


if __name__ == "__main__":
    # threaded=True so SSE + ffplay subprocesses don't block Flask
    app.run(host="0.0.0.0", port=3000, threaded=True)
