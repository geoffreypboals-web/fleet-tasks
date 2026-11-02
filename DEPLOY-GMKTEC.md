# Deploy fleet-tasks on a GMKtec mini PC

This guide takes a new GMKtec mini PC to a working fleet: Ollama with a model
that fits the machine, fleet-tasks running as services, and the web page open
from your laptop or phone. It works the same on any small x86 Linux box
(Intel NUC, Minisforum, Beelink); only step 1 is GMKtec-specific.

Plan on about an hour, most of it the Ubuntu install and the model download.

Every command is meant to be pasted into a terminal **on the mini PC**,
unless the step says otherwise.

## 1. Pick the model for your machine

The model has to fit in the machine's memory with room to spare. GMKtec
boxes fall into three groups. If yours isn't listed, go by its RAM and
graphics.

| Your GMKtec | Inside | Model to pull | `tier` | `num_ctx` |
|---|---|---|---|---|
| EVO-X2 (64-128 GB) | AMD Ryzen AI Max+ 395, Radeon 8060S, shared memory | `qwen3.6:27b` (128 GB: add `gemma3:27b` too) | `large` | 16384 |
| EVO-X1, NucBox K8 Plus / K11 / K12 (32-96 GB) | AMD Ryzen AI 9 HX 370 / Ryzen 7-9 8845HS-8945HS, Radeon 890M / 780M | 32 GB: `llama3.1:8b`. 64 GB or more: `gemma3:12b` or `qwen2.5-coder:14b` | `small` / `medium` | 8192 |
| NucBox G3 / G3 Plus / G5 / G9 (8-16 GB) | Intel N100 / N150 / N97, no usable GPU | `llama3.2:3b` (runs on the CPU, slowly) | `tiny` | 4096 |

Every model here is listed in `catalog.json`, with its licence in
`MODEL-LICENSES.md`. A model that is too big runs mostly on the CPU and is
very slow. Start smaller; you can pull a bigger one later.

## 2. Install Ubuntu

GMKtec ships Windows 11. fleet-tasks needs Linux with systemd: Ubuntu Server
24.04 LTS is the simplest. Ubuntu Desktop works too if you want a screen.
WSL on Windows isn't supported.

1. On another computer, download Ubuntu Server 24.04 LTS from
   https://ubuntu.com/download/server and write it to a USB stick
   (balenaEtcher or Rufus).
2. Plug the stick into the GMKtec and power it on, pressing **F7** to get
   the boot menu (on some models it's **F12** or **Esc**). Pick the USB
   stick.
3. Install with the defaults. Choose **Install OpenSSH server** when asked,
   and write down the user name you create.
4. After the restart, log in. You can now work from your own computer:
   `ssh YOUR_USER@THE_MINI_PC_IP` (the IP is shown at login, or run
   `ip -4 addr`).

**BIOS settings worth changing** (press **Del** at power-on; the menu names
vary by model and BIOS version):

- **Power on after power loss / Restore on AC power loss: Power On.** After a
  power cut the box comes back on, and fleet-tasks starts by itself.
- **AMD models: UMA frame buffer size / iGPU memory.** This is how much RAM
  the graphics may use as VRAM. The default is often small (512 MB to
  2 GB). On an EVO-X2, set it as high as the menu allows for the model you
  chose (for example 64 GB or 96 GB on a 128 GB machine). On 780M/890M
  machines, 8 GB or 16 GB is typical. Leave enough for the system: at least
  16 GB for normal RAM.

## 3. Base packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv curl poppler-utils tzdata
```

`poppler-utils` lets tasks read attached PDFs; the rest of fleet-tasks needs
only Python 3.10 or newer (Ubuntu 24.04 has 3.12).

## 4. Ollama and your model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b          # the model you picked in step 1
ollama pull nomic-embed-text     # optional: memory search by meaning
ollama run llama3.1:8b "Say hello in five words."
ollama ps
```

`ollama ps` shows where the model ran, in the `PROCESSOR` column:

- **`100% GPU`**: good.
- **`100% CPU` on an AMD model**: Ollama didn't use the Radeon. Check
  Ollama's GPU page (https://docs.ollama.com/gpu) for your chip. For the
  Radeon 780M, the usual fix is to tell ROCm to treat it as a supported
  chip:

  ```bash
  sudo systemctl edit ollama
  # add these two lines in the space it opens, then save:
  # [Service]
  # Environment="HSA_OVERRIDE_GFX_VERSION=11.0.0"
  sudo systemctl restart ollama && ollama run llama3.1:8b "hi" && ollama ps
  ```

  Newer chips (890M, 8060S) need a recent Ollama. Update with the same
  install command. If the GPU still isn't used, the CPU works; it's just
  slower.
- **N100/N150 models** always run on the CPU, which is expected.

Ollama listens on `127.0.0.1:11434`, on this machine only, which is all
fleet-tasks needs when it runs on the same box.

## 5. Tailscale (optional, recommended)

Tailscale gives the page an HTTPS address you can open from your phone or
laptop anywhere, without opening ports on your router. It is free for
personal use. Skip this step to use the page only through SSH (step 7,
option B).

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Open the link it prints, sign in, and approve the machine. Then, in the
Tailscale admin console (https://login.tailscale.com/admin/dns), turn on
**MagicDNS** and **HTTPS Certificates**; the page's address needs both.

## 6. Get fleet-tasks and set your policy

```bash
git clone https://github.com/geoffreypboals-web/fleet-tasks.git ~/fleet-tasks
cd ~/fleet-tasks
cp policy.example.json policy.json
nano policy.json
```

In `policy.json`, find `"endpoints"` and set the model, tier and context
from step 1. For an EVO-X2 it looks like this:

```json
"endpoints": [
  {"name": "gpu-server", "url": "http://127.0.0.1:11434", "model": "qwen3.6:27b", "tier": "large", "num_ctx": 16384}
],
```

`name` is any short label; it's what the page shows. Save with **Ctrl+O**,
**Enter**, then exit with **Ctrl+X**.

The services run from `/opt/stacks/fleet-tasks` and must keep running when
you log out:

```bash
sudo mkdir -p /opt/stacks && sudo chown "$USER": /opt/stacks
sudo loginctl enable-linger "$USER"
```

Then check the machine:

```bash
python3 doctor.py
```

Fix anything it marks **MISSING** (it says how). **WARN** lines are optional
features. For example, paid AI tools that aren't installed are only a warning.

## 7. Install

Pick one way to reach the page.

**A. With Tailscale** (step 5). Use the email you sign in to Tailscale with:

```bash
cd ~/fleet-tasks
FLEET_ALLOWED_LOGINS=you@example.com ./install.sh
```

It prints the page address, `https://<machine>.<your-tailnet>.ts.net:8960`.
Open it on any device signed in to your tailnet.

**B. Without Tailscale.** The page only opens from the mini PC itself, so
you reach it through an SSH tunnel:

```bash
cd ~/fleet-tasks
FLEET_LOCAL_LOGIN=me ./install.sh
```

Then, **on your own computer**:

```bash
ssh -L 8960:127.0.0.1:8960 YOUR_USER@THE_MINI_PC_IP
```

Keep that window open and browse to http://127.0.0.1:8960. Anyone who can log
in to the mini PC can open the page this way, so keep its accounts to
yourself. Don't combine B with `tailscale serve` for the page.

## 8. First task

```bash
F=/opt/stacks/fleet-tasks/fleet.py
$F nodes
$F submit summarize "fleet-tasks runs AI work on local models first and uses paid AI only through gates."
sleep 30; $F status
```

`nodes` should show your machine as up, with its model. The task moves from
`queued` to `done`. `$F status ID` shows the answer, and it also appears on
the page.

If it stays `queued`, see Troubleshooting below.

## 9. Keep it running

**Update** to a newer version:

```bash
cd ~/fleet-tasks && git pull && ./install.sh
```

`install.sh` never overwrites your `/opt/stacks/fleet-tasks/policy.json`.
`./install.sh --policy` replaces it with the folder's copy.

**Back up** your queue and memory once a day (the databases are copied safely
while the services run):

```bash
mkdir -p ~/fleet-backups
crontab -l 2>/dev/null | { cat; echo '17 3 * * * for d in queue memory; do sqlite3 ~/.local/state/fleet-tasks/$d.db ".backup $HOME/fleet-backups/$d-$(date +\%a).db"; done'; } | crontab -
sudo apt install -y sqlite3
```

That keeps the last seven days (one file per weekday). To restore, stop the
services, copy a backup back, and start them again:

```bash
systemctl --user stop fleet-tasks fleet-web
cp ~/fleet-backups/queue-Mon.db ~/.local/state/fleet-tasks/queue.db
systemctl --user start fleet-tasks fleet-web
```

**Uninstall**: `~/fleet-tasks/uninstall.sh`. It stops and removes the
services. It keeps `/opt/stacks/fleet-tasks` (with your policy) and
`~/.local/state/fleet-tasks` (queue, memory); delete those by hand to remove
everything.

## 10. Optional extras

- **Voice input on the page** (speech to text on the CPU, about 1 GB of
  disk): `~/fleet-tasks/voice/install.sh`.
- **Paid AI fallback**: install and sign in to a vendor's CLI (Claude Code,
  Gemini CLI or GitHub Copilot CLI; see REQUIREMENTS.md). Then, in
  `/opt/stacks/fleet-tasks/policy.json`, set that tool's `"enabled": true`
  and its `daily_calls`, and restart: `systemctl --user restart fleet-tasks`.
  Tasks that would use it still wait for your approval on the page, unless
  their kind allows it.
- **Claude Code integration**: `./install.sh --mcp` (an MCP server for Claude
  Code sessions), `./install.sh --hooks` (safety hooks).
- **More machines**: install Ollama on another PC and make it listen on your
  tailnet. Add it to `endpoints` with its tailnet address,
  `http://<machine>.<your-tailnet>.ts.net:11434`.
- **Design jobs**: run
  [3d-printing-model-prompt](https://github.com/geoffreypboals-web/3d-printing-model-prompt)
  (Docker) and set `tools.print3d.url` in `policy.json` to its address.

## Troubleshooting

| What you see | Why, and the fix |
|---|---|
| `install.sh` stops with "fix the MISSING items" | `doctor.py` found something required missing; each line says how to get it. Run `python3 doctor.py` again after fixing. |
| `mkdir: cannot create directory '/opt/stacks'` | Run step 6's `sudo mkdir -p /opt/stacks && sudo chown "$USER": /opt/stacks`. |
| Services stop when you log out | Linger is off: `sudo loginctl enable-linger "$USER"`. |
| A task stays `queued` | Run `fleet.py nodes`. If your machine shows down, check `systemctl status ollama` and that `url` in policy.json is `http://127.0.0.1:11434`. If the model isn't loaded, check `model` matches `ollama list` exactly. If the task's tier is bigger than your machine's `tier`, it waits for a bigger machine or paid AI. Submit with `--tier small` (or your tier). |
| Answers are very slow | The model runs on the CPU (`ollama ps`); see step 4. Or the model is too big for the RAM: pick a smaller one from step 1. |
| The page says "Open this page through its tailnet address" | Option A: open the `https://...ts.net:8960` address, not the IP, and check your login is in `FLEET_ALLOWED_LOGINS` (`~/.config/fleet-tasks/web.env`). Option B: `FLEET_LOCAL_LOGIN` isn't set: rerun `FLEET_LOCAL_LOGIN=me ./install.sh`. |
| The page says "This tailnet user isn't allowed here" | Your Tailscale login isn't in `FLEET_ALLOWED_LOGINS`: rerun install with it (comma-separate several). |
| `tailscale serve` fails during install | Turn on MagicDNS and HTTPS Certificates in the Tailscale admin console (step 5), then rerun `./install.sh`. |
| Service logs | `journalctl --user -u fleet-tasks -n 50` and `journalctl --user -u fleet-web -n 50`. |
