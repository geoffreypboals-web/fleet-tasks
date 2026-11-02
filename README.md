# fleet-tasks

A local-first AI task queue for your own machines. You hand it work (summarise
this, review that code, research a question, draft a reply), and it runs the
work on the local models you already have in [Ollama](https://ollama.com).
Paid AI (Claude, Gemini, Copilot, through your own accounts) is a fallback.
It is off by default and only used through gates you set.

- **One queue, many machines.** Each Ollama server gets a worker. A task goes
  to the cheapest machine big enough for it, so a machine that is idle picks
  up work and a sleeping one is skipped.
- **Checked answers.** A second model can check an answer. A failed check
  moves the task up a tier (tiny, small, medium, large) before any paid AI
  is involved.
- **Paid AI behind gates.** Each paid tool needs your approval or an allowed
  task kind. It also has a daily call budget, a prompt size cap, a secret
  scan and a "Stop paid AI" switch. Every paid call is logged.
- **A web page** with the queue, what each machine is doing and why, approve
  and deny buttons, today's budget and a stop switch. There's also a
  terminal view (`fleet.py top`).
- **Work with real material.** You can attach code from a git checkout,
  a PR (`gh`), documents (Word, PDF, HTML), or public web pages for research.
  Web pages go through an AI-defence screen that cuts prompt-injection text.
- **Split big jobs.** A large input is split across machines and the answers
  merged.
- **Shared memory.** Notes you and the fleet can search by meaning
  (`fleet.py remember` / `recall`).
- **Claude Code integration (optional):** an MCP server (`fleet_submit`,
  `fleet_result`, and more), so Claude Code sessions can hand cheap work to
  your local models. There are also safety hooks that refuse dangerous shell
  commands.
- **Design jobs (optional):** turns an idea into checked 3D-printable parts,
  with the companion
  [3d-printing-model-prompt](https://github.com/geoffreypboals-web/3d-printing-model-prompt)
  service.

Python standard library only (3.10+): no `pip install` to run it. It runs as
two systemd user services on any Linux machine with systemd.

## Deploy

- **On a GMKtec mini PC** (or any small Linux box): follow
  [DEPLOY-GMKTEC.md](DEPLOY-GMKTEC.md), step by step, from a new machine to
  your first answered task.
- **What a machine needs**, row by row: [REQUIREMENTS.md](REQUIREMENTS.md).
  `python3 doctor.py` checks every row and says how to fix what's missing.

The short version, on Ubuntu 22.04 or 24.04 with Ollama installed and a model
pulled:

```bash
git clone https://github.com/geoffreypboals-web/fleet-tasks.git ~/fleet-tasks
cd ~/fleet-tasks
cp policy.example.json policy.json        # then set your model and tier in "endpoints"
sudo mkdir -p /opt/stacks && sudo chown "$USER": /opt/stacks
sudo loginctl enable-linger "$USER"
python3 doctor.py
FLEET_LOCAL_LOGIN=me ./install.sh         # or, with Tailscale: FLEET_ALLOWED_LOGINS=you@example.com ./install.sh
/opt/stacks/fleet-tasks/fleet.py submit summarize "fleet-tasks is a local-first AI task queue."
/opt/stacks/fleet-tasks/fleet.py status
```

## Use

```bash
fleet.py nodes                                   # machines, tiers, what's loaded
fleet.py submit summarize "text"                 # kinds: triage, summarize, classify, draft, review, code, research, hard
fleet.py submit review "what to check" --repo my-app --path src/api.py
fleet.py submit research "question" --url https://example.com/page
fleet.py status [ID]                             # the queue, or one task with its answer
fleet.py approve ID | deny ID                    # tasks waiting for paid AI
fleet.py budget                                  # paid calls used today, per tool
fleet.py stop on | off                           # no paid AI at all while on
fleet.py pause | resume                          # no new work while paused
fleet.py top                                     # everything on one terminal screen
fleet.py remember "note" | recall "question"     # shared memory
```

`fleet.py --help` lists everything. The page (port 8960) does the same with
buttons, and each control has a tooltip.

## Configure

`policy.json` (copied from `policy.example.json`, never overwritten by
`install.sh`; `install.sh --policy` replaces it):

- **`endpoints`**: one entry per Ollama server, with `url`, `model` and
  `tier` (`tiny` up to 4B, `small` 7-8B, `medium` 12-14B, `large` 24-32B).
  You can add more machines later; a machine that is off is skipped.
- **`kinds`**: where each kind of task starts, and which paid tools it may
  climb to.
- **`frontier`**: the paid AI command-line tools, each `enabled: false`
  until you turn it on. Each has a `daily_calls` budget.

`catalog.json` lists what each model is good for. The fleet uses it to pick
the best model you have for each kind of task.

Environment settings (set them in the systemd unit, or in
`~/.config/fleet-tasks/web.env` for the page):

| Setting | What it does | Default |
|---|---|---|
| `FLEET_ALLOWED_LOGINS` | Tailscale logins that may open the page (comma separated) | none |
| `FLEET_LOCAL_LOGIN` | Without Tailscale: the name a request from this machine gets in as (open the page through an SSH tunnel) | off |
| `FLEET_URL` | the page address used in links and alerts | `http://127.0.0.1:8960` |
| `FLEET_PHONE` | a Tailscale device to send approval alerts to (Taildrop) | `off` |
| `FLEET_POLICY`, `FLEET_CATALOG` | other policy or catalog files | the ones beside `fleet.py` |
| `FLEET_STATE` | where the queue and memory live | `~/.local/state/fleet-tasks` |

## Your data

Everything stays on your machine, in `~/.local/state/fleet-tasks/`: the queue
(`queue.db`), memory (`memory.db`) and the paid-AI ledger. Nothing is sent
anywhere except to the Ollama servers in your policy, and to a paid AI tool
only when its gates allow it. Back up that folder (DEPLOY-GMKTEC.md, step 9).

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest tests -q
```

The tests use their own policy and catalog (`tests/policy.json`,
`tests/catalog.json`: a multi-machine example), never yours.

## Licence

Apache-2.0 ([LICENSE](LICENSE)). Third-party code and notices:
[NOTICE](NOTICE). Model licences: [MODEL-LICENSES.md](MODEL-LICENSES.md).
fleet-tasks ships no model weights; each install pulls its own.
