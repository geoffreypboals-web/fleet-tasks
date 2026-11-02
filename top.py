#!/usr/bin/env python3
"""top.py -- the fleet on one terminal screen, for when the web page can't be used (WP-152 step 3b).

  fleet.py top                 one screen, refreshed every 5 s; type a command and Enter
  fleet.py top --watch 10      refresh every 10 s
  fleet.py top --plain         one screen and exit (for scripts and job-watch)

It shows the same data as the page (web.state(), read straight from
queue.db), so it works with fleet-web or tailscale serve down, and over a
plain ssh. Commands while it runs:

  a ID   approve a task waiting for paid AI (asks y/N)     d ID   deny it
  c ID   cancel a task                                     p      pause / resume all (asks y/N)
  s      stop / allow paid AI (asks y/N)                   u NAME clear a paid AI's usage lock (asks y/N)
  ?      this help                                         q      quit
  Enter  refresh now

Approvals are recorded as terminal:<user>, like the page records the login.
"""
import argparse
import importlib.util
import os
import select
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fleet_web", HERE / "web.py")
web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web)
fleet = web.fleet

HELP = """commands:  a ID approve   d ID deny   c ID cancel   p pause/resume   s stop/allow paid AI
           u NAME unlock a paid AI   ? help   q quit   Enter refresh   (one task: fleet.py status ID)"""
COLOURS = {"free": "32", "working": "36", "busy": "33", "down": "90", "needs_approval": "35", "failed": "31",
           "done": "32", "blocked": "31", "denied": "31", "claimed": "36"}


def who():
    return f"terminal:{os.environ.get('USER', '?')}"


def paint(text, key, colour):
    code = COLOURS.get(key)
    return f"\033[{code}m{text}\033[0m" if colour and code else text


def render(s, colour=False, width=None):
    """The whole screen as text, from web.state()."""
    width = width or max(80, shutil.get_terminal_size((120, 40)).columns)
    out = []
    flags = []
    if s["paused"]:
        flags.append(paint("PAUSED (no new work starts)", "blocked", colour))
    if s["stop"]:
        flags.append(paint("PAID AI STOPPED", "blocked", colour))
    out.append(f"fleet  {time.strftime('%H:%M:%S')}  " + ("  ".join(flags) or "running"))
    out.append("")
    out.append("MACHINES")
    for m in s["machines"]:
        out.append(f"  {m['name']:<18} {paint(m['state'].ljust(8), m['state'], colour)} {m['tier']:<6} "
                   f"{m['model']:<18} {m['why']}"[:width])
    out.append("")
    t = s["total"]
    paid = f"PAID AI TODAY  {t['used']}" + (f"/{t['limit']} all together" if t["limit"] is not None else " calls")
    for p in s["providers"]:
        paid += f"   {p['name']} " + (f"{p['used']}/{p['limit']}" if p["enabled"] else "off")
        if p["enabled"] and p.get("raised"):
            paid += f" (+{p['raised']} today)"
        if p["enabled"] and p.get("monthly") is not None:
            paid += f" ({p['month']}/{p['monthly']} month)"
    out.append(paid)
    for p in s["providers"]:
        if p.get("locked_until"):
            out.append(paint(f"  {p['name']} LOCKED until {p['locked_until'][:16].replace('T', ' ')}: "
                             f"{p['lock_reason']}"[:width], "failed", colour))
    if s.get("usage"):
        out += [("  " + x)[:width] for x in web.USAGE.lines(s["usage"])]
    if s.get("claude_turns") is not None:
        out.append(f"  your Claude sessions today: {s['claude_turns']} turns")
    out.append("")
    if s["waiting"]:
        out.append(paint("WAITING FOR YOUR OK", "needs_approval", colour) + "   (a ID approve, d ID deny)")
        for w in s["waiting"]:
            out.append(f"  #{w['id']:<5} {w['kind']:<10} {w['preview'].strip()}"[:width])
            if w.get("last_note"):
                out.append(f"         {w['last_note']}"[:width])
        out.append("")
    out.append("LATEST TASKS")
    for x in s["tasks"][:10]:
        where = x["worker"] if x["status"] == "claimed" else (x["by"] or x["tier"])
        extra = f"  {x['parts']['done']}/{x['parts']['total']} parts" if x.get("parts") else ""
        extra += f"  moved up x{x['escalations']}" if x.get("escalations") else ""
        out.append(f"  #{x['id']:<5} {x['kind']:<10} {paint(x['status_words'][:26].ljust(26), x['status'], colour)} "
                   f"{where[:28]:<28}{extra}"[:width + 12])
    if not s["tasks"]:
        out.append("  (no tasks yet: fleet.py submit summarize \"text\")")
    if s["ledger"]:
        out.append("")
        out.append("PAID AI LOG")
        for e in s["ledger"][:5]:
            out.append(f"  {str(e.get('at', '')).replace('T', ' ')}  {e.get('event', '')} {e.get('provider', '')}"
                       f"{'  #' + str(e['task']) if e.get('task') else ''}{'  ' + e['error'] if e.get('error') else ''}"[:width])
    out.append("")
    out.append(HELP)
    return "\n".join(out)


def ask_yes(prompt, read=input):
    try:
        return read(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def act(line, read=input):
    """Run one typed command. Returns (message, quit?)."""
    parts = line.strip().split()
    if not parts:
        return "", False
    cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    if cmd in ("q", "quit", "exit"):
        return "", True
    if cmd in ("?", "h", "help"):
        return HELP, False
    if cmd in ("a", "d", "c"):
        if not arg.lstrip("#").isdigit():
            return f"{cmd} needs a task number, like: {cmd} 12", False
        tid = int(arg.lstrip("#"))
        with fleet.conn() as c:
            if cmd == "c":
                ok = fleet.cancel(c, tid, by=who())
                return (f"#{tid} cancelled" if ok else f"#{tid} is already over"), False
            if cmd == "a" and not ask_yes(f"Send #{tid} to paid AI once (uses one of today's calls)?", read):
                return "not approved", False
            ok = fleet.decide(c, tid, cmd == "a", by=who())
        return (f"#{tid} {'approved' if cmd == 'a' else 'denied'}" if ok else f"#{tid} isn't waiting for an OK"), False
    if cmd == "p":
        on = not fleet.paused()
        if not ask_yes("Pause all (no machine starts new work)?" if on else "Resume?", read):
            return "unchanged", False
        fleet.set_pause(on)
        return ("paused" if on else "resumed"), False
    if cmd == "s":
        on = not (fleet.STATE / "STOP").exists()
        if not ask_yes("Stop all paid AI?" if on else "Allow paid AI again (still only with your OK)?", read):
            return "unchanged", False
        fleet.set_stop(on)
        return ("paid AI stopped" if on else "paid AI allowed"), False
    if cmd == "u":
        if arg not in fleet.policy()["frontier"]:
            return f"u needs a paid AI's name: {', '.join(fleet.policy()['frontier'])}", False
        if not ask_yes(f"Clear {arg}'s usage lock now?", read):
            return "unchanged", False
        return (f"{arg} unlocked" if fleet.unlock(arg) else f"{arg} wasn't locked"), False
    return f"unknown command {cmd!r}; ? for help", False


def watch(every=5.0, colour=True):
    msg = ""
    while True:
        sys.stdout.write("\033[2J\033[H" + render(web.state(), colour) + (f"\n\n> {msg}" if msg else "") + "\n> ")
        sys.stdout.flush()
        ready, _, _ = select.select([sys.stdin], [], [], every)
        if not ready:
            msg = ""
            continue
        line = sys.stdin.readline()
        if line == "":                       # stdin closed (Ctrl-D)
            return 0
        msg, done = act(line)
        if done:
            return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet.py top", description=__doc__.split("\n")[0])
    ap.add_argument("--watch", type=float, default=5.0, metavar="SECS", help="refresh every SECS (default 5)")
    ap.add_argument("--plain", action="store_true", help="print one screen and exit, no colour")
    a = ap.parse_args(argv)
    if a.plain or not sys.stdin.isatty():
        print(render(web.state(), colour=False))
        return 0
    try:
        return watch(max(1.0, a.watch), colour=sys.stdout.isatty())
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
