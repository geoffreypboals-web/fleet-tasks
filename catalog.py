"""catalog.py -- which model fits which work (board WP-152 step 3e).

catalog.json holds facts about each model (where it runs, weight, context).
How good a model is for a kind of task comes from public reviews: a
research task proposes scores (each with its source and date), and only
scores the owner accepted count. Our own results are blended in once a
model has done 20 checked tasks of a kind (70% reviews / 30% ours, 50/50
from 100).

The choosers here are pure: fleet.py passes in what they need.

  pick_local(...)   which model an endpoint runs a task with
  order_paid(...)   paid providers, best fit first
  pick_paid(...)    which of a provider's models answers

Standard library only.
"""
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIERS = ["tiny", "small", "medium", "large", "frontier"]
BLEND_FROM, EVEN_FROM = 20, 100     # our results count from 20 checked tasks (30%), 50% from 100
_lock = threading.Lock()


def load(path=None):
    p = Path(path or os.environ.get("FLEET_CATALOG", HERE / "catalog.json"))
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"models": [], "kinds": []}


def model(cat, name):
    return next((m for m in cat["models"] if m["name"] == name), None)


# ---------------- scores: proposed, accepted, rejected ----------------

def _scores_file(state):
    return Path(state) / "model-scores.json"


def scores(state):
    try:
        return json.loads(_scores_file(state).read_text())
    except (OSError, ValueError):
        return {"next_id": 1, "items": []}


def _save(state, data):
    Path(state).mkdir(parents=True, exist_ok=True)
    tmp = _scores_file(state).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(_scores_file(state))


def propose(state, items, task=0):
    """Add proposed scores; returns their ids. Nothing counts until accepted."""
    with _lock:
        data = scores(state)
        ids = []
        for it in items:
            data["items"].append({"id": data["next_id"], "model": it["model"], "kind": it["kind"],
                                  "score": float(it["score"]), "source": it["source"], "date": it.get("date", ""),
                                  "note": it.get("note", "")[:300], "status": "proposed", "task": task,
                                  "at": datetime.now().isoformat(timespec="seconds"), "by": ""})
            ids.append(data["next_id"])
            data["next_id"] += 1
        _save(state, data)
    return ids


def decide(state, ids, accept, by=""):
    """The owner accepts or rejects proposed scores. Accepting one retires the
    earlier accepted score for the same model and kind. Returns how many changed."""
    with _lock:
        data = scores(state)
        n = 0
        for it in data["items"]:
            if it["id"] in ids and it["status"] == "proposed":
                if accept:
                    for old in data["items"]:
                        if old["status"] == "accepted" and old["model"] == it["model"] and old["kind"] == it["kind"]:
                            old["status"] = "replaced"
                it["status"] = "accepted" if accept else "rejected"
                it["by"], it["decided"] = by, datetime.now().isoformat(timespec="seconds")
                n += 1
        _save(state, data)
    return n


def accepted(data):
    """{(model, kind): item} for accepted scores."""
    return {(it["model"], it["kind"]): it for it in data["items"] if it["status"] == "accepted"}


# ---------------- our own results ----------------

def record(state, kind, model_name, passed):
    """One checked task: did the answer pass (and stay at its tier)?"""
    Path(state).mkdir(parents=True, exist_ok=True)
    with open(Path(state) / "model-results.jsonl", "a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
                            "model": model_name, "passed": bool(passed)}) + "\n")


def stats(state):
    """{(model, kind): (checked tasks, pass rate)}."""
    out = {}
    try:
        lines = (Path(state) / "model-results.jsonl").read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        k = (e.get("model"), e.get("kind"))
        if e.get("reset"):                 # the owner's fresh start for a paid AI (fleet.paid_reset)
            out.pop(k, None)
            continue
        n, p = out.get(k, (0, 0))
        out[k] = (n + 1, p + bool(e.get("passed")))
    return {k: (n, p / n) for k, (n, p) in out.items()}


def fit(model_name, kind, acc, st):
    """(score 0-10, why) blending the accepted review score with our pass rate,
    or (None, '') when there's no accepted review score."""
    it = acc.get((model_name, kind))
    if not it:
        return None, ""
    review = it["score"]
    n, rate = st.get((model_name, kind), (0, 0.0))
    if n >= EVEN_FROM:
        score, mix = 0.5 * review + 0.5 * 10 * rate, f"; ours {rate:.0%} of {n}, 50/50"
    elif n >= BLEND_FROM:
        score, mix = 0.7 * review + 0.3 * 10 * rate, f"; ours {rate:.0%} of {n}, 70/30"
    else:
        score, mix = review, ""
    src = it["source"].split("//")[-1].split("/")[0] if "//" in it["source"] else it["source"]
    return round(score, 1), f"fit {score:.1f} for {kind} ({src}{', ' + it['date'] if it.get('date') else ''}{mix})"


# ---------------- choosing ----------------

def pick_local(cat, ep, kind, task_tier, text_len, acc, st, idle):
    """(model, why) for a task on one endpoint. Only models on this endpoint (not
    above its tier) with room for the prompt. A model below the task's tier
    counts only when its accepted score beats every scored model at the
    task's tier (evidence that the smaller one is better, like a coder model
    for code), never just because the bigger ones have no score. The
    endpoint's own model stays unless the endpoint is idle and another
    model's fit is at least switch_margin higher: never evict."""
    own = ep["model"]
    if not cat.get("models"):
        return own, ""
    lo, hi = TIERS.index(task_tier) if task_tier in TIERS else 0, TIERS.index(ep.get("tier", "large"))
    need = text_len // 3 + 2048
    cands = [m for m in cat["models"] if m.get("cost") == "local" and ep["name"] in m.get("endpoints", [])
             and TIERS.index(m.get("tier", "large")) <= hi and m.get("context", 0) >= need]
    own_fit, own_why = fit(own, kind, acc, st)
    at_tier = [f for f in (fit(m["name"], kind, acc, st)[0] for m in cands
                           if TIERS.index(m.get("tier", "large")) >= lo) if f is not None]
    if own_fit is not None:
        at_tier.append(own_fit)
    best, best_fit, best_why = own, own_fit, own_why
    for m in cands:
        f, why = fit(m["name"], kind, acc, st)
        if f is None or (best_fit is not None and f <= best_fit):
            continue
        if TIERS.index(m.get("tier", "large")) < lo and not (at_tier and f > max(at_tier)):
            continue                                  # smaller than the task asks for, without evidence it's better
        best, best_fit, best_why = m["name"], f, why
    if best == own:
        return own, (f"{own_why}, loaded" if own_why else "")
    if not idle:
        return own, f"{best} fits better ({best_fit}) but another model is loaded; never evicts"
    if own_fit is not None and best_fit < own_fit + cat.get("switch_margin", 2):
        return own, f"{own_why}; {best} is only {best_fit - own_fit:.1f} better (switch at {cat.get('switch_margin', 2)})"
    return best, f"{best_why}, endpoint idle"


def best_paid_fit(cat, provider, kind, acc, st):
    fs = [fit(m["name"], kind, acc, st)[0] for m in cat.get("models", []) if m.get("provider") == provider]
    fs = [f for f in fs if f is not None]
    return max(fs) if fs else None


def order_paid(cat, names, kind, acc, st):
    """Main (non-last-resort) providers, best fit for the kind first. Providers
    within tie_margin keep the given (policy) order; unscored ones go after
    scored ones, in policy order. With no scores at all, the order is unchanged."""
    fits = {n: best_paid_fit(cat, n, kind, acc, st) for n in names}
    if all(f is None for f in fits.values()):
        return list(names)
    tie = cat.get("tie_margin", 1)
    out = []
    for n in names:                                   # insertion keeps policy order inside a tie
        i = len(out)
        while i > 0 and fits[n] is not None and (fits[out[i - 1]] is None or fits[n] > fits[out[i - 1]] + tie):
            i -= 1
        out.insert(i, n)
    return out


def pick_paid(cat, provider, kind, room, acc, st):
    """(model entry or None, why). Best fit among the provider's models whose
    weight fits today's room; weight 3+ only for big_kinds. No scores: the
    provider's default model. False when nothing fits the room."""
    ms = [m for m in cat.get("models", []) if m.get("provider") == provider]
    if not ms:
        return None, ""
    big_ok = kind in cat.get("big_kinds", [])
    ok = [m for m in ms if m.get("weight", 1) <= room and (big_ok or m.get("weight", 1) < 3)]
    if not ok:
        return False, f"{provider}'s models need more than the {room} call(s) left today"
    scored = [(fit(m["name"], kind, acc, st), m) for m in ok]
    scored = [(f, m) for f, m in scored if f[0] is not None]
    if scored:
        (f, why), m = max(scored, key=lambda x: (x[0][0], -x[1].get("weight", 1)))
        return m, why
    default = next((m for m in ok if m.get("default")), None)
    return default, ("default model" if default else "")


# ---------------- proposals from a research task's answer ----------------

def parse_proposals(cat, text):
    """Score proposals from a research answer: a JSON list of
    {model, kind, score, source, date}. Rows naming a model or kind the
    catalogue doesn't have, a score outside 0-10, or no source are dropped
    (returned as problems)."""
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if not m:
        return [], ["no JSON list of scores found in the answer"]
    try:
        rows = json.loads(m.group(0))
    except ValueError as e:
        return [], [f"the list isn't valid JSON ({e})"]
    names = {x["name"] for x in cat.get("models", [])}
    kinds = set(cat.get("kinds", []))
    good, bad = [], []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        why = (f"unknown model {r.get('model')!r}" if r.get("model") not in names else
               f"unknown kind {r.get('kind')!r}" if r.get("kind") not in kinds else
               "no source" if not str(r.get("source", "")).strip() else
               "the source isn't a web address" if not re.match(r"https?://[^\s<>\"']+$", str(r["source"]).strip())
               else None)
        try:
            s = float(r.get("score"))
            if not 0 <= s <= 10:
                why = why or f"score {s} is outside 0-10"
        except (TypeError, ValueError):
            why = why or f"score {r.get('score')!r} isn't a number"
        if why:
            bad.append(why)
        else:
            good.append({"model": r["model"], "kind": r["kind"], "score": s, "source": str(r["source"]).strip(),
                         "date": str(r.get("date", "")).strip(), "note": str(r.get("note", "")).strip()})
    return good, bad
