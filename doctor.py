#!/usr/bin/env python3
"""doctor.py -- check that a machine has everything fleet-tasks needs (REQUIREMENTS.md).

  python3 doctor.py                 every check, what breaks without it, how to install
  python3 doctor.py --install       what install.sh needs (it runs this first and stops on a miss)
  python3 doctor.py --install --mcp --hooks    ... plus the Claude Code parts

Exit 1 when something required is missing; warnings (an asleep machine, a
model not pulled yet, a paid-AI CLI that's switched off) never fail it.
Standard library only, so it runs before anything else is installed.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# policy.json beside this file; a fresh checkout has only policy.example.json (install.sh copies it).
POLICY = Path(os.environ.get("FLEET_POLICY") or (HERE / "policy.json" if (HERE / "policy.json").exists()
                                                else HERE / "policy.example.json"))
STATE = Path(os.environ.get("FLEET_STATE", Path.home() / ".local/state/fleet-tasks"))
MIN_PY = (3, 10)
MIN_SQLITE = (3, 22)

OK, WARN, MISS = "ok", "warn", "MISSING"


def _run(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return out.returncode, (out.stdout or out.stderr).strip().splitlines()[0:1]
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, [str(e)]


def _tool(name, version_args=("--version",)):
    """(found, first line of its version output)."""
    path = shutil.which(name)
    if not path:
        return False, ""
    code, line = _run([path, *version_args])
    return True, (line[0] if line else "")[:60]


_fleet = []


def _tailnet(url):
    """fleet.py's tailnet_url: *.ts.net names via `tailscale status`, since this
    machine may not use Tailscale's DNS."""
    try:
        if not _fleet:
            import importlib.util
            spec = importlib.util.spec_from_file_location("fleet_for_doctor", HERE / "fleet.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _fleet.append(mod)
        return _fleet[0].tailnet_url(url)
    except Exception:
        return url


def _get(url, timeout=3):
    with urllib.request.urlopen(_tailnet(url), timeout=timeout) as r:
        return json.load(r)


def checks(install=False, mcp=False, hooks=False):
    """[(status, what, detail, needed for, how to fix)]."""
    rows = []
    add = rows.append

    v = sys.version_info[:2]
    add((OK if v >= MIN_PY else MISS, "Python", f"{v[0]}.{v[1]}", "everything",
         "Ubuntu 22.04+ has 3.10; or install python3.10+"))
    sv = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    add((OK if sv >= MIN_SQLITE else MISS, "SQLite (Python's sqlite3)", sqlite3.sqlite_version,
         "the queue and memory", "comes with Python; rebuild Python against a newer libsqlite3"))

    try:
        pol = json.loads(POLICY.read_text())
        add((OK, "policy.json", str(POLICY), "everything", ""))
    except (OSError, ValueError) as e:
        pol = {}
        add((MISS, "policy.json", f"{POLICY}: {e}", "everything", "copy stacks/fleet-tasks/policy.json"))

    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / ".doctor").write_text("ok")
        (STATE / ".doctor").unlink()
        add((OK, "state folder", str(STATE), "the queue, memory, ledger", ""))
    except OSError as e:
        add((MISS, "state folder", f"{STATE}: {e}", "the queue, memory, ledger", "make it writable for this user"))
    out = Path(os.path.expanduser(((pol or {}).get("design") or {}).get("output_dir") or "~/designs"))
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / ".doctor").write_text("ok")
        (out / ".doctor").unlink()
        add((OK, "design output folder", str(out), "kept design files (Approve)", ""))
    except OSError as e:
        add((WARN, "design output folder", f"{out}: {e}", "kept design files (Approve)",
             "make it writable, or set policy.json design.output_dir"))

    # services and the page
    found, ver = _tool("systemctl")
    user_ok = found and _run(["systemctl", "--user", "is-system-running"])[0] in (0, 1)
    add((OK if user_ok else (MISS if install else WARN), "systemd --user", ver or "not found",
         "running the workers and page as services", "a systemd Linux (Ubuntu 22.04+); "
         "for a headless user also: sudo loginctl enable-linger $USER"))
    found, ver = _tool("tailscale", ("version",))
    local_only = bool(os.environ.get("FLEET_LOCAL_LOGIN"))   # install.sh FLEET_LOCAL_LOGIN=...: no Tailscale needed
    add((OK if found else (MISS if install and not local_only else WARN), "tailscale", ver or "not found",
         "the web page over the tailnet (tailscale serve), phone alerts (Taildrop), other machines",
         "https://tailscale.com/download/linux, then: tailscale up (or install with FLEET_LOCAL_LOGIN=me)"))

    # local machines
    eps = pol.get("endpoints", [])
    try:
        catalog = json.loads(Path(os.environ.get("FLEET_CATALOG", HERE / "catalog.json")).read_text())
    except (OSError, ValueError):
        catalog = {}
        add((WARN, "catalog.json", "missing or unreadable", "picking the best model per kind (WP-152 step 3e)",
             "run install.sh"))
    if not eps:
        add((MISS, "Ollama endpoints", "none in policy.json", "all local work", "list at least one in policy.json"))
    up = 0
    embed = (pol.get("memory") or {}).get("embed_model", "nomic-embed-text")
    embed_on = (pol.get("memory") or {}).get("embed_on") or [e["name"] for e in eps if "127.0.0.1" in e.get("url", "")]
    embed_seen = False
    for e in eps:
        try:
            names = [m.get("name", "") for m in _get(e["url"].rstrip("/") + "/api/tags").get("models", [])]
        except (OSError, ValueError, KeyError):
            add((WARN, f"Ollama {e.get('name')}", f"{e.get('url')} not answering", "work on that machine",
                 "start Ollama there (it may just be asleep); install: https://ollama.com/download"))
            continue
        up += 1
        has = e.get("model") in names or f"{e.get('model')}:latest" in names
        add((OK if has else WARN, f"Ollama {e['name']}", f"up, {e['model']} {'pulled' if has else 'NOT pulled'}",
             "work on that machine", f"ollama pull {e['model']} (on that machine)"))
        if e["name"] in embed_on and any(n.split(":")[0] == embed.split(":")[0] for n in names):
            embed_seen = True
        missing = [m["name"] for m in catalog.get("models", []) if e["name"] in m.get("endpoints", [])
                   and m["name"] not in names and f"{m['name']}:latest" not in names]
        if missing:
            add((WARN, f"catalogue models on {e['name']}", "not pulled: " + ", ".join(missing),
                 "routing those models there (catalog.json; the machine's own model still works)",
                 "ollama pull NAME on that machine, or drop it from that model's endpoints in catalog.json"))
    if eps and not up:
        add((WARN, "Ollama (any)", "no machine answering now", "all local work",
             "nothing runs until one is up; the queue keeps tasks meanwhile"))
    add((OK if embed_seen else WARN, f"embedding model {embed}", "pulled" if embed_seen else "not found on embed_on machines",
         "memory search by meaning (without it: search by words)", f"ollama pull {embed}"))

    # research tasks: public web pages over HTTPS
    try:
        import socket as _s
        ip = _s.getaddrinfo("example.com", 443, type=_s.SOCK_STREAM)[0][4][0]
        _s.create_connection((ip, 443), 5).close()
        add((OK, "outbound HTTPS", f"example.com reachable ({ip})", "", ""))
    except OSError as e:
        add((WARN, "outbound HTTPS", f"not reachable ({e})", "research tasks (reading public web pages)",
             "allow outbound HTTPS (443) and DNS from this machine"))

    # code sources
    found, ver = _tool("git")
    add((OK if found else WARN, "git", ver or "not found", "tasks given code (--repo/--path)", "sudo apt install git"))
    found, ver = _tool("gh")
    authed = found and _run(["gh", "auth", "status"])[0] == 0
    add((OK if authed else WARN, "gh (GitHub CLI)", (ver + (", signed in" if authed else ", NOT signed in")) if found
         else "not found", "tasks given a PR (--pr); Copilot's plan usage on the page (usage.py)", "https://cli.github.com, then: gh auth login"))

    # attached documents
    found, ver = _tool("pdftotext", ("-v",))
    add((OK if found else WARN, "pdftotext (poppler-utils)", ver or "not found", "attached PDF documents",
         "sudo apt install poppler-utils"))

    # paid AI (only the ones switched on are needed)
    for name, prov in (pol.get("frontier") or {}).items():
        cmd = list(prov.get("cmd") or [name])
        while cmd and (os.path.basename(cmd[0]) == "env" or "=" in cmd[0] or cmd[0].startswith("-")):
            cmd = cmd[2:] if cmd[0] in ("-C", "-u", "-S", "--chdir", "--unset") else cmd[1:]
        # env -C DIR HOME=... gemini ... -> gemini
        tool = cmd[0] if cmd else name
        found, ver = _tool(tool)
        need = prov.get("enabled")
        add((OK if found else (WARN if not need else MISS if install else WARN), f"paid AI: {tool}",
             (ver or "not found") + ("" if need else " (switched off in policy.json)"),
             f"{name} calls, after the owner's OK",
             {"claude": "https://claude.com/claude-code (npm i -g @anthropic-ai/claude-code), then sign in: claude",
              "gemini": "npm i -g @google/gemini-cli, then sign in once: gemini",
              "copilot": "https://docs.github.com/copilot (GitHub Copilot CLI), then: copilot login"}.get(name, "")))

        if found and prov.get("expect_version"):
            code, line = _run(list(prov.get("version_cmd") or [tool, "--version"]), timeout=60)
            have = line[0] if line else ""
            add((OK if prov["expect_version"] in have else WARN, f"paid AI: {name} version",
                 f"{have or 'unknown'} (checked: {prov['expect_version']})",
                 f"{name} calls (a version nobody checked is locked out, not called)",
                 "run the text-only check in RUNBOOK.md, then set expect_version in policy.json"))
        if "--available-tools=" in (prov.get("cmd") or []):
            add((MISS if need else WARN, f"paid AI: {name} tool filter", "--available-tools= is empty: every tool is on",
                 f"{name} calls (fleet.py refuses them)", "name a tool that doesn't exist: --available-tools=fleet_text_only"))

    # time zone data (a Gemini lock ends at Pacific midnight)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Los_Angeles")
        add((OK, "time zone data", "zoneinfo has America/Los_Angeles", "", ""))
    except Exception as e:
        add((WARN, "time zone data", f"missing ({e.__class__.__name__})",
             "paid-AI lock times (Gemini's ends at Pacific midnight; without it: this machine's midnight)",
             "sudo apt install tzdata"))

    # Claude Code parts
    if mcp or hooks or not install:
        found, ver = _tool("claude")
        add((OK if found else (MISS if (mcp or hooks) else WARN), "Claude Code (claude)", ver or "not found",
             "install.sh --mcp (registers the fleet MCP server) and --hooks",
             "https://claude.com/claude-code"))

    # shared PCs (node/gpu_free.py), only where gpu_probe is used
    for e in (e for e in eps if e.get("gpu_probe")):
        try:
            _get(e["gpu_probe"])
            add((OK, f"GPU check {e['name']}", e["gpu_probe"], "", ""))
        except (OSError, ValueError):
            add((WARN, f"GPU check {e['name']}", f"{e['gpu_probe']} not answering",
                 "fleet work on that shared PC (it's left alone until this answers)",
                 "on that PC: run node/gpu_free.py (needs nvidia-smi from the NVIDIA driver); owner's OK first"))
    rows += tool_checks(pol)
    return rows


TOOLS = Path(os.environ.get("FLEET_TOOLS", HERE / "tools.json"))


def load_tools():
    """tools.json's entries (every outside tool beyond the core), or [] when it's missing."""
    try:
        return json.loads(TOOLS.read_text()).get("tools", [])
    except (OSError, ValueError):
        return []


def tool_url(t, pol):
    """A service's address: policy.json tools.<id>.url, else the list's default."""
    return ((pol.get("tools") or {}).get(t["id"]) or {}).get("url") or t["check"].get("default_url", "")


def _ollama_models(pol):
    names = set()
    for e in pol.get("endpoints", []):
        try:
            names |= {m.get("name", "") for m in _get(e["url"].rstrip("/") + "/api/tags").get("models", [])}
        except (OSError, ValueError, KeyError):
            pass
    return names


def check_tool(t, pol, seen):
    """(status, detail) for one tools.json entry. `seen` keeps each service's
    JSON for the tools inside it (OpenSCAD inside the 3D print tool)."""
    c = t["check"]
    kind = c["type"]
    if kind == "program":
        found, ver = _tool(c["cmd"], tuple(c.get("version_args", ["--version"])))
        return (OK, ver or "found") if found else (WARN, "not found")
    if kind == "http_json":
        url = tool_url(t, pol)
        try:
            data = _get(url.rstrip("/") + c.get("path", ""))
        except (OSError, ValueError) as e:
            seen[t["id"]] = None
            return WARN, f"{url} not answering ({e.__class__.__name__})"
        seen[t["id"]] = data if isinstance(data, dict) else {}
        off = [f for f in c.get("fields", []) if not seen[t["id"]].get(f)]
        return (WARN, f"up at {url}; not available: {', '.join(off)}") if off else (OK, f"up at {url}")
    if kind == "inside":
        data = seen.get(c["of"])
        if data is None:
            return WARN, f"{c['of']} isn't answering, so this can't be checked"
        return (OK, f"available in {c['of']}") if data.get(c["field"]) else (WARN, f"{c['of']} reports it missing")
    if kind == "venv":
        py = os.path.expanduser(c["python"])
        if not os.path.exists(py):
            return WARN, f"{c['python']} not found"
        code_ = "import " + ", ".join(c["imports"])
        if c.get("cuda"):
            code_ += "; import torch; assert torch.cuda.is_available(), 'no GPU visible'; print(torch.cuda.device_count(), 'GPU')"
        code, line = _run([py, "-c", code_], timeout=120)
        return (OK, "imports ok" + (f", {line[0]}" if line and c.get("cuda") else "")) if code == 0 else \
            (WARN, f"can't import or no GPU: {(line[0] if line else '')[:80]}")
    if kind == "ollama_model":
        names = _ollama_models(pol)
        have = [m for m in c["models"] if m in names or f"{m}:latest" in names]
        return (OK, "pulled: " + ", ".join(have)) if have else (WARN, "none of " + ", ".join(c["models"]) + " pulled")
    if kind == "secret":
        if os.environ.get(c["name"]):
            return OK, f"{c['name']} set in the environment"
        if shutil.which("fleet-secret"):
            try:
                out = subprocess.run(["fleet-secret", "list", "--service", c["service"]], capture_output=True,
                                     text=True, timeout=30, check=False).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            if c["name"] in out.split():
                return OK, f"{c['name']} stored for {c['service']} (name only checked)"
        return WARN, f"no {c['name']} stored (fleet-secret, service {c['service']})"
    if kind == "gpu":
        code, _ = _run(["nvidia-smi", "--version"])
        if code != 0:
            return WARN, "no nvidia-smi (no NVIDIA driver)"
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=30, check=False).stdout
        gpus = [[x.strip() for x in ln.split(",")] for ln in out.splitlines() if ln.count(",") >= 2]
        big = [g for g in gpus if g[1].isdigit() and int(g[1]) >= c["min_mib"]]
        drv = gpus[0][2] if gpus else ""
        if not big:
            return WARN, f"no GPU with {c['min_mib']} MiB+ ({len(gpus)} found)"
        if int(drv.split(".")[0] or 0) < int(c.get("min_driver", "0")):
            return WARN, f"driver {drv} older than {c['min_driver']}"
        return OK, f"{len(big)} x {big[0][0]} ({big[0][1]} MiB), driver {drv}"
    return WARN, "check by hand: " + c.get("how", "")


def tool_checks(pol):
    """Rows for tools.json: every tool is optional (a feature waits without it), so a miss warns."""
    rows, seen = [], {}
    for t in load_tools():
        try:
            st, detail = check_tool(t, pol, seen)
        except Exception as e:                       # one odd tool never stops the rest
            st, detail = WARN, f"check failed: {e.__class__.__name__}"
        note = f" [licence: {t['licence']}" + (f"; {t['licence_note']}" if t.get("licence_note") else "") + "]" \
            if t.get("commercial") != "yes" else ""
        rows.append((st, f"tool: {t['name']}"[:60], detail, "; ".join(t["used_for"]) + note, t["install"]))
    return rows


def tools_markdown(tools=None):
    """The tool table in REQUIREMENTS.md, made from tools.json (doctor.py --tools-md)."""
    out = ["| Tool | Kind | Used for | Licence | Commercial use | Get it |", "|---|---|---|---|---|---|"]
    for t in tools if tools is not None else load_tools():
        com = {"yes": "yes", "conditions": "with conditions", "no": "**NO (non-commercial)**"}.get(t["commercial"], t["commercial"])
        if t.get("licence_note"):
            com += ": " + t["licence_note"]
        cell = lambda s: str(s).replace("|", "\\|")
        out.append("| " + " | ".join(cell(x) for x in (t["name"], t["kind"], "; ".join(t["used_for"]), t["licence"],
                                                      com, t["install"])) + " |")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--install", action="store_true", help="what install.sh needs; exit 1 on a miss")
    ap.add_argument("--mcp", action="store_true")
    ap.add_argument("--hooks", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tools-md", action="store_true", help="print REQUIREMENTS.md's tool table, made from tools.json")
    a = ap.parse_args(argv)
    if a.tools_md:
        print(tools_markdown())
        return 0
    rows = checks(a.install, a.mcp, a.hooks)
    if a.json:
        print(json.dumps([dict(zip(("status", "what", "detail", "for", "fix"), r)) for r in rows], indent=1))
    else:
        for st, what, detail, need, fix in rows:
            print(f"{st:<8} {what:<34} {detail}")
            if st != OK:
                print(f"{'':<8} {'':<34} needed for: {need}" + (f"\n{'':<8} {'':<34} fix: {fix}" if fix else ""))
    missing = [r for r in rows if r[0] == MISS]
    if missing and not a.json:
        print(f"\n{len(missing)} required item(s) missing; see REQUIREMENTS.md.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
