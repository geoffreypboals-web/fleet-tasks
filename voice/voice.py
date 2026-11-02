#!/usr/bin/env python3
"""voice.py -- speech to text for the fleet page's text boxes, on gpu-server.

A phone records what the owner says; web.py passes the recording here and
puts the words in the text box. Whisper (faster-whisper, MIT) runs on the
CPU, so the recording never leaves gpu-server and the GPUs' models are never
touched. Listens on 127.0.0.1 only: web.py is the way in (tailnet login
checked there).

  GET  /health   {"ok": true, "model": ...}
  POST /listen   the recording (webm/opus, mp4/aac, ogg, wav), at most 15 MB
                 and 3 minutes -> {"text": "..."}

Settings (environment): FLEET_VOICE_MODEL (small.en), FLEET_VOICE_THREADS (8),
FLEET_VOICE_PORT (8961), FLEET_VOICE_MODELS (where the model is kept).
"""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from faster_whisper import WhisperModel, decode_audio

MODEL = os.environ.get("FLEET_VOICE_MODEL", "small.en")
THREADS = int(os.environ.get("FLEET_VOICE_THREADS", "8"))
PORT = int(os.environ.get("FLEET_VOICE_PORT", "8961"))
ROOT = os.environ.get("FLEET_VOICE_MODELS", str(Path.home() / ".local/share/fleet-voice/models"))
MAX_BYTES, MAX_SECONDS, RATE = 15_000_000, 180, 16000

_model = None
_lock = threading.Lock()


def model():
    global _model
    if _model is None:
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS, download_root=ROOT)
    return _model


def transcribe(data):
    """The words in a recording, or raises ValueError with a reason to show."""
    try:
        audio = decode_audio(io.BytesIO(data), sampling_rate=RATE)
    except Exception as e:                     # PyAV raises many kinds; all mean "not audio we can read"
        raise ValueError(f"couldn't read the recording ({type(e).__name__})")
    if len(audio) > MAX_SECONDS * RATE:
        raise ValueError(f"the recording is over {MAX_SECONDS // 60} minutes")
    with _lock:                                # one at a time: the CPU threads are shared
        segments, _ = model().transcribe(audio, language="en", beam_size=5, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "model": MODEL})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/listen":
            return self._send(404, {"error": "not found"})
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BYTES:
            return self._send(413, {"error": "the recording is empty or over 15 MB"})
        try:
            return self._send(200, {"text": transcribe(self.rfile.read(size))})
        except ValueError as e:
            return self._send(400, {"error": str(e)})


if __name__ == "__main__":
    model()                                    # load (and fetch the first time) before taking requests
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
