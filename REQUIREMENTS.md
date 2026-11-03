# fleet-tasks: what a machine needs

Everything fleet-tasks calls, what stops working without it, and how to get
it.

**Check a machine:** run `python3 doctor.py`. It checks every row below and
prints what's missing and how to fix it.

**Install:** `install.sh` runs `doctor.py --install` first, and stops before
changing anything if something required is missing.

Versions are what gpu-server runs (2026-10-26); "minimum" is what the code
needs.

## Required

| What | gpu-server has | Minimum | Used for | Get it |
|---|---|---|---|---|
| Linux with systemd (user services) | Ubuntu 22.04.5, systemd 249 | systemd user manager | Running the workers (`fleet-tasks.service`) and the page (`fleet-web.service`) | Ubuntu 22.04+. On a machine nobody logs into: `sudo loginctl enable-linger $USER` |
| Python 3 | 3.10.12 | 3.10 | Everything (standard library only: no `pip install` needed to run it) | Comes with Ubuntu 22.04 |
| SQLite, through Python's `sqlite3` | 3.37.2 | 3.22 (WAL mode) | The queue (`queue.db`) and the memory (`memory.db`) | Comes with Python |
| Tailscale | 1.102.5 | any current | The page over the tailnet (`tailscale serve`), phone alerts (Taildrop), reaching other machines' Ollama. Optional on a single machine: install with `FLEET_LOCAL_LOGIN=me` and open the page through an SSH tunnel | https://tailscale.com/download/linux, then `tailscale up` |
| Ollama, on at least one machine | 0.33.2 | 0.5+ (`/api/chat`, `/api/embed`) | All local AI work | https://ollama.com/download |
| Each endpoint's model, pulled on its machine | qwen3.6:27b (gpu-server), llama3.1:8b (shared-pc), qwen2.5-coder:7b (laptop), llama3.2:3b (mini-pc) | as listed in `policy.json` `endpoints` | Work on that machine | `ollama pull <model>` on that machine |

## Needed for one feature (fleet-tasks runs without it)

| What | gpu-server has | Without it | Get it |
|---|---|---|---|
| `nomic-embed-text` on the `memory.embed_on` machines | pulled 2026-10-26 | Memory search works by words only, not by meaning | `ollama pull nomic-embed-text` |
| Outbound internet: DNS + HTTPS (443) from the machine running the workers | yes | Research tasks can't read web pages (everything else still works) | Allow outbound DNS and 443 |
| git | 2.34.1 | Tasks can't be given code (`--repo/--path`, the page's "Code to read") | `sudo apt install git` |
| `pdftotext` (poppler-utils, GPL-2.0: run as a separate program, not linked) | 22.02.0 | Attached PDF documents can't be read; text, Markdown, CSV, HTML and Word files still can | `sudo apt install poppler-utils` |
| GitHub CLI (`gh`), signed in | 2.102.0 | Tasks can't be given a PR (`--pr`) | https://cli.github.com, then `gh auth login` |
| Claude Code (`claude`), signed in | 2.1.310 | No paid Claude calls. `install.sh --mcp` and `--hooks` can't be used | https://claude.com/claude-code, then `claude` once to sign in |
| Gemini CLI (`gemini`), signed in | 0.63.0 | No Gemini calls (it's off until the owner signs in, WP-137) | `npm i -g @google/gemini-cli`, then `gemini` once |
| GitHub Copilot CLI (`copilot`), signed in | 1.0.103, pinned (`expect_version`; Node 22+; signed in through `gh`) | No Copilot fallback: when Claude and Gemini are both locked out, approved tasks wait instead | `npm i -g @github/copilot@1.0.103` (needs Node 22+), then `copilot login` (or `gh auth login`). Another version: run RUNBOOK's text-only check first |
| Aider (`aider` on PATH; the `vibe` setup, WP-38) | aider 0.86.2 | Backlog **Build** with the local model can't run; Claude builds still can | `pipx install aider-chat==0.86.2` |
| Time zone data (`tzdata`, Python's `zoneinfo`) | yes (`/usr/share/zoneinfo`) | Gemini's lock ends at this machine's midnight, not Pacific midnight (a few hours off) | `sudo apt install tzdata` |
| `node/gpu_free.py` on each shared PC with `gpu_probe` (needs `nvidia-smi` from the NVIDIA driver) | not running on shared-pc yet (WP-149, an owner task on the work board; no setup script exists yet, so it is a manual copy for now) | The fleet leaves that PC alone (fails closed) | Copy `node/gpu_free.py` there and run it as a service, with the owner's OK |
| The local models listed in `catalog.json` for each machine (`endpoints`) | all pulled on gpu-server (doctor checks each machine) | That model isn't routed there; the machine's own model still works | `ollama pull NAME` on that machine, or remove the machine from that model's `endpoints` |
| A writable design output folder (`policy.json` `design.output_dir`, default `~/designs`) | `~/designs` (2026-10-28) | Approve can't keep a design's files (STL, preview, spec, brief, manual) or offer them as a zip; the working copies stay in the state folder | nothing extra: it's made on first approval. To keep them elsewhere, set `design.output_dir` |
| `gpu_lease.py` (in this folder) and its lease folder | installed by `install.sh` | No GPU leases on gpu-server's pairs: work runs without checking them out | nothing extra |

## Outside tools (design, images, voice)

Every outside tool beyond the core, from `tools.json`: one list that
`doctor.py` checks, the host scan reads, and this table is made from
(`python3 doctor.py --tools-md`; a test keeps them the same). All are
optional: without one, only the features it's used for wait. A service's
address goes in `policy.json` `"tools": {"<id>": {"url": "..."}}`.
"Commercial use" is flagged anywhere it isn't a plain yes; a non-commercial
tool is marked **NO** and is never the default.

<!-- tools.json table: made by doctor.py --tools-md; don't edit by hand -->
| Tool | Kind | Used for | Licence | Commercial use | Get it |
|---|---|---|---|---|---|
| 3D print prompt tool (3d-printing-model-prompt) | service | design: build structural parts (OpenSCAD/CadQuery); design: preview render (/thumbnail); design: watertight and overhang checks; design: Meshy jobs (its key stays in that app) | MIT | yes | https://github.com/geoffreypboals-web/3d-printing-model-prompt (docker compose up -d); then set policy.json tools.print3d.url to its address |
| OpenSCAD | program (inside the 3D print tool) | design: structural parts from a written spec | GPL-2.0 (run as a separate program, not linked) | yes | in the 3D print tool's image; https://openscad.org |
| Blender | program (inside the 3D print tool) | design: preview renders; design: mesh repair, thickening, organic shapes | GPL-2.0-or-later (run as a separate program) | yes | in the 3D print tool's image; https://www.blender.org |
| FreeCAD (headless freecadcmd) | program (inside the 3D print tool) | design: STEP export; design: solid repair | LGPL-2.1-or-later | yes | in the 3D print tool's image (freecad-python3); https://www.freecad.org |
| Meshy AI (paid web service) | account | design: artistic parts (an approved image -> image-to-3D); design: terrain | Meshy terms of service | with conditions: who owns a generated model depends on the Meshy plan (free-plan models are public under CC BY 4.0; paid plans are private). Check the plan before selling a design. | https://www.meshy.ai: an account and an API key, set in the 3D print tool (never in fleet-tasks) |
| Image generator environment (PyTorch + diffusers) | python environment | design: a picture from the prompt, for the owner's OK before any 3D work | PyTorch BSD-3-Clause; diffusers, transformers, accelerate Apache-2.0; bitsandbytes MIT | yes | python3 -m venv ~/.local/opt/fleet-image && ~/.local/opt/fleet-image/bin/pip install torch --index-url https://download.pytorch.org/whl/cu128 && ~/.local/opt/fleet-image/bin/pip install diffusers transformers accelerate sentencepiece protobuf bitsandbytes huggingface_hub pillow (pick the PyTorch CUDA build your driver supports) |
| FLUX.1 [schnell] image model (black-forest-labs/FLUX.1-schnell) | model (about 34 GB download) | design: the picture for the owner's OK | Apache-2.0 | yes: FLUX.1 [dev] is non-commercial: don't swap it in for work you sell. | a free Hugging Face account; accept the model's terms on its page (approved at once); a Read token stored with: fleet-secret set --service=fleet-image HF_TOKEN (type the name; paste the token only at the hidden prompt), or the HF_TOKEN env var on a machine without fleet-secret. KEEP THE TOKEN: don't delete or regenerate it on huggingface.co while the fleet uses it (saved 2026-10-28); without it the image step can't download the model. To replace it, make the new one first, then fleet-secret rotate --service=fleet-image HF_TOKEN, then delete the old one. |
| Image service (fleet-image: FLUX.1 schnell on a leased GPU pair) | service | design: the picture of an artistic part, for the owner's OK before Meshy | fleet-tasks (this repo); FLUX.1 schnell Apache-2.0 | yes | image/install.sh (venv, one-time model download with HF_TOKEN, user service fleet-image). It takes a GPU pair through gpu_lease.py and waits for that pair's models to leave on their own: it never pushes one out |
| NVIDIA GPU, 16 GB, driver for CUDA 12.8+ | hardware | design: the image generator (FLUX schnell in 8-bit fits in 16 GB) | NVIDIA driver licence | yes | an NVIDIA card with 16 GB or more and the 570+ driver. On a shared machine the image job takes a GPU through gpu_lease.py, so it never pushes out a loaded Ollama model |
| A vision model in Ollama (gemma3:12b or gemma3:27b) | model | design: compare a render with the brief before it reaches the owner | Gemma Terms of Use (llava:13b: Llama 2 Community Licence) | with conditions: both allow commercial use under their terms and use policies; gemma3 is preferred | ollama pull gemma3:12b |
| Voice typing service (fleet-voice: faster-whisper small.en) | service | the page: speak into text boxes on a phone | faster-whisper MIT; Whisper model weights MIT | yes | voice/install.sh (its own venv in ~/.local/opt/fleet-voice; runs on the CPU) |
<!-- end of tools.json table -->

## For working on the code

| What | Used for |
|---|---|
| Python 3.10 or 3.11 with `pytest` (on gpu-server, a 3.11 venv: `python3.11 -m venv v && v/bin/pip install pytest`, run with `-p no:anyio`) | `tests/test_fleet_*.py` |
| `ruff` and `bandit` from `.github/security/requirements-lint.txt` (hash-locked; `pip install --require-hashes -r .github/security/requirements-lint.txt`) | the CI lint gate (`python-lint` job in `.github/workflows/tests.yml`) |

## Ports and files

- **Ports:** outbound 80/443 to public addresses only (research; private and tailnet addresses are refused), 127.0.0.1:8960 (the page, behind `tailscale serve --https=8960`)
  and each Ollama endpoint's port in `policy.json`.
- **State:** `~/.local/state/fleet-tasks/`: `queue.db`, `memory.db`,
  `frontier-ledger.jsonl`, `screen-log.jsonl`, `claude-usage.jsonl` (with `--usage`), `STOP`,
  `PAUSE`.
- **Settings:**
  - `/opt/stacks/fleet-tasks/policy.json` (copied once, edited locally).
  - `~/.config/fleet-tasks/web.env` (`FLEET_ALLOWED_LOGINS`).
  - With `--hooks`: entries in `~/.claude/settings.json`.

## Keeping this true

Every change that adds a tool, a model or a service updates this file and
`doctor.py` (or, for an outside tool, `tools.json` and the table above) in
the same PR. A test checks that every command the code runs
is listed here (`tests/test_fleet_doctor.py`).
