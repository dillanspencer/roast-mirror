import os
import subprocess
from pathlib import Path
from flask import Flask, render_template_string, send_from_directory

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
    print("Mirror triggered!")
    return "200"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, threaded=True)