import os
import subprocess
import os
import subprocess
from openai import OpenAI
from pathlib import Path
import base64

import cv2
from pathlib import Path
from flask import Flask, render_template_string, send_from_directory
from dotenv import load_dotenv

load_dotenv()

HLS_DIR = Path("/app/hls")
HLS_DIR.mkdir(exist_ok=True)

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

def start_ffmpeg():
    global ffmpeg_process
    if ffmpeg_process is None or ffmpeg_process.poll() is not None:
        print("Starting ffmpeg for HLS...")
        ffmpeg_process = subprocess.Popen(
            FFMPEG_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )


def capture_frame_from_camera(device="/dev/video0"):
    """
    Capture a single frame from the given video device using OpenCV
    and return it as a JPEG data URL (data:image/jpeg;base64,...)
    """
    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        print("Failed to open camera device")
        return None

    # Grab one frame
    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        print("Failed to read frame from camera")
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


@app.route("/")
def index():
    # Basic HTML page with a <video> tag using HLS
    html = """
    <!doctype html>
    <html>
      <head>
        <title>Mirror Video</title>
        <meta charset="utf-8" />
        <style>
          body { background: #222; color: #eee; text-align: center; font-family: sans-serif; }
          video { margin-top: 20px; max-width: 90vw; height: auto; border: 2px solid #555; }
        </style>
      </head>
      <body>
        <h1>Dilly's Mirror</h1>
        <video id="video" controls autoplay muted playsinline>
          <source src="/hls/stream.m3u8" type="application/vnd.apple.mpegurl">
          Your browser does not support HLS.
        </video>
      </body>
    </html>
    """
    start_ffmpeg()
    return render_template_string(html)



@app.route("/hls/<path:filename>")
def hls_files(filename):
    # Serve HLS playlist and segments
    return send_from_directory(HLS_DIR, filename)


@app.route("/trigger_mirror", methods=['POST'])
def trigger_mirror():
    # 1) Capture a still frame from the camera
    img_data_url = capture_frame_from_camera("/dev/video0")
    if img_data_url is None:
        return "Failed to capture frame", 500

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    prompt = "Roast this room. Use slang. Be funny."

    response = client.responses.create(
    model="gpt-4o-mini",
    input=[
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": img_data_url},
            ],
        }
        ],
    )

    print("OpenAI response:", response.output[0].content[0].text)
    return "200"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, threaded=True)