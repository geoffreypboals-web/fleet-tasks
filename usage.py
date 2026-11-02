#!/usr/bin/env python3
"""usage.py -- how much of each paid AI's plan is used, read without spending any of it.

No AI is asked anything here (owner 2026-10-28: "show the information about your
usage without increasing the usage"):

  copilot  GitHub's own count of the plan's premium credits, from the GitHub API
           (gh api /copilot_internal/user: a GitHub call, not a Copilot request).
           It isn't a published API, so a change on GitHub's side shows as "unknown"
           rather than breaking the page. It counts all Copilot use on the account,
           not only the fleet's.
  claude   tokens in the Claude Code session logs on this machine (~/.claude/projects),
           today and this month. Anthropic doesn't offer the plan's percentage used
           without a session, so this is the machine's own count.
  gemini   Google reports no remaining quota without a request; the fleet's call
           log (fleet.py budget) is the count.

The result is cached in usage.json for 15 minutes; snapshot(refresh=True) reads again.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(os.environ.get("FLEET_STATE", Path.home() / ".local/state/fleet-tasks"))
CLAUDE_LOGS = Path(os.environ.get("FLEET_CLAUDE_LOGS", Path.home() / ".claude/projects"))
MAX_AGE = 900


def copilot(run=subprocess.run):
    """The Copilot plan's premium credits, from GitHub. {"error": ...} when it can't be read."""
    try:
        out = run(["gh", "api", "/copilot_internal/user"], capture_output=True, text=True, timeout=20,
                  stdin=subprocess.DEVNULL)
        d = json.loads(out.stdout)
        q = d["quota_snapshots"]["premium_interactions"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as e:
        return {"error": f"couldn't read GitHub's count ({type(e).__name__})"}
    if q.get("unlimited"):
        return {"plan": d.get("copilot_plan", ""), "unlimited": True}
    total = q.get("entitlement") or 0
    used = q.get("credits_used", total - (q.get("remaining") or 0))
    return {"plan": d.get("copilot_plan", ""), "used": round(used), "of": total,
            "left_pct": q.get("percent_remaining"), "resets": str(d.get("quota_reset_date_utc") or d.get("quota_reset_date") or "")[:10],
            "overage_on": bool(q.get("overage_permitted")), "overage_max": q.get("overage_entitlement") or 0,
            "overage_used": q.get("overage_count") or 0}


def _local_day(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone().strftime("%Y-%m-%d")


def _claude_file(path):
    """{day: {"turns", "tokens", "cache_read"}} for one session log; a reply split over
    several lines (one per content block) counts once."""
    days, seen = {}, set()
    try:
        f = open(path, errors="replace")
    except OSError:
        return days
    with f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
                m = d["message"]
                u = m["usage"]
            except (ValueError, KeyError, TypeError):
                continue
            key = (m.get("id"), d.get("requestId"))
            if key != (None, None):
                if key in seen:
                    continue
                seen.add(key)
            day = _local_day(d.get("timestamp"))
            if not day:
                continue
            s = days.setdefault(day, {"turns": 0, "tokens": 0, "cache_read": 0})
            s["turns"] += 1
            s["tokens"] += sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))
            s["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    return days


def claude(logs=None, cache=None):
    """Claude Code tokens on this machine, today and this month. `cache` (path ->
    [mtime, size, days]) skips logs that haven't changed since the last read."""
    logs = Path(logs or CLAUDE_LOGS)
    cache = {} if cache is None else cache
    month, today = datetime.now().strftime("%Y-%m"), datetime.now().strftime("%Y-%m-%d")
    start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() - 86400
    tot = {"today": {"turns": 0, "tokens": 0, "cache_read": 0}, "month": {"turns": 0, "tokens": 0, "cache_read": 0}}
    live = {}
    for path in (logs.rglob("*.jsonl") if logs.is_dir() else []):
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < start:
            continue
        k = str(path)
        hit = cache.get(k)
        days = hit[2] if hit and hit[0] == st.st_mtime and hit[1] == st.st_size else _claude_file(path)
        live[k] = [st.st_mtime, st.st_size, days]
        for day, s in days.items():
            for span, ok in (("today", day == today), ("month", day.startswith(month))):
                if ok:
                    for f in s:
                        tot[span][f] += s[f]
    cache.clear()
    cache.update(live)
    return tot


def snapshot(refresh=False, state=None, run=subprocess.run, logs=None):
    """{"at", "copilot", "claude"}; from usage.json when it's under 15 minutes old."""
    state = Path(state or STATE)
    path = state / "usage.json"
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = {}
    if not refresh and old.get("at") and time.time() - old.get("t", 0) < MAX_AGE:
        return {k: old[k] for k in ("at", "copilot", "claude") if k in old}
    files = old.get("files") if isinstance(old.get("files"), dict) else {}
    new = {"at": datetime.now().isoformat(timespec="seconds"), "t": time.time(),
           "copilot": copilot(run), "claude": claude(logs, files), "files": files}
    state.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(new))
    tmp.replace(path)
    return {k: new[k] for k in ("at", "copilot", "claude")}


def lines(u):
    """Plain-text lines for the CLI and fleet.py top."""
    out = []
    cp = u.get("copilot") or {}
    if cp.get("error"):
        out.append(f"copilot (GitHub's count): {cp['error']}")
    elif cp.get("unlimited"):
        out.append(f"copilot (GitHub's count): {cp.get('plan')} plan, unlimited")
    elif cp:
        out.append(f"copilot (GitHub's count): {cp['used']} of {cp['of']} premium credits used"
                   f" ({cp['left_pct']}% left), resets {cp['resets']}, {cp.get('plan')} plan"
                   + (f"; OVERAGE ON (up to {cp['overage_max']} more, billed)" if cp.get("overage_on") else ""))
    cl = u.get("claude") or {}
    if cl:
        out.append(f"claude (this machine's logs): today {cl['today']['turns']} replies, "
                   f"{cl['today']['tokens']:,} tokens; this month {cl['month']['turns']} replies, "
                   f"{cl['month']['tokens']:,} tokens (+{cl['month']['cache_read']:,} cache reads)")
    out.append("gemini: Google doesn't report the remaining quota without a request; see the fleet's calls above")
    return out


if __name__ == "__main__":
    import sys
    print("\n".join(lines(snapshot(refresh="--refresh" in sys.argv))))
