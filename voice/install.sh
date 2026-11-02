#!/usr/bin/env bash
# install.sh -- install or refresh the voice service (speech to text for the fleet page).
# Optional: the page hides its microphone buttons while this isn't running.
# Needs python3-venv and about 1 GB of disk (the venv plus the small.en model).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
STACK=/opt/stacks/fleet-tasks/voice VENV="$HOME/.local/opt/fleet-voice" U="$HOME/.config/systemd/user"
[[ -x $VENV/bin/python ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade "faster-whisper>=1.2,<2"
mkdir -p "$STACK" "$U"
install -m 755 "$SRC/voice.py" "$STACK/"
install -m 644 "$SRC/fleet-voice.service" "$U/"
systemctl --user daemon-reload
systemctl --user enable --now fleet-voice.service
systemctl --user restart fleet-voice.service
echo "voice installed; the model loads in a few seconds (fetched once, about 500 MB)"
