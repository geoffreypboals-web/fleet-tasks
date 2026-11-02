#!/usr/bin/env python3
"""backlog.py -- the fleet page's Backlog tab, its build worker and the autopilot
(owner decision D-2026-10-31-05; plan: docs/plans/fleet-backlog-autopilot-plan.md).

What it does:
- Reads the AI board (scripts/ai_board.py) and lists open work in the owner's
  order (product, then oldest; D-2026-10-31-04), each item with its best-fit AI.
- Owner actions write straight to the board: product ranks and repos
  (set-products), and Assign (route an item to an AI).
- "Plan on fleet" queues a written plan as an ordinary fleet task (kind draft).
- "Build" makes the change: a git worktree on a new branch fleet/<wp>-<n>, Aider
  (`local`, free) or Claude Code with edit tools only (`claude`, waits for the
  owner's OK and counts against its daily calls), then a push and a draft PR.
  Never main, never a shell for the paid CLI, and the fleet never runs the code it
  wrote (CI does, on the PR).
- One queue per builder (local, local-01 on Story Writer's GPU pair, claude, and any
  policy.json adds), each with its
  own worker: a queue whose AI is unavailable (Ollama not answering, a paid
  provider locked, off or out of calls) waits, and the other queues keep going.
  Each item goes to the queue of its best-fit AI (BUILDERS[...]["routes"]).
- Every build gets a time estimate (the item's est_minutes/est_hours, else the median
  of recent builds of that repo on that queue, else policy autopilot.estimates by repo,
  product or default; Story Writer work counts in hours). A build's clock starts only
  when it starts running. A build is never stopped for taking long: past
  timeout_factor x its estimate (at least timeout seconds) it's flagged "seems
  stalled" on the page for the owner to look at. max_timeout (default none) is an
  optional hard stop. The page shows when each running build should finish and when
  each waiting one should start (after the one ahead of it).
- The autopilot (off until the owner turns it on) fills each queue itself, in the
  agreed order, for small items only (best_fit auto_ok), one build at a time per
  queue, at most policy autopilot.max_builds_per_day in all. STOP and PAUSE stop it.

Inputs: scripts/ai_board.py from FLEET_REPO (default ~/my-board-repo), the
board clone (AI_BOARD_DIR / AI_BOARD_REMOTE, as ai_board.py), the
checkouts fleet.repos() finds, policy.json "autopilot" and "builders".
Outputs: board changes, fleet tasks, branches + draft PRs, STATE/builds.json,
STATE/AUTOPILOT (present = on).
Troubleshooting:
- "no checkout of X": clone the repo directly under a policy.sources.roots
  folder (default ~) on gpu-server.
- A build "failed: push": check `gh auth status` and that the repo's remote is
  the GitHub one.
- "refused: needs a paid-mid agent": the builder's board account is too low a
  tier; set policy autopilot.board_agents.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The my-board-repo checkout whose scripts/ai_board.py we use. Run from the
# repo, that's two folders up; installed (install.sh copies this file elsewhere), it's
# FLEET_REPO or ~/my-board-repo -- the same board clone (~/_board) every agent uses.
REPO = Path(os.environ.get("FLEET_REPO") or (HERE if (HERE / "scripts" / "ai_board.py").exists()
                                              else Path.home() / "my-board-repo"))
_bspec = importlib.util.spec_from_file_location("ai_board", REPO / "scripts" / "ai_board.py")
BOARD = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(BOARD)

OWNER_AGENT = "human-owner@gpu-server"
CACHE_SECONDS = 30
OPEN = {"open", "todo", "claimed", "blocked"}
DEFAULTS = {
    "max_builds_per_day": 4,      # autopilot builds; the owner's own Build presses aren't capped
    "max_waiting_ok": 3,          # paid builds the autopilot may leave waiting for an OK
    "max_files": 10,              # "simple corrections": bigger diffs are refused, not pushed
    "max_lines": 400,
    "timeout": 1800,              # seconds: never flagged as stalled sooner than this
    "timeout_factor": 3,          # past this many times its estimate, a running build is flagged "seems stalled"
    "max_timeout": None,          # seconds: an optional hard stop; None = never stop a build for time
    "estimates": {"default_minutes": 20, "by_product": {"story-writer": 240}, "by_repo": {}},
    "board_agents": {"local": "local-fleet@gpu-server", "local-01": "local-fleet@gpu-server", "claude": "claude-A@gpu-server"},
    # Story Writer's running jobs, each tagged with its Ollama endpoint: a builder on a shared
    # pair stays off while one runs there. None: not asked (the lease and loaded-model checks remain).
    "story_writer_activity_url": "http://127.0.0.1:8900/api/activity",
}
# One queue each. {prompt} and {model} are filled in; policy.json "builders" can change any
# field or add a builder (say "copilot" with "routes": ["copilot"]). routes: the best-fit AIs
# (ai_board.ROUTES) whose items this queue builds; the builder naming fewest routes wins.
# provider: the policy.json frontier entry whose limits and locks gate a paid builder.
# check_url: answered = a local builder is available.
BUILDERS = {
    "local": {"label": "Aider on qwen3.6:27b (free)", "paid": False, "model": "qwen3.6:27b",
              "routes": ["local"], "check_url": "http://127.0.0.1:11436/api/tags",
              "env": {"OLLAMA_API_BASE": "http://127.0.0.1:11436"},
              "cmd": ["aider", "--model", "ollama_chat/{model}", "--yes-always", "--no-auto-commits",
                      "--no-gitignore", "--no-check-update", "--no-show-model-warnings", "--analytics-disable",
                      "--no-pretty", "--message", "{prompt}"]},
    # Story Writer's GPU pair (pair01, Ollama :11434), free whenever Story Writer isn't using
    # it (owner 2026-11-01: it sat idle for a day). "pair" makes the builder step aside: it
    # takes nothing while the pair is leased, wanted, has another model loaded, or Story
    # Writer reports a job on it, and stops a running build (back in the queue) when any of
    # those starts. Two builders with the same routes share the work (least loaded first).
    "local-01": {"label": "Aider on qwen3.6:27b, Story Writer's GPU pair (free; steps aside for Story Writer)",
                 "paid": False, "model": "qwen3.6:27b", "routes": ["local"], "pair": "pair01",
                 "check_url": "http://127.0.0.1:11434/api/tags",
                 "env": {"OLLAMA_API_BASE": "http://127.0.0.1:11434"},
                 "cmd": ["aider", "--model", "ollama_chat/{model}", "--yes-always", "--no-auto-commits",
                      "--no-gitignore", "--no-check-update", "--no-show-model-warnings", "--analytics-disable",
                      "--no-pretty", "--message", "{prompt}"]},
    "claude": {"label": "Claude Code, edit tools only (paid)", "paid": True, "model": "sonnet",
               "routes": ["claude-A", "claude-B", "claude", "copilot", "gemini"], "provider": "claude",
               "cmd": ["claude", "-p", "{prompt}", "--model", "{model}", "--permission-mode", "acceptEdits",
                       "--allowedTools", "Read,Edit,Write,Glob,Grep", "--disallowedTools", "Bash,WebFetch,WebSearch",
                       "--strict-mcp-config", "--output-format", "text"]},
}
# Never committed by a build, whatever the builder wrote (same idea as fleet.SKIP_FILES).
SECRET_PATH = re.compile(r"(^|/)(\.env[^/]*|.*\.(pem|key|p12|pfx|kdbx)|id_(rsa|ed25519|ecdsa)[^/]*)$", re.IGNORECASE)
# The builder's own working files (Aider's chat history, cache): deleted before the change is checked.
TOOL_FILES = re.compile(r"(^|/)\.aider[^/]*$")


class SteppedAside(Exception):
    """A build on a shared GPU pair stopped because Story Writer needs the GPUs."""


def ollama_loaded(base, timeout=3):
    """Model names loaded on an Ollama (its /api/ps); [] when it can't be asked."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/ps", timeout=timeout) as r:  # nosec B310 -- a local Ollama from policy
            return [m.get("name") or m.get("model") for m in json.load(r).get("models", [])]
    except (OSError, ValueError):
        return []


def story_writer_jobs(url, timeout=3):
    """Story Writer's /api/activity jobs (each with its "endpoint"); [] when it can't be asked."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310 -- the owner-set Story Writer URL
            data = json.load(r)
        return data.get("jobs", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Backlog:
    """The Backlog tab's data and actions. `fleet` is the fleet module (for
    submit, repos, budget, STOP/PAUSE); `run` is subprocess.run (tests swap it)."""

    def __init__(self, fleet, board_dir=None, remote=None, run=subprocess.run, clock=time.time, probe=None,
                 popen=subprocess.Popen, ollama_ps=None, activity=None):
        self.fleet, self.run, self.clock, self.popen = fleet, run, clock, popen
        self.ollama_ps = ollama_ps or ollama_loaded       # models loaded on an Ollama (url -> names)
        self.activity = activity or story_writer_jobs     # Story Writer's running jobs (url -> [job])
        self.probe = probe or url_answers
        self.git_lock = threading.Lock()   # worktree add/remove: one at a time across queues
        self.board_dir = Path(board_dir) if board_dir else BOARD.default_board_dir()
        self.remote = remote
        self.lock = threading.RLock()
        self._board, self._at = None, 0.0
        self.builds_file = fleet.STATE / "builds.json"
        self.switch = fleet.STATE / "AUTOPILOT"

    # ---------------- settings ----------------
    def settings(self):
        """policy.json "autopilot" over DEFAULTS; "builders" over BUILDERS."""
        pol = self.fleet.policy()
        s = dict(DEFAULTS, **{k: v for k, v in pol.get("autopilot", {}).items() if not k.startswith("_")})
        s["board_agents"] = dict(DEFAULTS["board_agents"], **s.get("board_agents", {}))
        est = pol.get("autopilot", {}).get("estimates", {})
        s["estimates"] = {"default_minutes": est.get("default_minutes", DEFAULTS["estimates"]["default_minutes"]),
                          "by_product": dict(DEFAULTS["estimates"]["by_product"], **est.get("by_product", {})),
                          "by_repo": dict(est.get("by_repo", {}))}
        extra = {k: v for k, v in pol.get("builders", {}).items() if not k.startswith("_")}
        s["builders"] = {k: dict(BUILDERS.get(k, {}), **extra.get(k, {})) for k in [*BUILDERS, *extra]
                         if "cmd" in dict(BUILDERS.get(k, {}), **extra.get(k, {}))}
        return s

    # ---------------- the queues ----------------
    def queues_for(self, to):
        """Every builder (queue) that builds this best-fit AI's items: those whose routes name
        it with the fewest routes (two local builders share the local items)."""
        fits = [(len(v.get("routes", [])), k) for k, v in self.settings()["builders"].items() if to in v.get("routes", [])]
        if not fits:
            return []
        least = min(n for n, _ in fits)
        return [k for n, k in fits if n == least]

    def queue_for(self, to):
        """The builder (queue) for a best-fit AI: of queues_for, an available one with the
        least work waiting, then the first; None when no builder takes it (an owner task)."""
        names = self.queues_for(to)
        if len(names) < 2:
            return names[0] if names else None
        builds = self.builds()
        load = {n: sum(1 for bd in builds if bd["builder"] == n and bd["status"] in ("queued", "running"))
                for n in names}
        return min(names, key=lambda n: (self.unavailable(n) is not None, load[n], names.index(n)))

    def unavailable(self, name):
        """Why this queue's AI can't take a build now, or None when it can."""
        b = self.settings()["builders"][name]
        if b.get("paid"):
            pol = self.fleet.policy()
            prov = b.get("provider", name)
            if prov in pol.get("frontier", {}):
                return self.fleet.why_not(prov, pol, self.fleet.read_ledger(), self.fleet.locks())
            return None
        if b.get("check_url") and not self.probe(b["check_url"]):
            return f"{b['check_url'].split('/api')[0]} isn't answering"
        if b.get("pair"):
            return self.yield_reason(b)
        return None

    def yield_reason(self, b):
        """Why a builder on a shared GPU pair must leave it to Story Writer now, or None:
        the pair is leased or wanted (gpu_lease), another model is loaded on its Ollama,
        or Story Writer reports a job running there."""
        pair, lease = b["pair"], getattr(self.fleet, "LEASE", None)
        if lease:
            try:
                held, wants = lease.holder_of(pair), lease.wanted(pair)
            except OSError:              # no lease folder: nothing can hold or want the pair
                held, wants = None, []
            if held:
                return f"its GPUs are checked out by {held.get('holder', 'another app')} (GPU lease)"
            if wants:
                return f"{', '.join(w.get('who', '?') for w in wants)} wants these GPUs"
        base = b.get("env", {}).get("OLLAMA_API_BASE") or b["check_url"].split("/api")[0]
        other = [m for m in self.ollama_ps(base) if m != b.get("model") and not self.fleet.is_embed_model(m)]
        if other:
            return f"{', '.join(other)} is loaded on these GPUs (Story Writer's work)"
        url = self.settings().get("story_writer_activity_url")
        port = ":" + base.rsplit(":", 1)[-1].split("/")[0]
        if url:
            mine = [j for j in self.activity(url) if port in str(j.get("endpoint") or "")
                    and str(j.get("status", "running")).lower() in ("running", "active", "working")]
            if mine:
                return f"Story Writer is running {mine[0].get('book_id') or 'a book'} on these GPUs"
        return None

    def queues(self):
        """Each queue for the page: name, label, available (or why not), its builds by status,
        and free_at: when the queue should have finished what it holds now (estimates)."""
        builds = self.builds()
        plan = self.schedule(builds)
        out = []
        for name, b in self.settings()["builders"].items():
            mine = [bd for bd in builds if bd["builder"] == name]
            why = self.unavailable(name)
            ends = [plan[bd["id"]]["done"] for bd in mine if bd["id"] in plan]
            out.append({"name": name, "label": b.get("label", name), "paid": bool(b.get("paid")),
                        "routes": b.get("routes", []), "available": why is None, "why": why or "",
                        "free_at": max(ends) if ends else None,
                        "stalled": any(plan[bd["id"]]["stalled"] for bd in mine if bd["id"] in plan),
                        **{k: sum(1 for bd in mine if bd["status"] == k) for k in ("running", "queued", "needs_ok")}})
        return out

    # ---------------- time estimates ----------------
    def estimate(self, wp, builder):
        """How long a build of this item on this queue should take: (minutes, where that
        comes from). The item's own est_minutes / est_hours first, then the median of the
        last 5 finished builds of the same repo on the same queue, then policy by repo,
        by product, or the default."""
        if wp.get("est_minutes"):
            return float(wp["est_minutes"]), "the item's estimate"
        if wp.get("est_hours"):
            return float(wp["est_hours"]) * 60, "the item's estimate"
        repo = (wp.get("repos") or [""])[0]
        took = [took_minutes(bd) for bd in self.builds()
                if bd["builder"] == builder and bd["repo"].lower() == repo.lower() and bd["status"] in ("pr_open", "no_change")]
        took = sorted(x for x in took if x)[-5:] if took else []
        if took:
            return max(1.0, took[len(took) // 2]), f"median of {len(took)} recent build(s) of {repo}"
        est = self.settings()["estimates"]
        by_repo = {k.lower(): v for k, v in est["by_repo"].items()}
        if repo.lower() in by_repo:
            return float(by_repo[repo.lower()]), f"policy estimate for {repo}"
        product = BOARD.product_of(self.board(), wp)
        if product["key"] in est["by_product"]:
            return float(est["by_product"][product["key"]]), f"policy estimate for {product['label']}"
        return float(est["default_minutes"]), "default estimate"

    def stall_after(self, minutes):
        """Seconds after its start that a run with this estimate is flagged "seems stalled":
        timeout_factor x the estimate, and never sooner than timeout. Only a flag: the
        build keeps running."""
        s = self.settings()
        return int(max(s["timeout"], minutes * 60 * s["timeout_factor"]))

    def schedule(self, builds=None):
        """Expected start and finish (UTC ISO) for every build still running, queued or
        waiting for an OK, one after another in each queue: {id: {"start", "done", "overdue",
        "stalled"}}. A waiting build's time starts when the one ahead of it should finish;
        a running build counts from its real start. overdue: past its estimate (it may just
        be slow); stalled: past stall_after, worth a look."""
        builds = self.builds() if builds is None else builds
        now = self.clock()
        out = {}
        for q in {bd["builder"] for bd in builds}:
            free = now
            mine = [bd for bd in builds if bd["builder"] == q]
            for bd in [x for x in mine if x["status"] == "running"] + \
                      [x for x in mine if x["status"] in ("queued", "needs_ok")]:
                mins = float(bd.get("est_minutes") or self.settings()["estimates"]["default_minutes"])
                stalled = False
                if bd["status"] == "running" and bd.get("started"):
                    start = parse_utc(bd["started"])
                    done = start + mins * 60
                    overdue = done < now
                    stalled = now - start > self.stall_after(mins)
                    done = max(done, now + 60)
                else:
                    start, overdue = free, False
                    done = start + mins * 60
                free = max(free, done)
                out[bd["id"]] = {"start": iso(start), "done": iso(done), "overdue": overdue, "stalled": stalled}
        return out

    # ---------------- the board ----------------
    def board(self, fresh=False):
        """The board, synced at most every CACHE_SECONDS (the page polls)."""
        with self.lock:
            if fresh or self._board is None or self.clock() - self._at > CACHE_SECONDS:
                BOARD.ensure_clone(self.board_dir, self.remote or BOARD.default_remote())
                self._board, self._at = BOARD.sync(self.board_dir), self.clock()
            return self._board

    def change(self, agent, fn):
        """Apply one board change (ai_board.mutate), then drop the cache."""
        with self.lock:
            BOARD.ensure_clone(self.board_dir, self.remote or BOARD.default_remote())
            line = BOARD.mutate(self.board_dir, agent, fn)
            self._board = None
            return line

    def state(self):
        """Everything the Backlog tab shows: products, order within, the open items in
        work order with best fit, the builds and the autopilot's switch and counts."""
        b = self.board()
        order = BOARD.product_order(b)
        live = [(i, wp) for i, wp in enumerate(b["packages"]) if wp.get("status") in OPEN]
        live.sort(key=lambda t: (0 if t[1].get("lane") != "human" else 1, BOARD.work_order_key(b, t[1]), t[0]))
        builds = self.builds()
        last = {}
        for bd in builds:
            last[bd["wp"]] = bd
        items, owner = [], []
        for _, wp in live:
            if wp.get("lane") == "human":
                owner.append(owner_task(b, wp))
                continue
            fit = BOARD.best_fit(b, wp)
            items.append({"id": wp["id"], "title": wp.get("title", ""), "status": wp["status"],
                          "product": BOARD.product_of(b, wp)["label"], "added": BOARD.added_at(b, wp)[:10],
                          "repos": wp.get("repos", []), "tier": BOARD.package_tier(wp), "lane": wp.get("lane"),
                          "est_sessions": wp.get("est_sessions"), "claim": (wp.get("claim") or {}).get("agent"),
                          "route": (wp.get("route") or {}).get("to"), "fit": fit,
                          "build": last.get(wp["id"])})
        s = self.settings()
        return {"products": order["products"], "within": order["within"], "items": items, "owner_tasks": owner,
                "routes": {k: v["label"] for k, v in BOARD.ROUTES.items()},
                "builders": {k: v["label"] for k, v in s["builders"].items()},
                "queues": self.queues(),
                "autopilot": {"on": self.switch.exists(), "today": self.builds_today(auto_only=True),
                              "max": s["max_builds_per_day"]},
                "schedule": self.schedule(builds),
                "builds": builds[-20:][::-1]}

    def set_order(self, spec):
        """The owner's product ranks / repos / new products (ai_board set-products)."""
        if not isinstance(spec, dict):
            raise ValueError("order: a JSON object")
        return self.change(OWNER_AGENT, lambda b, n: BOARD.apply_set_products(b, OWNER_AGENT, n, spec))

    def assign(self, wp_id, to):
        """Route an item to an AI (ai_board route)."""
        return self.change(OWNER_AGENT, lambda b, n: BOARD.apply_route(b, wp_id, OWNER_AGENT, n, to))

    def item(self, wp_id):
        b = self.board()
        try:
            return BOARD.find(b, wp_id)
        except BOARD.BoardError as e:
            raise ValueError(str(e))

    # ---------------- plan on fleet ----------------
    def owner_answer(self, wp_id, answer, comment="", by=OWNER_AGENT):
        """The owner closes one of their tasks from the page: done (I did it), approved or
        denied, with a comment (ai_board apply_owner_answer)."""
        agent = by if BOARD.is_human(by) else OWNER_AGENT
        return self.change(agent, lambda b, n: BOARD.apply_owner_answer(b, wp_id, agent, n, answer, comment))

    def plan(self, wp_id):
        """Queue a written plan for the item as a fleet task (kind draft); returns its id."""
        wp = self.item(wp_id)
        prompt = ("Write a short, concrete implementation plan for this board work package: the files to "
                  "change, the steps, the tests to add, and anything that needs the owner. Don't write the "
                  "whole code.\n\n" + spec_text(wp))
        return self.fleet.submit("draft", prompt[:20000])

    # ---------------- builds ----------------
    def builds(self):
        with self.lock:
            try:
                return json.loads(self.builds_file.read_text())
            except (OSError, ValueError):
                return []

    def _save(self, builds):
        self.builds_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.builds_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(builds, indent=1))
        tmp.replace(self.builds_file)

    def _update(self, bid, **fields):
        with self.lock:
            builds = self.builds()
            for bd in builds:
                if bd["id"] == bid:
                    bd.update(fields, updated=utc_now())
            self._save(builds)

    def builds_today(self, auto_only=False):
        day = utc_now()[:10]
        return sum(1 for bd in self.builds() if bd["created"][:10] == day and (bd["auto"] or not auto_only))

    def request_build(self, wp_id, builder=None, auto=False, by=OWNER_AGENT):
        """Queue a build of the item. builder: local or claude (default: local when the
        best fit is a local model, else claude). A paid build waits for approve_build."""
        wp = self.item(wp_id)
        b = self.board()
        fit = BOARD.best_fit(b, wp)
        if fit["to"] == "human":
            raise ValueError(f"{wp_id} is an owner task; nothing for an AI to build.")
        builders = self.settings()["builders"]
        builder = builder or self.queue_for(fit["to"])
        if builder not in builders:
            raise ValueError(f"builder: one of {', '.join(builders)}")
        if not wp.get("repos"):
            raise ValueError(f"{wp_id} names no repo to change.")
        with self.lock:
            builds = self.builds()
            if any(bd["wp"] == wp_id and bd["status"] in ("needs_ok", "queued", "running") for bd in builds):
                raise ValueError(f"{wp_id} already has a build waiting or running.")
            bid = 1 + max((bd["id"] for bd in builds), default=0)
            paid = self.settings()["builders"][builder].get("paid", False)
            mins, why = self.estimate(wp, builder)
            builds.append({"est_minutes": round(mins, 1), "est_source": why, "id": bid, "wp": wp_id, "title": wp.get("title", ""), "repo": wp["repos"][0],
                           "builder": builder, "auto": auto, "by": by, "status": "needs_ok" if paid else "queued",
                           "created": utc_now(), "updated": utc_now(), "note": "", "pr": None, "branch": None})
            self._save(builds)
        return bid

    def approve_build(self, bid, ok=True):
        """The owner's OK (or no) for a paid build."""
        bd = next((x for x in self.builds() if x["id"] == bid), None)
        if not bd or bd["status"] != "needs_ok":
            raise ValueError("That build isn't waiting for an OK.")
        self._update(bid, status="queued" if ok else "denied", note="" if ok else "you said no")

    def next_queued(self, queue):
        """The oldest build waiting in this queue, or None."""
        return next((bd for bd in self.builds() if bd["status"] == "queued" and bd["builder"] == queue), None)

    def recover(self):
        """After a restart: a build left 'running' was cut off; mark it so."""
        for bd in self.builds():
            if bd["status"] == "running":
                self._update(bd["id"], status="failed", note="interrupted by a restart")

    def run_build(self, bd):
        """One build, start to finish. Returns its final status."""
        s = self.settings()
        builder = s["builders"][bd["builder"]]
        agent = s["board_agents"].get(bd["builder"], f"{bd['builder']}-fleet@gpu-server")
        why = self.unavailable(bd["builder"])
        if why:
            self._update(bd["id"], note=f"waiting: {why}")
            return "queued"
        checkout = next((p for n, p in self.fleet.repos().items() if n.lower() == bd["repo"].lower()), None)
        if not checkout:
            self._update(bd["id"], status="failed", note=f"no checkout of {bd['repo']} on this machine")
            return "failed"
        n = sum(1 for x in self.builds() if x["wp"] == bd["wp"])
        branch = f"fleet/{bd['wp'].lower()}-{n}"
        work = self.fleet.STATE / "worktrees" / f"{bd['repo']}-{bd['wp']}-{n}"
        self._update(bd["id"], status="running", branch=branch, note="claiming on the board", started=utc_now())
        try:
            self.change(agent, lambda b, now: BOARD.apply_claim(b, bd["wp"], agent, now, branch=branch))
        except BOARD.BoardError as e:
            self._update(bd["id"], status="failed", note=f"board refused the claim: {e}")
            return "failed"
        result, note, pr = "failed", "", None
        try:
            with self.git_lock:
                base = self._default_branch(checkout)
                self._git(checkout, "fetch", "--quiet", "origin", base)
                if work.exists():
                    self._git(checkout, "worktree", "remove", "--force", str(work), check=False)
                self._git(checkout, "worktree", "add", "-b", branch, str(work), f"origin/{base}")
            self._update(bd["id"], note=f"{builder.get('label', bd['builder'])} is working")
            wp = self.item(bd["wp"])
            prompt = build_prompt(wp)
            cmd = [prompt if x == "{prompt}" else x.replace("{model}", builder.get("model", "")) for x in builder["cmd"]]
            if builder.get("paid"):
                self.fleet.ledger({"event": "call", "provider": bd["builder"], "task": f"build-{bd['id']}",
                                   "kind": "build", "chars": len(prompt), "approved_by": bd["by"]})
            env = dict(os.environ, **builder.get("env", {}))
            if builder.get("pair"):
                out = self._run_yielding(builder, cmd, str(work), env, s.get("max_timeout"))
            else:
                out = self.run(cmd, cwd=str(work), capture_output=True, text=True, timeout=s.get("max_timeout"),
                               env=env, check=False)
            if out.returncode != 0:
                raise RuntimeError(f"{bd['builder']} exited {out.returncode}: {(out.stderr or out.stdout)[-300:]}")
            for f in self._changed(work):
                if TOOL_FILES.search(f):
                    target = work / f
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
            changed = self._changed(work)
            if not changed:
                result, note = "no_change", "the builder changed nothing"
            else:
                secret = [f for f in changed if SECRET_PATH.search(f)]
                files, lines = len(changed), self._lines(work)
                if secret:
                    raise RuntimeError(f"touched files a build never commits: {', '.join(secret[:3])}")
                if files > s["max_files"] or lines > s["max_lines"]:
                    raise RuntimeError(f"too big for a simple correction ({files} files, {lines} lines; "
                                       f"limits {s['max_files']}/{s['max_lines']})")
                self._git(work, "add", "-A")
                self._git(work, "-c", "user.name=fleet-tasks", "-c", "user.email=fleet-tasks@gpu-server",
                          "commit", "--quiet", "-m", f"fleet: {bd['wp']} {bd['title']}"[:72] +
                          f"\n\nBuilt by {builder.get('label', bd['builder'])} from the AI board item {bd['wp']}.")
                self._git(work, "push", "--quiet", "-u", "origin", branch)
                body = (f"Board item **{bd['wp']}**: {bd['title']}\n\nBuilt by the fleet "
                        f"({builder.get('label', bd['builder'])}; {'autopilot' if bd['auto'] else 'Build button'}). "
                        f"{files} files, {lines} lines. The fleet didn't run this code: CI on this PR is the first "
                        f"test. Review before merging.\n\n---\n_Generated by fleet-tasks on gpu_server_")
                pr = self._run_ok(["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
                                   "--title", f"{bd['wp']}: {bd['title']}"[:120], "--body", body], work).strip()
                result, note = "pr_open", f"draft PR {pr}"
        except SteppedAside as e:
            result, note = "queued", f"stepped aside: {e}; it runs again when these GPUs are free"
        except subprocess.TimeoutExpired as e:
            note = (f"stopped after {int(e.timeout // 60)} min by policy autopilot.max_timeout "
                    f"({bd.get('est_minutes')} min estimated, {bd.get('est_source', 'default')})")
        except (RuntimeError, OSError) as e:
            note = str(e)[:400]
        finally:
            with self.git_lock:
                self._git(checkout, "worktree", "remove", "--force", str(work), check=False)
        try:
            if result == "pr_open":
                self.change(agent, lambda b, now: BOARD.apply_checkpoint(
                    b, bd["wp"], agent, now, f"fleet build: {note}", next_step="owner: review and merge the draft PR"))
            else:
                self.change(agent, lambda b, now: BOARD.apply_release(b, bd["wp"], agent, now, f"fleet build: {note}"))
        except BOARD.BoardError as e:
            note += f" (board: {e})"
        self._update(bd["id"], status=result, note=note, pr=pr,
                     **({} if result == "queued" else {"finished": utc_now()}))
        return result

    YIELD_CHECK_S = 30

    def _run_yielding(self, builder, cmd, cwd, env, timeout):
        """The builder's command on a shared GPU pair: checks every YIELD_CHECK_S whether
        Story Writer needs the pair, and if so stops the build (SteppedAside)."""
        proc = self.popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = self.clock()
        while True:
            try:
                out, err = proc.communicate(timeout=self.YIELD_CHECK_S)
                return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                pass
            why = self.yield_reason(builder)
            if why or (timeout and self.clock() - started > timeout):
                proc.terminate()
                try:
                    proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                if why:
                    raise SteppedAside(why)
                raise subprocess.TimeoutExpired(cmd, timeout)

    # ---------------- the autopilot ----------------
    def set_autopilot(self, on):
        self.fleet.STATE.mkdir(parents=True, exist_ok=True)
        if on:
            self.switch.write_text(utc_now() + "\n")
        else:
            self.switch.unlink(missing_ok=True)

    def autopilot_pick(self, queue=None):
        """The next item the autopilot should build (for this queue, if named), or None:
        in the agreed order, best fit auto_ok, open and unclaimed with no unmet deps,
        a checkout here, and no build of it before."""
        b = self.board(fresh=True)
        tried = {bd["wp"] for bd in self.builds()}
        names = {n.lower() for n in self.fleet.repos()}
        live = [(i, wp) for i, wp in enumerate(b["packages"]) if wp.get("status") in ("open", "todo")]
        live.sort(key=lambda t: (BOARD.work_order_key(b, t[1]), t[0]))
        for _, wp in live:
            fit = BOARD.best_fit(b, wp)
            if not fit["auto_ok"] or wp["id"] in tried or BOARD.unmet_deps(b, wp):
                continue
            if queue and queue not in self.queues_for(fit["to"]):
                continue
            if not wp.get("repos") or wp["repos"][0].lower() not in names:
                continue
            return wp["id"]
        return None

    def tick_queue(self, queue):
        """One step for one queue: run its oldest queued build when its AI is available,
        else (autopilot on) queue its next item. Returns what it did."""
        if (self.fleet.STATE / "STOP").exists() or self.fleet.paused():
            return "stopped"
        bd = self.next_queued(queue)
        if bd:
            why = self.unavailable(queue)
            if why:
                self._update(bd["id"], note=f"waiting: {why}")
                return f"waiting: {why}"
            return f"build {bd['id']}: {self.run_build(bd)}"
        if not self.switch.exists():
            return "off"
        s = self.settings()
        builds = self.builds()
        if any(x["builder"] == queue and x["status"] in ("needs_ok", "running") for x in builds):
            return "busy"
        if self.builds_today(auto_only=True) >= s["max_builds_per_day"]:
            return "daily limit"
        if s["builders"][queue].get("paid") and sum(1 for x in builds if x["status"] == "needs_ok") >= s["max_waiting_ok"]:
            return "waiting for your OK"
        wp = self.autopilot_pick(queue)
        if not wp:
            return "nothing to build"
        try:
            bid = self.request_build(wp, builder=queue, auto=True, by="autopilot")
        except ValueError as e:    # another queue took it a moment ago
            return f"skipped {wp}: {e}"
        return f"queued build {bid} for {wp}"

    def tick(self):
        """One step for every queue, in turn (the CLI; the page runs one worker per queue).
        Returns {queue: what it did}."""
        return {q: self.tick_queue(q) for q in self.settings()["builders"]}

    def loop(self, every=60):
        """The background threads in web.py: one worker per queue, each ticking forever and
        never dying on an error, so a stuck or unavailable AI holds up only its own queue."""
        self.recover()
        workers = {}
        while True:
            for q in self.settings()["builders"]:
                if q not in workers or not workers[q].is_alive():
                    workers[q] = threading.Thread(target=self._worker, args=(q, every), name=f"backlog-{q}", daemon=True)
                    workers[q].start()
            time.sleep(every)

    def _worker(self, queue, every):
        while True:
            try:
                self.tick_queue(queue)
            except Exception as e:  # noqa: BLE001 -- one bad tick must not stop the queue; it's logged
                print(f"backlog queue {queue}: {e.__class__.__name__}: {e}", flush=True)
            time.sleep(every)

    # ---------------- git helpers ----------------
    def _run_ok(self, cmd, cwd):
        out = self.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120, check=False)
        if out.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(out.stderr or out.stdout).strip()[-300:]}")
        return out.stdout

    def _git(self, path, *args, check=True):
        cmd = ["git", "-C", str(path), *args]
        if check:
            return self._run_ok(cmd, path if Path(path).exists() else self.fleet.STATE)
        return self.run(cmd, capture_output=True, text=True, timeout=120, check=False).stdout

    def _default_branch(self, checkout):
        ref = self._git(checkout, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).strip()
        return ref.rsplit("/", 1)[-1] if ref else "main"

    def _changed(self, work):
        out = self._git(work, "status", "--porcelain", "--untracked-files=all")
        return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]

    def _lines(self, work):
        self._git(work, "add", "-A", "--intent-to-add")
        total = 0
        for line in self._git(work, "diff", "--numstat").splitlines():
            parts = line.split("\t")
            total += sum(int(x) for x in parts[:2] if x.isdigit())
        return total


COMMAND = re.compile(r"`([^`\n]{2,400})`")


def owner_task(board, wp):
    """An owner task for the "Your tasks" list: what to do (next step, steps, done-when),
    the commands found in it (text in backticks), what waits on it, and whether it
    still waits on other items."""
    text = " ".join([wp.get("next_step") or "", *(wp.get("steps") or []), *(wp.get("acceptance") or [])])
    blocks = [x["id"] for x in board["packages"] if wp["id"] in (x.get("depends_on") or [])
              and x.get("status") not in BOARD.FINISHED]
    return {"id": wp["id"], "title": wp.get("title", ""), "status": wp["status"],
            "product": BOARD.product_of(board, wp)["label"], "added": BOARD.added_at(board, wp)[:10],
            "next_step": wp.get("next_step") or "", "steps": list(wp.get("steps") or []),
            "acceptance": list(wp.get("acceptance") or []), "repos": wp.get("repos", []),
            "commands": list(dict.fromkeys(COMMAND.findall(text))),
            "waits_on": BOARD.unmet_deps(board, wp), "unblocks": blocks,
            "last_note": next((h.get("note") for h in reversed(wp.get("history", [])) if h.get("note")), "")}


def parse_utc(text):
    """Epoch seconds from utc_now()'s format."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def iso(epoch):
    """utc_now()'s format for an epoch time."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def took_minutes(bd):
    """How long a finished build ran, in minutes, or None when it wasn't timed."""
    if not (bd.get("started") and bd.get("finished")):
        return None
    return (parse_utc(bd["finished"]) - parse_utc(bd["started"])) / 60


def url_answers(url, timeout=3):
    """True when a GET of the URL answers with HTTP 2xx (a local model server is up)."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310 -- a fixed local URL from policy.json
            return 200 <= r.status < 300
    except (OSError, ValueError):
        return False


def spec_text(wp):
    """The package as plain text for a model: title, repos, steps, acceptance, notes."""
    out = [f"{wp['id']}: {wp.get('title', '')}", f"Repos: {', '.join(wp.get('repos', [])) or 'not given'}"]
    for key, head in (("steps", "Steps"), ("acceptance", "Done when"), ("owns", "Files it owns")):
        if wp.get(key):
            out.append(head + ":\n" + "\n".join(f"- {x}" for x in wp[key]))
    for h in wp.get("history", [])[-3:]:
        if h.get("note"):
            out.append(f"Earlier note ({h.get('event')}): {h['note']}")
    return "\n\n".join(out)


def build_prompt(wp):
    """What the builder is asked to do: the smallest change that meets the package."""
    return ("You are making a small, reviewable change in this git checkout for the board work package "
            "below. Make the smallest change that meets it; add or update a test when the repo has tests. "
            "Don't touch unrelated files, don't add dependencies, and never write secrets, tokens or "
            ".env files. Don't commit: the fleet commits and opens a draft PR for review.\n\n" + spec_text(wp))
