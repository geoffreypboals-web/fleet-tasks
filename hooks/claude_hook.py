#!/usr/bin/env python3
"""claude_hook.py -- three small Claude Code hooks for gpu-server (board WP-152 step 3).

  pretool-bash   PreToolUse on Bash: refuse a short list of commands that
                 destroy a machine or a branch, or print a secret into the
                 transcript (WP-153: sops/age decrypt to the screen, the
                 rendered /run/fleet-secrets files, age private keys, bw get). It only ever refuses. Every
                 other command gets no answer from here, so Claude Code's own
                 permission prompts and modes decide, as before.
  prompt         UserPromptSubmit: when a prompt asks for work the local
                 fleet does for free (summarise, draft, classify, review
                 text, translate), add one line saying so. No model call.
  stop           Stop (optional): count Claude turns per day in
                 ~/.local/state/fleet-tasks/claude-usage.jsonl, for the page.
                 It only counts; it never blocks.
  install [--usage] | uninstall
                 add or remove these hooks in ~/.claude/settings.json (a
                 dated backup is written first).

Every hook fails open: on any error it exits 0 with no output, so a bug
here can never stop a session. Standard library only, no network.
"""
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve()
STATE = Path(os.environ.get("FLEET_STATE", Path.home() / ".local/state/fleet-tasks"))
SETTINGS = Path(os.environ.get("CLAUDE_SETTINGS", Path.home() / ".claude/settings.json"))
MARK = "fleet-tasks/hooks/claude_hook.py"       # how install finds its own entries

# ---------------- pretool-bash: the refusal list ----------------
# The idea comes from Ruflo's dangerous-command list (MIT, ruvnet/claude-flow);
# these rules are written for this machine and checked per command segment.

HOME = str(Path.home())
ROOTISH = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "${HOME}", "${HOME}/", "${HOME}/*",
           HOME, HOME + "/", HOME + "/*", "/home", "/home/", "/home/*", "/root", "/etc", "/usr", "/var", "/boot",
           "/bin", "/lib", "/opt", "/data"}
DISK = re.compile(r"^/dev/(sd[a-z]|nvme\d|hd[a-z]|vd[a-z]|xvd[a-z]|mmcblk\d|md\d|dm-\d|disk\d)")
FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+(-\S+\s+)*)?(env\s+\S+\s+)*(ba|z|da|k|fi)?sh\b")
DROP_DB = re.compile(r"\bdrop\s+(database|schema)\b", re.IGNORECASE)
TO_DISK = re.compile(r">\s*/dev/(sd[a-z]|nvme\d|hd[a-z]|vd[a-z]|mmcblk\d)")
SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n|&(?!>))\s*")
DB_CLIENT = re.compile(r"\b(psql|mysql|mariadb|sqlite3|mongosh|cockroach|clickhouse-client)\b")
WRAPPERS = {"sudo", "doas", "env", "nice", "nohup", "time", "command", "exec", "xargs", "timeout"}
WRAPPER_ARG = {"-u", "-g", "-C", "-n", "-p", "-s", "-k"}       # wrapper options that take a value
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# Secrets (board WP-153 part 3): whatever a command prints lands in the session's transcript.
SECRET_PATH = re.compile(r"(/run/(user/\d+/)?fleet-secrets\b|\$\{?XDG_RUNTIME_DIR\}?/fleet-secrets\b|"
                         r"(^|/)\.config/sops/age/keys\.txt\b|(^|/)sops/age/keys\.txt\b|recovery[-_]?key[^/\s]*\.txt\b)")
SECRET_OK = {"ls", "stat", "test", "[", "rm", "shred", "fleet-secret", "systemctl", "mkdir", "chmod", "chown"}


def _redirected(argv):
    """True when the segment's output goes to a file (> f, >> f, 1> f), not the screen."""
    return any(re.fullmatch(r"(1?>>?|&>>?)", a) or re.match(r"(1?>>?|&>>?)\S", a) for a in argv[1:])


def secret_refusal(argv, seg):
    name = os.path.basename(argv[0])
    words = argv[1:]
    if name == "sops":
        if any(w in ("exec-env", "exec-file") for w in words[:2]):
            return "sops exec-env/exec-file in a Claude session (the program could print the secrets)"
        decrypting = words[:1] == ["decrypt"] or "-d" in words or "--decrypt" in words
        if decrypting and not ("--output" in words or any(w.startswith("--output=") for w in words) or _redirected(argv)):
            return "sops decrypting to the screen (it would land in the transcript): decrypt to a file, or use fleet-secret render"
    if name in ("age", "rage") and ("-d" in words or "--decrypt" in words) and not (
            "-o" in words or "--output" in words or any(w.startswith("--output=") for w in words) or _redirected(argv)):
        return "age decrypting to the screen: use -o FILE"
    if name == "bw" and words[:1] in (["get"], ["list"]) or name == "bw" and words[:1] == ["export"] and \
            not any(w == "--output" or w.startswith("--output=") for w in words):
        return f"bw {words[0]} prints vault items into the transcript: open the Bitwarden app instead"
    if SECRET_PATH.search(seg) and name not in SECRET_OK and not any("fleet_secret.py" in w for w in argv):
        return ("reading rendered secrets or an age private key in a Claude session (it would land in the "
                "transcript); programs read them themselves, people use Vaultwarden")
    return None


def _argv(segment):
    """The command's words, after wrappers: 'sudo -u x env A=1 rm -rf /' -> ['rm', '-rf', '/']."""
    try:
        argv = shlex.split(segment, comments=True)
    except ValueError:
        argv = segment.split()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in WRAPPERS or ASSIGN.match(a) or (i and argv[i - 1] == "timeout" and a[:1].isdigit()):
            i += 1
        elif a.startswith("-") and i and os.path.basename(argv[i - 1]) in WRAPPERS | WRAPPER_ARG | {a}:
            i += 2 if a in WRAPPER_ARG else 1
        else:
            break
    return argv[i:]


def _flags(argv):
    short, long_ = set(), set()
    for a in argv[1:]:
        if a == "--":
            break
        if a.startswith("--"):
            long_.add(a)
        elif a.startswith("-") and len(a) > 1:
            short.update(a[1:])
    return short, long_


def _targets(argv):
    """The words that aren't options (everything after a bare --)."""
    out, after = [], False
    for a in argv[1:]:
        if a == "--" and not after:
            after = True
        elif after or not a.startswith("-"):
            out.append(a)
    return out


def refusal(command):
    """Why this Bash command is refused, or None (no opinion)."""
    if FORK_BOMB.search(command):
        return "a fork bomb"
    if PIPE_TO_SHELL.search(command):
        return "piping a download straight into a shell (download it, read it, then run it)"
    if DROP_DB.search(command) and DB_CLIENT.search(command):
        return "DROP DATABASE / DROP SCHEMA"
    if TO_DISK.search(command):
        return "writing straight onto a disk device"
    for seg in SPLIT.split(command):
        argv = _argv(seg)
        if not argv:
            continue
        name = os.path.basename(argv[0])
        short, long_ = _flags(argv)
        targets = _targets(argv)
        if name == "rm" and ({"r", "R"} & short or "--recursive" in long_) and \
                ("f" in short or "--force" in long_) and any(t in ROOTISH for t in targets):
            return f"rm -rf on {next(t for t in targets if t in ROOTISH)}"
        if name == "rm" and "--no-preserve-root" in long_:
            return "rm --no-preserve-root"
        if name.startswith("mkfs") or (name in ("wipefs", "shred") and any(DISK.match(t) for t in targets)):
            return f"{name} (formats or wipes a disk)"
        if name == "dd" and any(a.startswith("of=") and DISK.match(a[3:]) for a in argv[1:]):
            return "dd onto a disk device"
        if name in ("chmod", "chown", "chgrp") and ({"R"} & short or "--recursive" in long_) and \
                any(t in ROOTISH for t in targets):
            return f"{name} -R on {next(t for t in targets if t in ROOTISH)}"
        why = secret_refusal(argv, seg)
        if why:
            return why
        if name == "git" and len(argv) > 1 and argv[1] == "push":
            force = ("f" in short or "--force" in long_) and not any(x.startswith("--force-with-lease") for x in long_)
            refs = [t for t in targets[1:] if t != "push"]
            hits_main = any(re.fullmatch(r"\+?(refs/heads/)?(main|master)|.*:(refs/heads/)?(main|master)", t) for t in refs)
            plus_main = any(re.fullmatch(r"\+(refs/heads/)?(main|master)|\+.*:(refs/heads/)?(main|master)", t) for t in refs)
            if (force and hits_main) or plus_main:
                return "force-pushing main/master"
    return None


def pretool_bash(event):
    if event.get("tool_name") != "Bash":
        return None
    why = refusal(str((event.get("tool_input") or {}).get("command") or ""))
    if not why:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"Refused by the gpu-server safety hook: {why}. "
                                   "If this is really wanted, the owner runs it by hand."}}


# ---------------- prompt: the fleet hint ----------------

HINTS = [  # (pattern, kind); first match wins. Words only: no model call, no network.
    (re.compile(r"\b(summari[sz]e|summary of|tl;?dr|condense|boil (it|this) down)\b", re.I), "summarize"),
    (re.compile(r"\b(classify|categori[sz]e|triage|sort (these|this|them) into|label (these|each))\b", re.I), "classify"),
    (re.compile(r"\b(proofread|copy-?edit|check (the |this |my )?(text|wording|grammar|spelling|chapter|draft)|"
                r"review (the |this |my )?(text|chapter|draft|document|doc|essay|article|copy))\b", re.I), "review"),
    (re.compile(r"\b(translate|draft (a|an|the|me)|write (a|an|me a) (first )?draft|rewrite (this|the|it)|"
                r"reword|paraphrase)\b", re.I), "draft"),
]
MIN_CHARS = 12


def prompt(event):
    text = str(event.get("prompt") or "")
    if len(text.strip()) < MIN_CHARS or text.lstrip().startswith("/"):
        return None
    for pat, kind in HINTS:
        if pat.search(text):
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                    f"The local fleet can do this at no cost: fleet_submit kind={kind} (MCP server 'fleet'; "
                    f"fleet_split for long text), then fleet_result. Use paid models only if the local "
                    f"answer isn't good enough."}}
    return None


# ---------------- stop: count turns ----------------

def stop(event):
    STATE.mkdir(parents=True, exist_ok=True)
    line = {"at": datetime.now().isoformat(timespec="seconds"), "session": str(event.get("session_id") or "")[:64]}
    with open(STATE / "claude-usage.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")
    return None


def turns_today(path=None):
    """Claude turns counted today (for the page), or None when not counted."""
    path = path or STATE / "claude-usage.jsonl"
    if not path.exists():
        return None
    day = datetime.now().date().isoformat()
    n = 0
    with open(path) as f:
        for line in f:
            n += line.startswith('{"at": "' + day)
    return n


# ---------------- install / uninstall ----------------

def _entry(sub, matcher=None, timeout=2):
    e = {"hooks": [{"type": "command", "command": f"/usr/bin/python3 {HERE} {sub}", "timeout": timeout}]}
    if matcher:
        e["matcher"] = matcher
    return e


def _strip(hooks):
    """Settings hooks without this file's entries (other hooks are kept as they are)."""
    out = {}
    for event, groups in (hooks or {}).items():
        kept = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if hs:
                kept.append(dict(g, hooks=hs))
        if kept:
            out[event] = kept
    return out


def install(usage=False):
    s = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name(SETTINGS.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    hooks = _strip(s.get("hooks"))
    hooks.setdefault("PreToolUse", []).append(_entry("pretool-bash", "Bash"))
    hooks.setdefault("UserPromptSubmit", []).append(_entry("prompt"))
    if usage:
        hooks.setdefault("Stop", []).append(_entry("stop"))
    s["hooks"] = hooks
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    tmp.replace(SETTINGS)
    return sorted(e for e, g in hooks.items() if any(MARK in h.get("command", "") for x in g for h in x["hooks"]))


def uninstall():
    if not SETTINGS.exists():
        return
    s = json.loads(SETTINGS.read_text())
    shutil.copy2(SETTINGS, SETTINGS.with_name(SETTINGS.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    hooks = _strip(s.get("hooks"))
    if hooks:
        s["hooks"] = hooks
    else:
        s.pop("hooks", None)
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    tmp.replace(SETTINGS)


HANDLERS = {"pretool-bash": pretool_bash, "prompt": prompt, "stop": stop}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd == "install":
        print("hooks on: " + ", ".join(install("--usage" in argv)) + f" (in {SETTINGS})")
        return 0
    if cmd == "uninstall":
        uninstall()
        print(f"hooks removed from {SETTINGS}")
        return 0
    try:                                        # a hook: fail open, always exit 0
        event = json.loads(sys.stdin.read() or "{}")
        out = HANDLERS[cmd](event) if cmd in HANDLERS else None
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
