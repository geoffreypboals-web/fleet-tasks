# fleet voice: talk instead of typing

The fleet page shows a **🎤 Speak** button under "What to do", "Text to
split" and the memory search. Tap it, say what you want, and tap **Stop**.
The words go in at the cursor, and you can edit them before you send.

- **Private:** the phone sends the recording to gpu-server over the tailnet.
  Whisper (`small.en`, through faster-whisper) turns it into words on the
  CPU. Nothing goes to Google, Apple or any paid AI, and the GPUs' models
  are never touched or evicted.
- **Limits:** up to 3 minutes per recording (the page stops on its own at
  2:50) and 15 MB. English only.
- **Speed:** about 1 to 2 seconds for 10 seconds of speech, on 8 CPU threads.
- **Hidden** when the browser can't record, or when this service isn't
  running.

## Install

    ./stacks/fleet-tasks/voice/install.sh

This makes a venv in `~/.local/opt/fleet-voice` (about 500 MB) and the user
service `fleet-voice` (127.0.0.1:8961). The first start fetches the model
(about 500 MB) into `~/.local/share/fleet-voice/models`.

The web page reaches it as `FLEET_VOICE_URL` (default http://127.0.0.1:8961).
To change the model, set `FLEET_VOICE_MODEL` (for example `medium.en`, which
is slower and more accurate) and `FLEET_VOICE_THREADS` in a drop-in for
`fleet-voice.service`.

## Licences

faster-whisper, CTranslate2 and the Whisper weights are all MIT: fine for
commercial use.

## The first time on a phone

The browser asks to use the microphone. Allow it for this page. If you said
no, turn it back on in the browser's site settings for
gpu-server.your-tailnet.ts.net.
