"""AI work board: claim, checkpoint, complete and verify work packages so
every AI that works on the fleet (two Claude accounts, Copilot, Gemini, the
local models on gpu-server, cloud sessions) checks work out before starting,
never duplicates another agent's work, and routes each package to the
cheapest kind of AI that can do it. Rules: docs/multi-ai/MULTI-AI-WORK.md.

The board is one JSON file, `board.json`, on branch `ai-board` of this
repo's origin (my-board-repo). Every change runs the same cycle:

    fetch -> reset to origin/ai-board -> validate + apply -> commit -> push

A push that GitHub rejects means another agent changed the board first, so
the cycle starts again from the fresh board and re-validates. Whoever's push
lands first owns the claim; the other agent gets a clear refusal. Git is the
lock, so no server is needed and it works across machines.

    python scripts/ai_board.py whoami
    python scripts/ai_board.py list [--available]
    python scripts/ai_board.py next                  # suggest a package for my lane
    python scripts/ai_board.py show WP-09
    python scripts/ai_board.py claim WP-09 [--branch ai/wp-09-grammar] [--takeover]
    python scripts/ai_board.py checkpoint WP-09 "client + tests done" --next "wire into audit"
    python scripts/ai_board.py release WP-09 "out of budget before starting"
    python scripts/ai_board.py done WP-09 --commit abc1234 --tests "pytest: 1051 passed" \
        [--pr URL] [--repo-path D:/repos/_wt/WP-09/my-app]
    python scripts/ai_board.py verify WP-09 --ok "re-ran tests, reviewed diff"
    python scripts/ai_board.py verify WP-09 --reject "grammar flags missing on Read tab"
    python scripts/ai_board.py backlog               # open work in the owner's order, with best-fit AI
    python scripts/ai_board.py route WP-09 --to local   # owner: hand a package to an AI (see ROUTES)
    python scripts/ai_board.py init SEED.json        # one time: create the ai-board branch

Environment:
    AI_BOARD_AGENT  who you are, "<account>@<machine>", e.g. claude-A@winD,
                    copilot@winD, local-vibe@gpu-server, human-owner@winD (required;
                    the account must be listed in the board's agent_tiers)
    AI_BOARD_DIR    where the board clone lives (default: <this repo's parent>/_board)
    AI_BOARD_REMOTE git URL of the board repo (default: this repo's origin)

Stdlib only, so it runs on Windows, gpu-server and cloud sessions unchanged.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRANCH = "ai-board"
BOARD_FILE = "board.json"
LOG_FILE = "LOG.md"
VIEW_FILE = "BOARD.md"
DEFAULT_LEASE_HOURS = 6
PUSH_ATTEMPTS = 6
ACTIVE = ("claimed",)
FINISHED = ("done", "verified")
# Cost tiers, cheapest first. A package's "tier" is the cheapest kind of
# agent allowed to build it; an agent may take work at or below its tier.
TIER_RANK = {"local": 1, "paid-mid": 2, "paid-high": 3}
DEFAULT_TIER = "paid-mid"


class BoardError(Exception):
    """A refused board operation: the message says why and what to do."""


# --------------------------------------------------------------------------
# Pure board logic (no git): every command is apply_<cmd>(board, ...) that
# mutates a copy and returns (board, log_line). Tests exercise these directly.
# --------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def account_of(agent: str) -> str:
    return agent.split("@", 1)[0]


def validate_agent(agent: str | None) -> str:
    if not agent or not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+", agent):
        raise BoardError("Set AI_BOARD_AGENT to <account>@<machine>, e.g. my-account@winD.")
    return agent


def canonical_account(board: dict, agent: str) -> str:
    """The account behind an agent id. The board's `agent_aliases` map old
    names onto the owner's real account names (e.g. claude-A -> my-account), so
    claims made under an old name still count as the same person for tier,
    lane and the "verify must be a different account" rule."""
    acct = account_of(agent)
    return board.get("agent_aliases", {}).get(acct, acct)


def is_human(agent: str) -> bool:
    return account_of(agent).startswith("human")


def agent_tier(board: dict, agent: str) -> str:
    """The agent's cost tier from the board's agent_tiers map: an exact
    account match, else the longest key the account starts with followed by
    '-' (so "local" covers local-vibe and local-openwebui)."""
    if is_human(agent):
        return "human"
    acct = canonical_account(board, agent)
    tiers = board.get("agent_tiers", {})
    for key in sorted(tiers, key=len, reverse=True):
        if acct == key or acct.startswith(key + "-"):
            return tiers[key]
    raise BoardError(f"Account {acct} isn't in the board's agent_tiers ({', '.join(sorted(tiers))}). "
                     "Add it as local, paid-mid or paid-high before it takes work.")


def package_tier(wp: dict) -> str:
    return wp.get("tier", DEFAULT_TIER)


def find(board: dict, wp_id: str) -> dict:
    for wp in board["packages"]:
        if wp["id"].upper() == wp_id.upper():
            return wp
    raise BoardError(f"No package {wp_id}. Run `list` to see ids.")


def lease_hours(board: dict) -> float:
    return float(board.get("lease_hours", DEFAULT_LEASE_HOURS))


def is_stale(board: dict, wp: dict, now: datetime) -> bool:
    claim = wp.get("claim")
    if wp["status"] not in ACTIVE or not claim:
        return False
    return now - parse_iso(claim["heartbeat_at"]) > timedelta(hours=lease_hours(board))


def _paths_overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def ownership_conflicts(board: dict, wp: dict, now: datetime) -> list[str]:
    """Other live claims that own a path this package also owns."""
    clashes = []
    for other in board["packages"]:
        if other is wp or other["status"] not in ACTIVE or is_stale(board, other, now):
            continue
        for mine in wp.get("owns", []):
            for theirs in other.get("owns", []):
                if _paths_overlap(mine, theirs):
                    clashes.append(f"{other['id']} owns {theirs} (held by {other['claim']['agent']})")
    return clashes


def unmet_deps(board: dict, wp: dict) -> list[str]:
    return [d for d in wp.get("depends_on", []) if find(board, d)["status"] not in FINISHED]


def claimable(board: dict, wp: dict, agent: str, now: datetime) -> tuple[bool, str]:
    if wp.get("lane") == "human" and not is_human(agent):
        return False, "human task (needs the project owner)"
    if not is_human(agent):
        mine, needed = agent_tier(board, agent), package_tier(wp)
        if TIER_RANK[mine] < TIER_RANK[needed]:
            return False, f"needs a {needed} agent (you are {mine})"
    if wp["status"] == "blocked":
        return False, f"blocked: {wp.get('blocked_reason', 'see notes')}"
    if wp["status"] in FINISHED:
        return False, f"already {wp['status']}"
    if wp["status"] in ACTIVE and not is_stale(board, wp, now):
        return False, f"claimed by {wp['claim']['agent']}"
    deps = unmet_deps(board, wp)
    if deps:
        return False, "waiting on " + ", ".join(deps)
    clashes = ownership_conflicts(board, wp, now)
    if clashes:
        return False, "file clash: " + "; ".join(clashes)
    return True, "stale claim, needs --takeover" if wp["status"] in ACTIVE else "available"


def _require_holder(wp: dict, agent: str) -> None:
    if wp["status"] not in ACTIVE or not wp.get("claim"):
        raise BoardError(f"{wp['id']} is {wp['status']}, not claimed.")
    if wp["claim"]["agent"] != agent:
        raise BoardError(f"{wp['id']} is held by {wp['claim']['agent']}, not you ({agent}).")


def _history(wp: dict, now: datetime, agent: str, event: str, note: str = "") -> None:
    wp.setdefault("history", []).append({"at": iso(now), "agent": agent, "event": event, "note": note})


def apply_claim(board, wp_id, agent, now, branch=None, takeover=False):
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    ok, why = claimable(board, wp, agent, now)
    if not ok:
        raise BoardError(f"Can't claim {wp['id']}: {why}.")
    previous = wp.get("claim") if wp["status"] in ACTIVE else None
    if previous and not takeover:
        raise BoardError(
            f"{wp['id']} has a stale claim by {previous['agent']} (no checkpoint for over "
            f"{lease_hours(board):g} h). Re-run with --takeover to continue their branch "
            f"{previous['branch']}.")
    branch = branch or (previous or {}).get("branch") or f"ai/{wp['id'].lower()}"
    wp["status"] = "claimed"
    wp["claim"] = {"agent": agent, "branch": branch, "claimed_at": iso(now), "heartbeat_at": iso(now)}
    event = f"takeover from {previous['agent']}" if previous else "claim"
    _history(wp, now, agent, event, f"branch {branch}")
    return board, f"{event} {wp['id']} (branch {branch})"


def apply_checkpoint(board, wp_id, agent, now, note, next_step=None):
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    _require_holder(wp, agent)
    if not note.strip():
        raise BoardError("A checkpoint needs a note saying what is done.")
    wp["claim"]["heartbeat_at"] = iso(now)
    wp["last_checkpoint"] = note.strip()
    if next_step:
        wp["next_step"] = next_step.strip()
    _history(wp, now, agent, "checkpoint", note.strip())
    return board, f"checkpoint {wp['id']}: {note.strip()}"


def apply_release(board, wp_id, agent, now, reason):
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    _require_holder(wp, agent)
    if not reason.strip():
        raise BoardError("Say why you're releasing it, so the next agent knows the state.")
    wp["status"] = "open"
    wp["released_branch"] = wp["claim"]["branch"]
    wp["claim"] = None
    _history(wp, now, agent, "release", reason.strip())
    return board, f"release {wp['id']}: {reason.strip()}"


def apply_done(board, wp_id, agent, now, commit, tests, pr=None, repo_checked=False):
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    _require_holder(wp, agent)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit or ""):
        raise BoardError("--commit must be the SHA of the pushed commit that completes the package.")
    if not (tests or "").strip():
        raise BoardError("--tests must say which checks ran and their result (e.g. 'pytest: 1051 passed').")
    wp["status"] = "done"
    wp["done"] = {"agent": agent, "at": iso(now), "commit": commit, "branch": wp["claim"]["branch"],
                  "pr": pr, "tests": tests.strip(), "commit_verified_on_branch": repo_checked}
    _history(wp, now, agent, "done", f"{commit} {pr or ''}".strip())
    return board, f"done {wp['id']} at {commit}"


OWNER_ANSWERS = {"done": "done it", "approved": "approved", "denied": "denied"}


def apply_owner_answer(board, wp_id, agent, now, answer, comment=""):
    """The owner closes one of their own tasks (lane human): "done" (I did it),
    "approved" or "denied", with an optional comment. The task becomes verified
    (the owner is the check), and the answer and comment are kept on it and in its
    history so the AI that waits on it can read them. Returns (board, log line)."""
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    if not is_human(agent):
        raise BoardError("Only the owner answers an owner task.")
    if wp.get("lane") != "human":
        raise BoardError(f"{wp['id']} isn't an owner task; an AI finishes it with `done`.")
    if answer not in OWNER_ANSWERS:
        raise BoardError(f"Answer one of: {', '.join(OWNER_ANSWERS)}")
    if wp["status"] in FINISHED:
        raise BoardError(f"{wp['id']} is already {wp['status']}.")
    comment = (comment or "").strip()[:2000]
    if answer == "denied" and not comment:
        raise BoardError("Say why it's denied, so the AI knows what to change.")
    wp["status"] = "verified"
    wp["claim"] = None
    wp["owner_answer"] = {"answer": answer, "comment": comment, "agent": agent, "at": iso(now)}
    # Finished items carry a "done" record (the status table reads it); an owner task has no commit.
    wp["done"] = {"agent": agent, "at": iso(now), "commit": "", "branch": "", "pr": None,
                  "tests": f"owner task: {OWNER_ANSWERS[answer]}", "commit_verified_on_branch": False, "by_owner": True}
    wp["verified"] = {"agent": agent, "at": iso(now), "notes": f"owner: {OWNER_ANSWERS[answer]}" + (f": {comment}" if comment else "")}
    _history(wp, now, agent, f"owner {answer}", comment)
    return board, f"owner {OWNER_ANSWERS[answer]} {wp['id']}"


def apply_verify(board, wp_id, agent, now, ok, notes):
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    if wp["status"] != "done":
        raise BoardError(f"{wp['id']} is {wp['status']}; only a done package can be verified.")
    if canonical_account(board, agent) == canonical_account(board, wp["done"]["agent"]):
        raise BoardError("Verification has to come from the other account, not the one that built it.")
    if not is_human(agent) and agent_tier(board, agent) == "local":
        raise BoardError("Local models can't verify; a paid agent or the owner has to check this.")
    if not (notes or "").strip():
        raise BoardError("Say what you checked (tests re-run, diff reviewed, acceptance items).")
    if ok:
        wp["status"] = "verified"
        wp["verified"] = {"agent": agent, "at": iso(now), "notes": notes.strip()}
        _history(wp, now, agent, "verified", notes.strip())
        return board, f"verified {wp['id']}"
    # Rejected: hand it straight back to its builder with a fresh lease.
    builder = wp["done"]["agent"]
    wp["status"] = "claimed"
    wp["claim"] = {"agent": builder, "branch": wp["done"]["branch"],
                   "claimed_at": iso(now), "heartbeat_at": iso(now)}
    wp["next_step"] = f"Fix verification findings: {notes.strip()}"
    _history(wp, now, agent, "rejected", notes.strip())
    return board, f"rejected {wp['id']}: {notes.strip()}"


# Owner rule 2026-10-31 (D-2026-10-31-04): unless the owner names an item,
# work the board by PRODUCT, then by AGE (oldest add date first). The order
# of products is the owner's: set from the "Backlog product priority" page
# with `ai_board.py set-products`, stored in board["product_order"]. Without
# it, this default applies. A package spanning several products ranks at its
# best one; a repo not listed (or "all") counts as "other".
DEFAULT_PRODUCTS = [
    {"key": "story-writer", "label": "Story Writer", "rank": 1, "repos": ["my-app"]},
    {"key": "shopify", "label": "Shopify / commerce", "rank": 2,
     "repos": ["Shopify-App", "Shopify-Custom-Manager", "shopify-marketplace-sync", "ProductOps", "SocialOps"]},
    {"key": "3d-print", "label": "3D printing", "rank": 3,
     "repos": ["3dPrinterWorkshopManager", "3d-printing-model-prompt", "CR10-Firmware-Vault",
               "Toolchanger-Research", "3DPrinterStructureReview", "Duender-CR10s", "print-farm-manager"]},
    {"key": "finance", "label": "Finance", "rank": 4,
     "repos": ["FinanceOps", "FamilyFinanceOps", "finance-core", "StockOps",
               "Kids-Fiancial-Operations-MintQuest-AI"]},
    {"key": "automation", "label": "Automation", "rank": 5,
     "repos": ["my-board-repo"]},
    {"key": "other", "label": "Everything else", "rank": 6, "repos": []},
]
WITHIN_ORDERS = ("oldest", "newest", "priority")
REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
PRODUCT_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def product_order(board) -> dict:
    """The owner's product order: {"products": [...], "within": "oldest"}."""
    order = board.get("product_order") or {}
    products = order.get("products") or DEFAULT_PRODUCTS
    # A product added to DEFAULT_PRODUCTS after the owner saved an order
    # joins it at its default rank, without taking repos the owner placed.
    have = {p["key"] for p in products}
    placed = {r.lower() for p in products for r in p.get("repos", [])}
    new = [dict(p, repos=[r for r in p["repos"] if r.lower() not in placed])
           for p in DEFAULT_PRODUCTS if p["key"] not in have]
    if new:
        rest = [p for p in products if p["key"] != "other"]
        other = [p for p in products if p["key"] == "other"]
        # Everything else stays after the new product(s).
        top = max(int(p.get("rank", 9)) for p in new)
        other = [dict(p, rank=min(9, max(int(p.get("rank", 9)), top + 1))) for p in other]
        products = rest + new + other
    within = order.get("within") if order.get("within") in WITHIN_ORDERS else "oldest"
    return {"products": products, "within": within}


def product_of(board, wp) -> dict:
    """The highest-ranked product any of the package's repos belongs to."""
    products = product_order(board)["products"]
    by_repo = {r.lower(): p for p in products for r in p.get("repos", [])}
    other = next((p for p in products if p["key"] == "other"), {"key": "other", "label": "Everything else", "rank": 99})
    hits = [by_repo[r.lower()] for r in wp.get("repos", []) if r.lower() in by_repo]
    return min(hits, key=lambda p: int(p.get("rank", 99))) if hits else other


def _first_stamp(wp) -> str:
    """The package's earliest history timestamp, or "" when it has none."""
    stamps = [h.get("at", "") for h in wp.get("history", []) if h.get("at")]
    return min(stamps) if stamps else ""


def added_at(board, wp) -> str:
    """When the package was added (ISO, sorts as text): its earliest history
    timestamp. A package with no history (seeded by hand) takes the date of
    the nearest package before it on the board, which was added around the
    same time; "9999" only when nothing before it has a date."""
    own = _first_stamp(wp)
    if own:
        return own
    packages = board["packages"]
    i = next((n for n, p in enumerate(packages) if p is wp), len(packages))
    for prev in reversed(packages[:i]):
        stamp = _first_stamp(prev)
        if stamp:
            return stamp
    return "9999"


def work_order_key(board, wp) -> tuple:
    """Sort key for the owner's rule: product rank, then age (or the chosen
    order within a product). Ties fall through to the caller's own keys."""
    within = product_order(board)["within"]
    age = added_at(board, wp)
    if within == "newest":
        age_key = tuple(-ord(c) for c in age)
    elif within == "priority":
        age_key = (int(wp.get("priority", 9)), age)
    else:
        age_key = (age,)
    return (int(product_of(board, wp).get("rank", 99)), age_key)


def apply_set_products(board, agent, now, spec: dict) -> tuple[dict, str]:
    """Store the owner's product order (from the priority page's JSON).

    spec: {"ranks": {"story-writer": 1, ...}, "within": "oldest",
    "repos": {"my-board-repo": "story-writer", ...},
    "labels": {"home-lab": "Home lab"}}. "labels" names a product; a key the
    board doesn't know yet is added as a new product. "repos" moves
    each named repo into that product (out of any other; "other" means
    Everything else); any repo name works, new ones included. Unknown
    product keys, bad repo names and ranks outside 1-9 are refused. Only the owner's own sessions
    should run this (it's recorded in the decision log by the caller).
    Returns (board, log line), like the other apply_* commands."""
    ranks = spec.get("ranks") or {}
    base = [dict(p) for p in product_order(board)["products"]]
    labels = spec.get("labels") or {}
    if not isinstance(labels, dict):
        raise BoardError("'labels' must map product key to its name")
    for key, label in labels.items():
        label = str(label).strip()
        if not PRODUCT_KEY.fullmatch(str(key)):
            raise BoardError(f"Bad product key {key!r}: lowercase letters, digits and '-' only")
        if not 1 <= len(label) <= 40:
            raise BoardError(f"Name for {key} must be 1-40 characters")
        known = next((p for p in base if p["key"] == key), None)
        if known:
            known["label"] = label
        else:  # a new product, ranked just before Everything else until given a rank
            base.insert(len(base) - 1 if base and base[-1]["key"] == "other" else len(base),
                        {"key": key, "label": label, "rank": int(ranks.get(key, 5)), "repos": []})
    keys = {p["key"] for p in base}
    bad = [k for k in ranks if k not in keys]
    if bad:
        raise BoardError(f"Unknown product(s) {', '.join(bad)}; known: {', '.join(sorted(keys))}")
    for p in base:
        if p["key"] in ranks:
            r = int(ranks[p["key"]])
            if not 1 <= r <= 9:
                raise BoardError(f"Rank for {p['key']} must be 1-9, got {r}")
            p["rank"] = r
    repos = spec.get("repos") or {}
    if not isinstance(repos, dict):
        raise BoardError("'repos' must map repo name to product key")
    for repo, key in repos.items():
        if not REPO_NAME.fullmatch(str(repo)):
            raise BoardError(f"Bad repo name {repo!r}: letters, digits, '.', '_' and '-' only")
        if key not in keys:
            raise BoardError(f"Unknown product {key!r} for {repo}; known: {', '.join(sorted(keys))}")
        for p in base:
            p["repos"] = [r for r in p.get("repos", []) if r.lower() != str(repo).lower()]
        target = next(p for p in base if p["key"] == key)
        target["repos"] = target["repos"] + [str(repo)]
    within = spec.get("within", "oldest")
    if within not in WITHIN_ORDERS:
        raise BoardError(f"'within' must be one of {', '.join(WITHIN_ORDERS)}")
    board["product_order"] = {"products": base, "within": within, "set_by": agent, "set_at": iso(now)}
    shown = ", ".join(f"{p['rank']}. {p['label']}" for p in sorted(base, key=lambda p: int(p["rank"])))
    moved = "".join(f"; {r} -> {k}" for r, k in repos.items())
    return board, f"set-products: {shown}; within a product: {within}{moved}"


def suggest_next(board, agent, now) -> dict | None:
    """The next available package for me.

    Ranking (owner rules 2026-10-31): packages the owner routed to me
    (`route`) first, then the owner's product order
    (product_order / DEFAULT_PRODUCTS), then age -- oldest add date first --
    then work at my own tier, then my lane. Local-tier work
    is never suggested to a paid agent: it waits for a local model or a
    script run, which costs no paid tokens. A paid agent can still claim it
    explicitly when no local model is available. The owner ("human-..." accounts) is only
    pointed at human tasks; anyone can still claim a package explicitly."""
    if is_human(agent):
        cands = [wp for wp in board["packages"]
                 if wp.get("lane") == "human" and claimable(board, wp, agent, now)[0]]
        return cands[0] if cands else None
    mine = TIER_RANK[agent_tier(board, agent)]
    my_lane = board.get("lane_accounts", {}).get(canonical_account(board, agent))
    ranked = []
    for index, wp in enumerate(board["packages"]):
        if wp.get("lane") == "human" or not claimable(board, wp, agent, now)[0]:
            continue
        if package_tier(wp) == "local" and mine > TIER_RANK["local"]:
            continue
        tier_gap = mine - TIER_RANK[package_tier(wp)]
        ranked.append((0 if routed_to_me(board, wp, agent) else 1, work_order_key(board, wp), tier_gap,
                       int(wp.get("priority", 9)), 0 if wp.get("lane") == my_lane else 1, index, wp))
    return min(ranked, key=lambda r: r[:6])[6] if ranked else None


# Who a package can be routed to (owner decision D-2026-10-31-05). "lane"
# and "tier" are what routing sets on the package so that AI's `next` and
# `claimable` agree; None leaves the package's own value.
ROUTES = {
    "claude-A": {"label": "Claude (account A)", "lane": "A", "tier": None},
    "claude-B": {"label": "Claude (account B)", "lane": "B", "tier": None},
    "claude": {"label": "Claude (either account)", "lane": "any", "tier": None},
    "copilot": {"label": "Copilot", "lane": "any", "tier": "paid-mid"},
    "gemini": {"label": "Gemini", "lane": "any", "tier": "paid-mid"},
    "local": {"label": "Local model (Aider on gpu-server)", "lane": "local", "tier": "local"},
    "human": {"label": "You (owner task)", "lane": "human", "tier": None},
}


def routed_to_me(board: dict, wp: dict, agent: str) -> bool:
    """True when the package's route names this agent: its account, its
    Claude lane (claude-A / claude-B), any Claude (claude) or its tool."""
    to = (wp.get("route") or {}).get("to")
    if not to:
        return False
    account = canonical_account(board, agent)
    lane = board.get("lane_accounts", {}).get(account)
    if to == "claude":
        return lane in ("A", "B")
    if to in ("claude-A", "claude-B"):
        return lane == to[-1]
    return account == to or account_of(agent) == to or account.startswith(to)


def best_fit(board: dict, wp: dict) -> dict:
    """Who should do this package, and why: {"to", "label", "why", "auto_ok"}.

    An owner route wins. Otherwise the package's lane and tier decide: human
    lane -> owner; local tier -> a local model; paid-high -> Claude on its
    lane; paid-mid -> Copilot (Gemini is off until signed in, WP-137).
    auto_ok: small enough for the fleet's autopilot to build by itself
    (est_sessions <= 1, not paid-high, not a human task)."""
    route = wp.get("route") or {}
    tier, lane = package_tier(wp), wp.get("lane", "any")
    small = float(wp.get("est_sessions", 1) or 0) <= 1
    if route.get("to") in ROUTES:
        to, why = route["to"], f"routed by {route.get('by', 'the owner')}"
    elif lane == "human":
        to, why = "human", "needs you (owner task)"
    elif tier == "local":
        to, why = "local", "local tier: mechanical work a local model does for free"
    elif tier == "paid-high":
        to = "claude-A" if lane == "A" else "claude-B" if lane == "B" else "claude"
        why = "paid-high: design, security or cross-repo work"
    else:
        to, why = "copilot", "paid-mid: a clear single-module change"
        if lane in ("A", "B"):
            to, why = f"claude-{lane}", f"paid-mid on Claude's lane {lane}"
    auto_ok = to != "human" and tier != "paid-high" and small and wp.get("status") in ("open", "todo")
    return {"to": to, "label": ROUTES[to]["label"], "why": why, "auto_ok": auto_ok}


def apply_route(board, wp_id, agent, now, to):
    """Route a package to an AI (ROUTES): sets its lane (and tier where the
    route needs one) and records who routed it. Returns (board, log line)."""
    if to not in ROUTES:
        raise BoardError(f"Route to one of: {', '.join(ROUTES)}")
    board = copy.deepcopy(board)
    wp = find(board, wp_id)
    if wp["status"] in FINISHED:
        raise BoardError(f"{wp['id']} is already {wp['status']}.")
    target = ROUTES[to]
    before = {"lane": wp.get("lane"), "tier": package_tier(wp)}
    wp["lane"] = target["lane"]
    if target["tier"]:
        wp["tier"] = target["tier"]
    elif package_tier(wp) == "local" and to != "human":
        wp["tier"] = "paid-mid"             # off the local tier, so a paid AI's `next` offers it
    wp["route"] = {"to": to, "by": agent, "at": iso(now), "from": before}
    _history(wp, now, agent, "route", f"to {to}")
    return board, f"route {wp['id']} -> {to}"


OWNER_WORD = re.compile(r"\bowner\b", re.IGNORECASE)


def _clip(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _pr_link(pr: str | None) -> str:
    return f" [PR]({pr})" if pr and pr.startswith("https://") else (f" {pr}" if pr else "")


def render_owner_view(board: dict, now: datetime) -> list[str]:
    """The owner's summary at the top of BOARD.md: what needs a person,
    grouped by the action it needs, then a one-line queue per agent lane."""
    live = [wp for wp in board["packages"] if wp["status"] not in FINISHED]
    decide, later = [], []
    for wp in live:
        if wp.get("lane") != "human":
            continue
        waits = unmet_deps(board, wp)
        if waits:
            later.append(f"- **{wp['id']}** {wp['title']} (after {', '.join(waits)})")
            continue
        todo = wp.get("next_step") or "; ".join(wp.get("steps") or [])
        stale = " Claimed but stale: finish it or say it's done." if is_stale(board, wp, now) else ""
        decide.append(f"- **{wp['id']}** {wp['title']}. {_clip(todo)}{stale}")
    on_owner = [f"- **{wp['id']}** {wp['title']}. {_clip(wp.get('next_step', ''))}"
                for wp in live if wp.get("lane") != "human" and wp["status"] in ("open",) + ACTIVE
                and OWNER_WORD.search(wp.get("next_step") or "")]
    verify = []
    for wp in board["packages"]:
        if wp["status"] == "done":
            d = wp["done"]
            verify.append(f"- **{wp['id']}** {wp['title']}: built by {d['agent']} at `{d['commit']}`"
                          f"{_pr_link(d.get('pr'))}. Verify from any account except "
                          f"{canonical_account(board, d['agent'])}.")
    blocked = [f"- **{wp['id']}** {wp['title']}: {_clip(wp.get('blocked_reason', 'see notes'))}"
               for wp in live if wp["status"] == "blocked"]
    queue: dict[str, list[str]] = {}
    for wp in live:
        if wp.get("lane") == "human" or wp["status"] not in ("open",) + ACTIVE:
            continue
        mark = wp["id"]
        if wp["status"] in ACTIVE:
            mark += f" ({account_of(wp['claim']['agent'])}{', stale' if is_stale(board, wp, now) else ''})"
        elif unmet_deps(board, wp):
            mark += f" (waits on {', '.join(unmet_deps(board, wp))})"
        queue.setdefault(wp.get("lane", "-"), []).append(mark)

    lines = ["## Owner view", "",
             f"Rebuilt on every board change; last change {iso(now)}.", "",
             "- **Decisions (H-...)** have no button. In a chat with either Claude account say "
             "`H-06 done` (or give the answer), or run `ai_board done` as `human-owner@<machine>`.",
             "- **Pull requests**: open the link on any computer signed in to GitHub and press Merge.",
             "- **Verify** has to come from a different account than the builder's.", ""]
    for heading, rows in ((f"### Your decisions ({len(decide)})", decide),
                          (f"### Agent work waiting on you ({len(on_owner)})", on_owner),
                          (f"### Waiting for a verifier ({len(verify)})", verify),
                          (f"### Blocked ({len(blocked)})", blocked),
                          (f"### Your decisions later, after other work ({len(later)})", later)):
        if rows:
            lines += [heading, "", *rows, ""]
    if queue:
        lines += ["### Agent queue (nothing for you to do)", ""]
        lines += [f"- Lane {lane}: {', '.join(ids)}" for lane, ids in sorted(queue.items())]
        lines.append("")
    return lines + ["## All packages", ""]


def render_view(board: dict, now: datetime) -> str:
    lines = [f"# {board.get('title', 'AI work board')}", "",
             "Generated by `scripts/ai_board.py` - don't edit by hand; `board.json` is the source.", "",
             *render_owner_view(board, now),
             f"Lease: a claim with no checkpoint for {lease_hours(board):g} h can be taken over.", "",
             "| Package | Lane | Tier | Status | Holder | Last checkpoint | Next step |",
             "|---|---|---|---|---|---|---|"]
    for wp in board["packages"]:
        claim = wp.get("claim") or {}
        status = wp["status"] + (" (stale)" if is_stale(board, wp, now) else "")
        holder = claim.get("agent", "")
        if wp["status"] in FINISHED:
            holder = f"built by {wp['done']['agent']}"
            if wp.get("verified"):
                holder += f", verified by {wp['verified']['agent']}"
        elif claim:
            age = now - parse_iso(claim["heartbeat_at"])
            holder += f" ({int(age.total_seconds() // 3600)}h {int(age.total_seconds() % 3600 // 60)}m ago)"
        cells = [f"**{wp['id']}** {wp['title']}", wp.get("lane", ""), package_tier(wp), status, holder,
                 wp.get("last_checkpoint", ""), wp.get("next_step", "")]
        lines.append("| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Git plumbing
# --------------------------------------------------------------------------

def git(args: list[str], cwd: Path | str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise BoardError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def default_remote() -> str:
    env = os.environ.get("AI_BOARD_REMOTE")
    if env:
        return env
    here = Path(__file__).resolve().parent
    return git(["remote", "get-url", "origin"], cwd=here).stdout.strip()


def default_board_dir() -> Path:
    env = os.environ.get("AI_BOARD_DIR")
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root.parent / "_board"


def ensure_clone(board_dir: Path, remote: str) -> None:
    if (board_dir / ".git").exists():
        return
    board_dir.parent.mkdir(parents=True, exist_ok=True)
    proc = git(["clone", "--branch", BRANCH, "--single-branch", remote, str(board_dir)], check=False)
    if proc.returncode != 0:
        raise BoardError(f"Couldn't clone the {BRANCH} branch from {remote}. Has `init` been run? "
                         f"git said: {proc.stderr.strip()}")


def sync(board_dir: Path) -> dict:
    git(["fetch", "--quiet", "origin", BRANCH], cwd=board_dir)
    git(["reset", "--quiet", "--hard", f"origin/{BRANCH}"], cwd=board_dir)
    return json.loads((board_dir / BOARD_FILE).read_text(encoding="utf-8"))


def write_board(board_dir: Path, board: dict, now: datetime, log_line: str, agent: str) -> None:
    (board_dir / BOARD_FILE).write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    (board_dir / VIEW_FILE).write_text(render_view(board, now), encoding="utf-8")
    with open(board_dir / LOG_FILE, "a", encoding="utf-8") as log:
        log.write(f"- {iso(now)} {agent} {log_line}\n")


def mutate(board_dir: Path, agent: str, change, _before_push=None) -> str:
    """Apply `change(board, now) -> (board, log_line)` atomically via push.

    `_before_push` is a test hook that runs between commit and push, to
    simulate another agent landing a change in that window."""
    last_error = ""
    for attempt in range(PUSH_ATTEMPTS):
        board = sync(board_dir)
        now = now_utc()
        board, log_line = change(board, now)  # BoardError here = refused, not retried
        write_board(board_dir, board, now, log_line, agent)
        git(["add", BOARD_FILE, VIEW_FILE, LOG_FILE], cwd=board_dir)
        git(["commit", "--quiet", "-m", f"board: {log_line} [{agent}]"], cwd=board_dir)
        if _before_push:
            _before_push(attempt)
        push = git(["push", "--quiet", "origin", f"HEAD:{BRANCH}"], cwd=board_dir, check=False)
        if push.returncode == 0:
            return log_line
        last_error = push.stderr.strip()
        time.sleep(min(2 ** attempt, 8) if not _before_push else 0)
    raise BoardError(f"Board push kept being rejected after {PUSH_ATTEMPTS} tries: {last_error}")


def commit_on_branch(repo_path: str, branch: str, commit: str) -> None:
    # Explicit refspec: in a --single-branch clone (cloud sessions) a bare
    # `git fetch origin <branch>` only writes FETCH_HEAD, never origin/<branch>.
    git(["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=repo_path)
    proc = git(["merge-base", "--is-ancestor", commit, f"origin/{branch}"], cwd=repo_path, check=False)
    if proc.returncode != 0:
        raise BoardError(f"Commit {commit} isn't on origin/{branch} in {repo_path}. Push it first.")


def init_board(board_dir: Path, remote: str, seed_path: str, agent: str) -> str:
    if git(["ls-remote", "--heads", remote, BRANCH], check=False).stdout.strip():
        raise BoardError(f"{BRANCH} already exists on {remote}; nothing to initialise.")
    seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    for wp in seed["packages"]:
        wp.setdefault("status", "open")
        wp.setdefault("claim", None)
    board_dir.mkdir(parents=True, exist_ok=True)
    git(["init", "--quiet"], cwd=board_dir)
    git(["checkout", "--quiet", "--orphan", BRANCH], cwd=board_dir)
    if not git(["remote"], cwd=board_dir).stdout.strip():
        git(["remote", "add", "origin", remote], cwd=board_dir)
    now = now_utc()
    (board_dir / LOG_FILE).write_text("# Board log (append-only)\n\n", encoding="utf-8")
    write_board(board_dir, seed, now, f"init from {Path(seed_path).name}", agent)
    git(["add", BOARD_FILE, VIEW_FILE, LOG_FILE], cwd=board_dir)
    git(["commit", "--quiet", "-m", f"board: init [{agent}]"], cwd=board_dir)
    git(["push", "--quiet", "-u", "origin", f"HEAD:{BRANCH}"], cwd=board_dir)
    return f"created {BRANCH} with {len(seed['packages'])} packages"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_package(board: dict, wp: dict, agent: str, now: datetime) -> None:
    ok, why = claimable(board, wp, agent, now)
    print(f"{wp['id']}  {wp['title']}")
    print(f"  priority {wp.get('priority', '-')} | lane {wp.get('lane')} | tier {package_tier(wp)} | "
          f"phase {wp.get('phase', '-')} | "
          f"est {wp.get('est_sessions', '?')} session(s) | "
          f"status {wp['status']} | for you: {why}")
    if wp.get("licence_flags"):
        print(f"  LICENCE FLAGS: {'; '.join(wp['licence_flags'])}")
    for key in ("repos", "owns", "shared", "depends_on"):
        if wp.get(key):
            print(f"  {key}: {', '.join(wp[key])}")
    if wp.get("claim"):
        c = wp["claim"]
        print(f"  claim: {c['agent']} on {c['branch']} since {c['claimed_at']}, last heartbeat {c['heartbeat_at']}")
    for key in ("last_checkpoint", "next_step"):
        if wp.get(key):
            print(f"  {key}: {wp[key]}")
    if wp.get("steps"):
        print("  steps:")
        for i, step in enumerate(wp["steps"], 1):
            print(f"    {i}. {step}")
    if wp.get("acceptance"):
        print("  acceptance:")
        for item in wp["acceptance"]:
            print(f"    - {item}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    ls = sub.add_parser("list"); ls.add_argument("--available", action="store_true")
    sub.add_parser("next")
    sh = sub.add_parser("show"); sh.add_argument("wp")
    cl = sub.add_parser("claim"); cl.add_argument("wp"); cl.add_argument("--branch"); cl.add_argument("--takeover", action="store_true")
    ck = sub.add_parser("checkpoint"); ck.add_argument("wp"); ck.add_argument("note"); ck.add_argument("--next", dest="next_step")
    rl = sub.add_parser("release"); rl.add_argument("wp"); rl.add_argument("reason")
    oa = sub.add_parser("owner-answer", help="the owner closes an owner task: done, approved or denied")
    oa.add_argument("wp"); oa.add_argument("answer", choices=list(OWNER_ANSWERS)); oa.add_argument("--comment", default="")
    dn = sub.add_parser("done"); dn.add_argument("wp"); dn.add_argument("--commit", required=True)
    dn.add_argument("--tests", required=True); dn.add_argument("--pr"); dn.add_argument("--repo-path")
    vf = sub.add_parser("verify"); vf.add_argument("wp")
    g = vf.add_mutually_exclusive_group(required=True); g.add_argument("--ok"); g.add_argument("--reject")
    it = sub.add_parser("init"); it.add_argument("seed")
    sub.add_parser("products")
    sp = sub.add_parser("set-products"); sp.add_argument("spec", help='JSON, e.g. {"ranks": {"finance": 2}, "within": "oldest"}')
    bp = sub.add_parser("backlog"); bp.add_argument("--limit", type=int, default=10)
    rt = sub.add_parser("route"); rt.add_argument("wp"); rt.add_argument("--to", required=True, choices=list(ROUTES))
    args = p.parse_args(argv)

    try:
        agent = validate_agent(os.environ.get("AI_BOARD_AGENT"))
        board_dir, remote = default_board_dir(), default_remote()
        if args.cmd == "init":
            print(init_board(board_dir, remote, args.seed, agent)); return 0
        ensure_clone(board_dir, remote)
        if args.cmd == "whoami":
            board = sync(board_dir)
            acct = canonical_account(board, agent)
            lane = board.get("lane_accounts", {}).get(acct, "none (can take any lane)")
            print(f"agent {agent} | account {acct} | tier {agent_tier(board, agent)} | "
                  f"lane {lane} | board {board_dir}"); return 0
        if args.cmd in ("products", "backlog"):
            board = sync(board_dir)
            order = product_order(board)
            if args.cmd == "products":
                for prod in sorted(order["products"], key=lambda q: int(q.get("rank", 99))):
                    print(f"{prod['rank']}. {prod['label']} ({prod['key']}): {', '.join(prod.get('repos') or ['anything not listed'])}")
                print(f"within a product: {order['within']}")
                return 0
            live = [(i, wp) for i, wp in enumerate(board["packages"])
                    if wp["status"] not in FINISHED and wp.get("lane") != "human"]
            live.sort(key=lambda t: (work_order_key(board, t[1]), t[0]))
            for _, wp in live[:args.limit]:
                fit = best_fit(board, wp)
                print(f"{wp['id']:<7} {product_of(board, wp)['label']:<20} added {added_at(board, wp)[:10]}  "
                      f"{wp['status']:<8} best: {fit['to']:<9} {wp['title']}")
            return 0
        if args.cmd in ("list", "next", "show"):
            board, now = sync(board_dir), now_utc()
            if args.cmd == "show":
                _print_package(board, find(board, args.wp), agent, now); return 0
            if args.cmd == "next":
                wp = suggest_next(board, agent, now)
                if not wp:
                    print("Nothing is claimable for you right now. Run `list` to see what's blocking."); return 0
                _print_package(board, wp, agent, now); return 0
            for wp in board["packages"]:
                ok, why = claimable(board, wp, agent, now)
                if args.available and not ok:
                    continue
                print(f"{wp['id']:<6} {wp['status']:<9} lane {wp.get('lane', '-'):<5} "
                      f"{package_tier(wp):<9} {wp['title']}  [{why}]")
            return 0
        if args.cmd == "claim":
            change = lambda b, n: apply_claim(b, args.wp, agent, n, args.branch, args.takeover)
        elif args.cmd == "checkpoint":
            change = lambda b, n: apply_checkpoint(b, args.wp, agent, n, args.note, args.next_step)
        elif args.cmd == "release":
            change = lambda b, n: apply_release(b, args.wp, agent, n, args.reason)
        elif args.cmd == "done":
            checked = False
            if args.repo_path:
                branch = find(sync(board_dir), args.wp).get("claim", {}) or {}
                commit_on_branch(args.repo_path, branch.get("branch", ""), args.commit)
                checked = True
            change = lambda b, n: apply_done(b, args.wp, agent, n, args.commit, args.tests, args.pr, checked)
        elif args.cmd == "route":
            change = lambda b, n: apply_route(b, args.wp, agent, n, args.to)
        elif args.cmd == "owner-answer":
            change = lambda b, n: apply_owner_answer(b, args.wp, agent, n, args.answer, args.comment)
        elif args.cmd == "set-products":
            try:
                spec = json.loads(args.spec)
            except json.JSONDecodeError as e:
                raise BoardError(f"set-products needs JSON: {e}")
            change = lambda b, n: apply_set_products(b, agent, n, spec)
        else:  # verify
            change = lambda b, n: apply_verify(b, args.wp, agent, n, bool(args.ok), args.ok or args.reject)
        print(mutate(board_dir, agent, change))
        return 0
    except BoardError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
