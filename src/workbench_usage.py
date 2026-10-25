"""Persistent, rebuildable token accounting for saved Codex invocations.

Logs are imported in the background, never on a summary request. Thread/turn
IDs deduplicate archived, installed, and merged repair logs. The index retains
observed history even when a job or an installed log is removed or replaced.
"""

from collections import defaultdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from codex_transcripts import transcript_stage
from write_paper import resolve_manifest_path


PRICING_VERSION = "openai-2026-10-25"
PRICING_URL = "https://developers.openai.com/api/docs/pricing"
# USD per million tokens: uncached input, cache read, cache write (30m), output.
# https://developers.openai.com/api/docs/models/gpt-6.1-sol
# https://developers.openai.com/api/docs/pricing?tab=suite
RATES = {
    "gpt-6-astra": (10, 1, 12.5, 50),
    "gpt-6.1-sol": (2, .1, 2.5, 10),
    "gpt-6-sol": (2, .2, 2.5, 10),
    "gpt-6-luna": (.1, .01, .125, .5),
    "gpt-5.6-sol": (4, .4, 5, 20),
    "gpt-5.3-codex": (1.75, .175, 1.75, 14),
}
TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)
EVENT_RE = re.compile(rb'^\s*\{\s*"type"\s*:\s*"(thread.started|turn.started|turn.completed|turn.failed|error)"')
ENTITY_RE = re.compile(r"^(OP-\d+|attempt-\d+|draft-\d+)$")


def path_key(value):
    return os.path.normcase(str(resolve_manifest_path(str(value)))).replace("\\", "/").rstrip("/")


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def token_usage(value):
    """Reject malformed counters rather than producing a plausible zero cost."""
    if not isinstance(value, dict):
        return None
    if not all(type(value.get(key)) is int and value[key] >= 0
               for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
        return None
    result = {key: value[key] for key in TOKEN_FIELDS if key in value}
    if any(type(n) is not int or n < 0 for n in result.values()):
        return None
    if result["cached_input_tokens"] + result.get("cache_write_input_tokens", 0) > result["input_tokens"]:
        return None
    if result.get("reasoning_output_tokens", 0) > result["output_tokens"]:
        return None
    return result


def model_rates(model, tier):
    """Return the same effective USD/MTok rates used for calculation and display."""
    rates = RATES.get(model)
    if rates is None or tier not in {"standard", "fast", "ultrafast"}:
        return None
    if tier == "ultrafast" and model not in {"gpt-6-astra", "gpt-6.1-sol"}:
        return None
    multiplier = {"standard": 1, "fast": 2, "ultrafast": 6}[tier]
    return tuple(rate * multiplier for rate in rates)


def estimated_cost(usage, model, tier):
    rates = model_rates(model, tier)
    if rates is None:
        return None
    cached = usage["cached_input_tokens"]
    writes = usage.get("cache_write_input_tokens", 0)
    uncached = usage["input_tokens"] - cached - writes
    return (uncached * rates[0] + cached * rates[1]
            + writes * rates[2] + usage["output_tokens"] * rates[3]) / 1_000_000


class UsageIndex:
    def __init__(self, store, roots, manuscripts, hub=None):
        self.store = store
        self.roots = [Path(root).resolve() for root in roots]
        self.manuscripts = Path(manuscripts).resolve()
        self.hub = hub
        self.stopping = threading.Event()
        self.pending = threading.Event()
        self.thread = None
        self.indexing = True
        self.error = ""
        self.updated_at = None
        self._json_cache = {}
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS usage_sources (
                    path TEXT PRIMARY KEY, signature TEXT NOT NULL,
                    offset INTEGER NOT NULL, state_json TEXT NOT NULL,
                    tail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_invocations (
                    id TEXT PRIMARY KEY, stage TEXT NOT NULL, model TEXT,
                    tier TEXT, quality INTEGER NOT NULL,
                    usage_json TEXT, completed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_scopes (
                    invocation_id TEXT NOT NULL REFERENCES usage_invocations(id),
                    kind TEXT NOT NULL, path TEXT NOT NULL,
                    PRIMARY KEY (invocation_id, kind, path)
                );
                CREATE INDEX IF NOT EXISTS usage_scope_lookup ON usage_scopes(kind, path);
            """)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="usage-index", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.pending.set()
        if self.thread:
            self.thread.join(timeout=5)

    def request_refresh(self):
        self.pending.set()

    def _loop(self):
        while not self.stopping.is_set():
            self.pending.clear()
            self.indexing = True
            try:
                self.refresh()
                self.error = ""
                self.updated_at = time.time()
            except Exception as exc:
                self.error = str(exc)
                logging.exception("Could not index Codex usage")
            finally:
                self.indexing = False
                if self.hub:
                    self.hub.publish("usage.changed")
            self.pending.wait(30)

    def _json(self, path):
        try:
            stat = path.stat()
        except OSError:
            return {}
        signature = stat.st_mtime_ns, stat.st_size
        cached = self._json_cache.get(path)
        if cached is None or cached[0] != signature:
            cached = signature, read_json(path)
            self._json_cache[path] = cached
        return cached[1]

    def _scopes(self, events):
        """Attribute by actual workspace, never all inputs of a batch job."""
        scopes = set()
        for directory in events.parents:
            if ENTITY_RE.fullmatch(directory.name):
                if directory.name.startswith("OP-"):
                    scopes.add(("problem", path_key(directory)))
                    scopes.add(("paper", path_key(directory.parent)))
                    break
                elif directory.name.startswith("attempt-"):
                    scopes.add(("attempt", path_key(directory)))
                else:
                    scopes.add(("draft", path_key(directory)))
                    scopes.add(("manuscript", path_key(directory.parent)))
                    break
            if directory.parent == self.manuscripts:
                scopes.add(("manuscript", path_key(directory)))
            if any(directory == root or root in directory.parents for root in self.roots):
                if (directory / "paper.pdf").is_file() or (directory / "source").is_dir():
                    scopes.add(("paper", path_key(directory)))
                    break
        return scopes

    @staticmethod
    def _tier(value):
        if value.get("requested_service_tier"):
            return value["requested_service_tier"]
        if value.get("requested_ultrafast_mode") is True:
            return "ultrafast"
        fast = value.get("requested_fast_mode")
        return ("fast" if fast else "standard") if type(fast) is bool else None

    def _installed_context(self, events):
        prefix = events.name.removesuffix("events.jsonl")
        if prefix == "repair-":
            prefix = ""
        manifest = self._json(events.parent / f"{prefix}manifest.json")
        if prefix == "review-" and events.parent.name.startswith("draft-"):
            manifest = self._json(events.parent / "paper-review-manifest.json")
        return {"stage": transcript_stage(events) or "Codex",
                "model": manifest.get("requested_model"), "tier": self._tier(manifest),
                "quality": 2, "scopes": sorted(self._scopes(events))}

    def _managed_context(self, sources, run):
        events = resolve_manifest_path(sources["events"])
        scopes = self._scopes(events)
        scopes.update({("run", run["id"]), ("job", run["job_id"])})
        stage = sources.get("stage") or transcript_stage(events) or "Codex"
        argv = json.loads(run["argv_json"])
        review = stage.startswith(("Review", "Paper review")) and run["action"] in {"solve", "write", "revise"}
        prefix = "--review-" if review else "--"

        def option(flag):
            for index, arg in enumerate(argv):
                if arg == flag and index + 1 < len(argv):
                    return argv[index + 1]
                if arg.startswith(flag + "="):
                    return arg[len(flag) + 1:]
            return None

        model = sources.get("model") or option(prefix + "model") or option("--model")
        tier = sources.get("service_tier")
        if tier is None:
            tier = "ultrafast" if prefix + "ultrafast" in argv else "fast" if prefix + "fast" in argv else None
            tier = tier or ("ultrafast" if "--ultrafast" in argv else "fast" if "--fast" in argv else "standard")
        # A review command naming an attempt is precise even before installation.
        targets = json.loads(run["targets_json"])
        if len(targets) == 1 and targets[0].get("kind") in {"attempt", "draft"}:
            scopes.add((targets[0]["kind"], path_key(targets[0]["path"])))
        return {"stage": stage, "model": model, "tier": tier,
                "quality": 3 if sources.get("model") else 1, "scopes": sorted(scopes)}

    def refresh(self):
        """Discover sources; import only new/changed files, in short transactions."""
        with self.store.connect() as db:
            runs = db.execute("""SELECT runs.id, runs.job_id, runs.argv_json,
                runs.targets_json, runs.log_path, jobs.action FROM runs
                JOIN jobs ON jobs.id = runs.job_id""").fetchall()
            known = {row["path"]: dict(row) for row in db.execute("SELECT * FROM usage_sources")}
        changed = 0
        for run in runs:
            if self.stopping.is_set():
                return changed
            root = resolve_manifest_path(run["log_path"]).parent / "codex"
            for folder in root.glob("turn-*"):
                sources = self._json(folder / "source.json")
                if not isinstance(sources.get("events"), str):
                    continue
                events = folder / "events"
                if not events.is_file():
                    events = resolve_manifest_path(sources["events"])
                    if not any(events.is_relative_to(root) or events.is_relative_to(path)
                               for path in [*self.roots, self.manuscripts]):
                        continue
                changed += self.import_source(events, self._managed_context(sources, run), known)
        # Installed logs also cover CLI runs predating managed transcript archives.
        for root in dict.fromkeys([*self.roots, self.manuscripts]):
            for directory, children, files in os.walk(root):
                if self.stopping.is_set():
                    return changed
                children[:] = [name for name in children if name not in {
                    "source", "artifacts", "figures", "code", "paper-input", ".git", ".loose-ends",
                } and not name.startswith((".attempt-install-", ".review-install-", ".draft-install-"))]
                for name in files:
                    if name.endswith("events.jsonl"):
                        events = Path(directory) / name
                        changed += self.import_source(events, self._installed_context(events), known)
        return changed

    def import_source(self, events, context, known=None):
        """Incrementally consume complete lines; reset safely on replacement."""
        key = path_key(events)
        try:
            stat = events.stat()
        except OSError:
            return 0
        context_json = json.dumps(context, sort_keys=True)
        signature = json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ino, context_json])
        if known is None:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM usage_sources WHERE path = ?", (key,)).fetchone()
                prior = dict(row) if row else None
        else:
            prior = known.get(key)
        if prior and prior["signature"] == signature:
            return 0
        offset, state = 0, {}
        records = {}
        try:
            with events.open("rb") as stream:
                if prior:
                    old = json.loads(prior["signature"])
                    if old[0] < stat.st_size and old[2:] == [stat.st_ino, context_json]:
                        stream.seek(max(0, prior["offset"] - 128))
                        tail = stream.read(min(128, prior["offset"]))
                        if hashlib.sha256(tail).hexdigest() == prior["tail"]:
                            offset = prior["offset"]
                            state = json.loads(prior["state_json"])
                stream.seek(offset)
                while not self.stopping.is_set():
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    if not EVENT_RE.match(line[:160]):
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = event["type"]
                    if kind == "thread.started":
                        state = {"thread": event.get("thread_id") or f"source:{key}", "turn": 0}
                        records[f"{state['thread']}:1"] = (None, False)
                        continue
                    if kind == "turn.started":
                        state["turn"] = state.get("turn", 0) + 1
                    identity = f"{state.get('thread', 'source:' + key)}:{max(1, state.get('turn', 1))}"
                    usage = token_usage(event.get("usage")) if kind == "turn.completed" else None
                    if identity not in records or usage is not None:
                        records[identity] = (usage, kind == "turn.completed")
                if not records and not state and stat.st_size:
                    records[f"source:{key}:1"] = (None, False)
                stream.seek(max(0, offset - 128))
                tail_digest = hashlib.sha256(stream.read(min(128, offset))).hexdigest()
        except OSError:
            return 0
        if self.stopping.is_set():
            return 0  # Do not mark an interrupted import as fully consumed.
        with self.store.connect() as db:
            for identity, (usage, completed) in records.items():
                db.execute("""INSERT INTO usage_invocations VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                    stage = CASE WHEN (excluded.quality >= quality AND stage NOT LIKE '%Repair%')
                        OR excluded.stage LIKE '%Repair%'
                        THEN excluded.stage ELSE stage END,
                    model = CASE WHEN excluded.model IS NOT NULL AND
                        (model IS NULL OR excluded.quality >= quality) THEN excluded.model ELSE model END,
                    tier = CASE WHEN excluded.tier IS NOT NULL AND
                        (tier IS NULL OR excluded.quality >= quality) THEN excluded.tier ELSE tier END,
                    quality = MAX(quality, excluded.quality),
                    usage_json = COALESCE(excluded.usage_json, usage_json),
                    completed = MAX(completed, excluded.completed)
                    """, (identity, context["stage"], context.get("model"), context.get("tier"),
                          context["quality"], json.dumps(usage) if usage is not None else None, int(completed)))
                db.executemany("INSERT OR IGNORE INTO usage_scopes VALUES (?, ?, ?)",
                               [(identity, kind, path) for kind, path in context["scopes"]])
            # New scopes (e.g. a job's installed artifact) apply to earlier turns
            # too when context changes; a reset above reparses the whole source.
            db.execute("INSERT OR REPLACE INTO usage_sources VALUES (?, ?, ?, ?, ?)",
                       (key, signature, offset, json.dumps(state), tail_digest))
        if known is not None:
            known[key] = {"signature": signature, "offset": offset,
                          "state_json": json.dumps(state), "tail": tail_digest}
        return 1

    def summary(self, targets):
        """All accounting on this path reads SQLite only."""
        selected = set()
        for target in targets:
            kind, value = target["kind"], target["path"]
            value = value if kind in {"job", "run"} else path_key(value)
            selected.add((kind, value))
        parameters = [value for pair in sorted(selected) for value in pair]
        with self.store.connect() as db:
            rows = db.execute("WITH selected(kind, path) AS (VALUES "
                + ",".join("(?,?)" for _ in selected) + """ )
                SELECT * FROM usage_invocations WHERE id IN (
                    SELECT invocation_id FROM usage_scopes JOIN selected USING (kind, path)
                )""", parameters).fetchall() if selected else []
        total = self._totals(rows)
        stages = defaultdict(list)
        for row in rows:
            stages[row["stage"]].append(row)
        pricing = []
        for model, tier in sorted({(row["model"], row["tier"]) for row in rows},
                                  key=lambda pair: (pair[0] or "", pair[1] or "")):
            rates = model_rates(model, tier)
            pricing.append({"model": model, "tier": tier,
                            "rates": dict(zip(("input", "cachedInput", "cacheWrite", "output"), rates))
                            if rates is not None else None})
        return {**total, "stages": [{"stage": stage, **self._totals(values)}
                                   for stage, values in sorted(stages.items())],
                "indexing": self.indexing, "indexError": self.error, "updatedAt": self.updated_at,
                "pricingVersion": PRICING_VERSION, "pricingUrl": PRICING_URL,
                "pricingRates": pricing,
                "assumptions": "API-equivalent token estimate at current short-context rates; "
                "30-minute cache writes; unreported cache writes use ordinary input rates. "
                "Excludes tool fees and long-context premiums. "
                "Subscription charges may differ. Reasoning is included in output."}

    @staticmethod
    def _totals(rows):
        counters = {field: 0 for field in TOKEN_FIELDS}
        available = {field: 0 for field in TOKEN_FIELDS}
        missing = unpriced = priced = 0
        cost = 0.0
        models = set()
        for row in rows:
            if row["model"]:
                models.add(f"{row['model']} · {row['tier'] or 'unknown tier'}")
            usage = json.loads(row["usage_json"]) if row["usage_json"] else None
            if usage is None:
                missing += 1
                continue
            for field in TOKEN_FIELDS:
                if field in usage:
                    counters[field] += usage[field]
                    available[field] += 1
            estimate = estimated_cost(usage, row["model"], row["tier"])
            if estimate is None:
                unpriced += 1
            else:
                cost += estimate
                priced += 1
        return {"tokens": counters, "reported": available, "invocations": len(rows),
                "missingUsage": missing, "unpriced": unpriced, "priced": priced,
                "estimatedCost": cost if priced else None, "models": sorted(models)}
