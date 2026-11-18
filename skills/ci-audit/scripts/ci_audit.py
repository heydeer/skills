#!/usr/bin/env python3
"""Measure where a repository's GitHub Actions time and money go.

Reads recent workflow runs through the `gh` CLI (read-only) and prints a
Markdown report:

  - how long a PR waits for its checks, and how that wait splits into
    waiting for a runner, setup, and real work
  - every job's queue / setup / work time, billable minutes and failure rate
  - the slowest steps
  - billable minutes and estimated cost by runner type, projected to a month
  - work that bought nothing: superseded runs that kept running, cancelled
    and failed runs, duplicate triggers, per-minute rounding, re-runs
  - cache hits and misses read from job logs
  - how many CI rounds each PR takes

Usage:
  python3 ci_audit.py                         # current repo, last 14 days
  python3 ci_audit.py --repo owner/name --days 30 --max-runs 500
  python3 ci_audit.py --price blacksmith-4vcpu=0.004 --json audit.json
  python3 ci_audit.py --budget-minutes 5      # exit code 3 when over budget
  python3 ci_audit.py --save-dir raw/         # keep the raw API data
  python3 ci_audit.py --from-dir raw/         # re-analyze without the API

Standard library only. Reads GitHub through the `gh` CLI when it is installed, and otherwise with the
token in GH_TOKEN or GITHUB_TOKEN; either needs read access to Actions.
It never writes to the repository or to GitHub.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

PR_EVENTS = {"pull_request", "pull_request_target"}
FIRST_WAVE_SECONDS = 180
FAILED = ("failure", "timed_out")
DECIDED = ("success",) + FAILED

# GitHub-hosted standard runners, USD per minute. Source:
# https://docs.github.com/en/billing/reference/actions-runner-pricing
# (read 2026-11). Larger runners and third-party runners carry labels the
# owner chose, so they can't be priced from the label: pass --price for those.
GITHUB_HOSTED_PRICES = [
    (r"^ubuntu-slim$", 0.002),
    (r"^ubuntu-[\w.]+-arm$", 0.005),
    (r"^ubuntu-(latest|\d+\.\d+)$", 0.006),
    (r"^windows-(latest|\d+)(-vs\d+)?(-arm)?$", 0.010),
    (r"^macos-(latest|\d+)-xlarge$", 0.102),
    (r"^macos-(latest|\d+)-large$", 0.077),
    (r"^macos-(latest|\d+)(-intel)?$", 0.062),
]

# What the same Linux minutes would cost elsewhere: public list prices in USD per minute by vCPU count,
# read from each provider's pricing page on the date in "prices_read" below. They change (tiers close to
# new customers, new providers undercut the table), so verify the cheapest candidates' pages before
# quoting, and pass --providers FILE (same shape, as JSON) for fresher numbers. Sizes a page doesn't
# list are scaled from the nearest listed size.
PROVIDER_PRICES = {
    "GitHub-hosted": {
        "url": "https://docs.github.com/en/billing/reference/actions-runner-pricing",
        "x64": {2: 0.006, 4: 0.012, 8: 0.022, 16: 0.042, 32: 0.082, 64: 0.162},
        "arm": {2: 0.005, 4: 0.008, 8: 0.014, 16: 0.026, 32: 0.050, 64: 0.098},
        "windows": {2: 0.010, 4: 0.022, 8: 0.042, 16: 0.082, 32: 0.162, 64: 0.322},
        "macos": {"standard": 0.062, "xlarge": 0.102, "large": 0.077},
        "note": "each job rounded up to a minute"},
    "Depot": {
        "url": "https://depot.dev/pricing", "x64": {2: 0.006},
        "note": "plans from $20/mo with 2,000 minutes included; only the 2 vCPU rate is listed"},
    "Blacksmith": {
        "url": "https://www.blacksmith.sh/pricing",
        "x64": {2: 0.004, 4: 0.008, 8: 0.016, 16: 0.032},
        "arm": {2: 0.0025, 4: 0.004, 8: 0.007, 16: 0.013},
        "windows": {2: 0.008}, "macos": {"xlarge": 0.08},
        "note": "3,000 free minutes a month; sizes beyond the base follow their documented "
                "half-of-GitHub-per-minute rule; macOS is a 6 vCPU M4"},
    "WarpBuild": {
        "url": "https://www.warpbuild.com/pricing",
        "x64": {2: 0.004, 4: 0.008, 8: 0.016, 16: 0.032, 32: 0.064},
        "arm": {2: 0.003, 4: 0.006, 8: 0.012, 16: 0.024, 32: 0.048},
        "windows": {4: 0.016, 8: 0.032, 16: 0.064, 32: 0.128}, "macos": {"xlarge": 0.08, "large": 0.16},
        "note": "cache storage and operations billed separately; macOS is 6 or 12 vCPU"},
    "Namespace": {
        "url": "https://namespace.so/pricing", "x64": {2: 0.002, 4: 0.008, 8: 0.016},
        "macos": {"standard": 0.04, "xlarge": 0.06},
        "note": "prepaid rates on plans from $100/mo; the 2 vCPU shape has 4 GB of memory"},
    "Ubicloud": {
        "url": "https://www.ubicloud.com/use-cases/github-actions",
        "x64": {2: 0.002, 4: 0.004, 8: 0.008, 16: 0.016},
        "note": "premium runners (standard closed to new customers since 2026-05); 1,250 free minutes a month"},
    "Sengi": {
        "url": "https://sengi.run/pricing", "x64": {2: 0.001, 4: 0.002, 8: 0.004},
        "note": "Basic tier; rounds each job up to a minute; 3,000 free base minutes a month; performance untested"},
    "RunsOn (your AWS account, spot)": {
        "url": "https://runs-on.com/pricing/", "self_run": True,
        "x64": {2: 0.0009, 4: 0.0013, 8: 0.0024, 16: 0.0034},
        "arm": {2: 0.0009, 4: 0.0016, 8: 0.0029, 16: 0.0043},
        "note": "plus a licence from EUR 300 a year and about $7/mo of AWS control plane; you run it"},
}

SLIM_RATE = 0.002   # GitHub's 1-vCPU Linux runner (ubuntu-slim), same pricing page

INSTALL_STEP = re.compile(r"\binstall\b|\bnpm ci\b|\b(pnpm|yarn|bun) i\b|\buv sync\b|\bgo mod download\b|\bcargo fetch\b"
                          r"|\bbundle\b|node-setup|dependencies", re.I)

# Label fragments that identify who runs the machine. Used for display only.
PROVIDERS = [
    ("blacksmith", "Blacksmith"), ("depot", "Depot"), ("namespace", "Namespace"),
    ("nscloud", "Namespace"), ("warp", "WarpBuild"), ("ubicloud", "Ubicloud"),
    ("buildjet", "BuildJet"), ("runs-on", "RunsOn"), ("self-hosted", "self-hosted"),
]

# A step counts as setup when its name matches one of these. Everything else
# is work. It is a heuristic over step names; the report lists every slow step
# with its class so a wrong guess is visible.
SETUP_PATTERNS = re.compile(
    r"^set up job$|^complete job$|^post |^initialize containers$|^stop containers$"
    r"|^pull .*image|^build .*@|checkout|\bset ?up\b|\bsetup-|\binstall\b|\brestore\b"
    r"|\bcache\b|download.artifact|\blog ?in\b|\bconfigure\b|\bauthenticate\b|\bbootstrap\b"
    r"|\bnpm ci\b|\b(pnpm|yarn|bun) i\b|\buv sync\b|\bgo mod download\b|\bcargo fetch\b",
    re.I,
)
CHEAP_CHECK = re.compile(r"lint|format|fmt|prettier|biome|eslint|typecheck|type.check|tsc|ruff|mypy|clippy|vet", re.I)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")   # job logs carry color codes that break line matching

CACHE_HIT = re.compile(r"Cache restored from key|Restored from cache key|Cache hit for(?: restore-key)?:")
CACHE_MISS = re.compile(r"[Cc]ache (?:is )?not found|No cache found")
TURBO_SUMMARY = re.compile(r"Cached:\s+(\d+) cached, (\d+) total")
NX_SUMMARY = re.compile(r"read the output from the cache instead of running the command for (\d+) out of (\d+) tasks")


# ---------------------------------------------------------------- helpers

def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds(start, end):
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def fmt(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def share(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "-"


def money(value):
    return f"${value:,.2f}" if value < 100 else f"${value:,.0f}"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def group_name(job_name):
    """Collapse matrix legs and shards into one job: 'test (ubuntu, 20)' -> 'test'."""
    name = re.sub(r"\s*\([^()]*\)\s*$", "", job_name or "?")
    name = re.sub(r"\s*\([^()]*$", "", name)   # a long leg list is truncated mid-parenthesis
    return re.sub(r"\s+\d+\s*/\s*\d+$", "", name) or job_name


# ---------------------------------------------------------------- data source

class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Log downloads redirect to a signed storage URL, which rejects a request that still carries the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
        return new


NO_ACCESS = ("This script reads GitHub through the `gh` CLI or, without it, a token. Neither is available here.\n"
             "  - Install gh (https://cli.github.com) and run `gh auth login`, or\n"
             "  - set GH_TOKEN (or GITHUB_TOKEN) to a token that can read Actions on this repository.\n"
             "To analyze data exported elsewhere with --save-dir, use --from-dir instead.")


class Source:
    """Reads from the GitHub API through `gh` or a token, or from a directory saved earlier."""

    def __init__(self, repo, from_dir=None, save_dir=None):
        self.repo, self.from_dir, self.save_dir = repo, from_dir, save_dir
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.use_gh = bool(shutil.which("gh"))
        if self.use_gh and not self.token and not from_dir:
            # raw log bodies carry terminal escape sequences, which `gh api` refuses to print;
            # with the CLI's own token they can be fetched over plain HTTP instead
            proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            if proc.returncode == 0:
                self.token = proc.stdout.strip() or None
        self.log_errors = []
        self.opener = urllib.request.build_opener(_DropAuthOnRedirect)
        self.requests = self.reused = 0
        self.rate_limited = False
        if save_dir:
            for sub in ("jobs", "logs"):
                os.makedirs(os.path.join(save_dir, sub), exist_ok=True)

    def _gh(self, path, params=None, raw=False):
        url = path + ("?" + urlencode(params) if params else "")
        if self.rate_limited:   # every further request would fail the same way and count against the limit
            raise RuntimeError("rate limit reached")
        for attempt in range(4):   # network blips are common over hundreds of requests
            self.requests += 1
            text, error = self._call(url)
            if error is None:
                break
            if re.search(r"secondary rate limit", error, re.I) and attempt < 3:
                time.sleep(30 * (attempt + 1))   # too many requests at once: GitHub lifts this within a minute or so
                continue
            if re.search(r"rate limit", error, re.I):
                self.rate_limited = True
            if self.rate_limited or re.search(r"HTTP 40[134]|HTTP 410|HTTP 422", error) or attempt == 3:
                raise RuntimeError(error)
            time.sleep(2 ** attempt)
        return text if raw else json.loads(text)

    def _call(self, url):
        """One API request. Returns (text, None) or (None, what went wrong)."""
        if self.use_gh:
            proc = subprocess.run(["gh", "api", url], capture_output=True)
            if proc.returncode == 0:
                return proc.stdout.decode("utf-8", "replace"), None
            return None, proc.stderr.decode("utf-8", "replace").strip() or f"gh api {url} failed"
        if not self.token:
            sys.exit(NO_ACCESS)
        return self._http(url)

    def _http(self, url):
        base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        request = urllib.request.Request(f"{base}/{url}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ci-audit"})
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as error:
            return None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            return None, str(getattr(error, "reason", error))

    def _read(self, *parts):
        path = os.path.join(self.from_dir, *parts)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def _save(self, text, *parts):
        if self.save_dir:
            with open(os.path.join(self.save_dir, *parts), "w", encoding="utf-8") as handle:
                handle.write(text)

    def quota(self):
        """(requests left, limit, reset time) for this token, or None. Asking is free: it uses none of the limit."""
        try:
            text, error = self._call("rate_limit")
            core = json.loads(text)["resources"]["core"]
            return core["remaining"], core["limit"], datetime.fromtimestamp(core["reset"], timezone.utc)
        except (TypeError, KeyError, ValueError):   # GitHub Enterprise Server can have rate limits turned off
            return None

    def repo_info(self):
        if self.from_dir:
            return json.loads(self._read("repo.json") or "{}")
        info = self._gh(f"repos/{self.repo}")
        info = {k: info.get(k) for k in ("full_name", "private", "default_branch")}
        self._save(json.dumps(info), "repo.json")
        return info

    def runs(self, since, max_runs, event=None, branch=None):
        if self.from_dir:
            return json.loads(self._read("runs.json") or "[]")
        runs, seen = [], set()
        stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")

        def read(start, end):
            """Newest runs created in this span. True when the API's 1,000-result cap hid older ones."""
            for page in range(1, 11):
                if len(runs) >= max_runs:
                    return False
                params = {"per_page": 100, "page": page, "status": "completed",
                          "created": f"{stamp(start)}..{stamp(end)}"}
                if event:
                    params["event"] = event
                if branch:
                    params["branch"] = branch
                batch = self._gh(f"repos/{self.repo}/actions/runs", params).get("workflow_runs", [])
                for run in batch:
                    if run["id"] not in seen:
                        seen.add(run["id"])
                        runs.append(run)
                if len(batch) < 100:
                    return False
            return True

        day_end = datetime.now(timezone.utc) + timedelta(minutes=5)
        if read(since, day_end):   # one query covers most repositories; a busy one continues a day at a time
            day_end = min(parse_ts(r["created_at"]) for r in runs) + timedelta(seconds=1)
            while len(runs) < max_runs and day_end > since:
                day_start = max(since, day_end - timedelta(days=1))
                read(day_start, day_end)
                day_end = day_start
        runs = runs[:max_runs]
        self._save(json.dumps(runs), "runs.json")
        return runs

    def jobs(self, run):
        run_id = run["id"]
        if run.get("conclusion") == "skipped":   # every job of a skipped run was skipped: no time, no minutes
            return []
        if self.from_dir:
            text = self._read("jobs", f"{run_id}.json")
            if text is None:   # the saving pass could not read this run; keep it out, don't fake an empty one
                raise RuntimeError("jobs/<run>.json missing from --from-dir (unread when saved)")
            return json.loads(text)
        if self.save_dir:   # a run saved by an earlier pass is not fetched again, unless it was re-run since
            saved = os.path.join(self.save_dir, "jobs", f"{run_id}.json")
            if os.path.exists(saved):
                with open(saved, encoding="utf-8") as handle:
                    jobs = json.load(handle)
                if all((j.get("run_attempt") or 1) == (run.get("run_attempt") or 1) for j in jobs):
                    self.reused += 1
                    return jobs
        jobs, page = [], 1
        while True:
            data = self._gh(f"repos/{self.repo}/actions/runs/{run_id}/jobs",
                            {"per_page": 100, "page": page, "filter": "latest"})
            jobs.extend(data.get("jobs", []))
            if len(jobs) >= data.get("total_count", 0) or not data.get("jobs"):
                break
            page += 1
        self._save(json.dumps(jobs), "jobs", f"{run_id}.json")
        return jobs

    def attempt_jobs(self, run_id, attempt):
        """Jobs of one earlier attempt of a re-run run, or None when they can't be read."""
        name = f"{run_id}.attempt{attempt}.json"
        for folder in (self.from_dir, self.save_dir):
            if folder and os.path.exists(os.path.join(folder, "jobs", name)):
                with open(os.path.join(folder, "jobs", name), encoding="utf-8") as handle:
                    return json.load(handle)
        if self.from_dir:
            return None
        try:
            jobs = self._gh(f"repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", {"per_page": 100}).get("jobs", [])
        except RuntimeError:
            return None
        self._save(json.dumps(jobs), "jobs", name)
        return jobs

    def log(self, job_id):
        if self.from_dir:
            return self._read("logs", f"{job_id}.txt")
        if self.save_dir and os.path.exists(os.path.join(self.save_dir, "logs", f"{job_id}.txt")):
            with open(os.path.join(self.save_dir, "logs", f"{job_id}.txt"), encoding="utf-8", errors="replace") as handle:
                return handle.read()
        path = f"repos/{self.repo}/actions/jobs/{job_id}/logs"
        self.requests += 1
        if self.token:
            # not through `gh api`: it refuses log bodies ("the response contains terminal escape sequences")
            text, error = self._http(path)
        else:
            proc = subprocess.run(["gh", "api", "--allow-escape-sequences", path], capture_output=True)
            text = proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None
            error = None if text is not None else proc.stderr.decode("utf-8", "replace").strip()
        if text is None:
            self.log_errors.append((error or "?").splitlines()[-1][:160])  # logs expire, and some tokens can't read them
            return None
        self._save(text, "logs", f"{job_id}.txt")
        return text

    def branch_rules(self, branch):
        """Required status-check contexts for a branch (rulesets plus classic protection), or None when unreadable."""
        if self.from_dir:
            text = self._read("rules.json")
            return json.loads(text) if text else None
        contexts, readable = set(), False
        try:
            rules = self._gh(f"repos/{self.repo}/rules/branches/{branch}")
            readable = True
            for rule in rules:
                if rule.get("type") == "required_status_checks":
                    for check in (rule.get("parameters") or {}).get("required_status_checks", []):
                        contexts.add(check.get("context"))
        except RuntimeError:
            pass
        try:
            classic = self._gh(f"repos/{self.repo}/branches/{branch}/protection/required_status_checks")
            readable = True
            contexts.update(classic.get("contexts") or [])
        except RuntimeError:
            pass  # 404 when the branch has no classic protection, 403 without admin read
        if not readable:
            return None
        contexts = sorted(c for c in contexts if c)
        self._save(json.dumps(contexts), "rules.json")
        return contexts


# ---------------------------------------------------------------- pricing

def runner_label(job):
    labels = job.get("labels") or []
    return ",".join(labels) if labels else "(unknown)"


def provider_of(label):
    low = label.lower()
    for fragment, name in PROVIDERS:
        if fragment in low:
            return name
    if any(re.match(pattern, low) for pattern, _ in GITHUB_HOSTED_PRICES):
        return "GitHub-hosted"
    return "unknown (larger or custom runner)"


def shape_of(label, public_repo):
    """(family, arch, size) of a runner read from its labels, or None when it can't be told.

    Size is the vCPU count on Linux and Windows. macOS machines differ too much between providers to
    compare by cores, so there it is a class: standard, xlarge (about 6 cores) or large (about 12).
    """
    low = label.lower()
    if "self-hosted" in low or "ubuntu-slim" in low:
        return None
    arch = "arm" if re.search(r"arm|aarch64|graviton", low) else "x64"
    vcpu = None
    for part in low.split(","):
        match = re.search(r"(\d+)-?v?cpu|(\d+)-?cores?\b|standard-(\d+)|-(\d+)x$|ubuntu-\d+\.\d+-(\d+)$", part)
        if match:
            vcpu = int(next(g for g in match.groups() if g))
            break
    if "macos" in low:
        if vcpu:
            return "macos", "arm", "standard" if vcpu <= 4 else "xlarge" if vcpu <= 8 else "large"
        return "macos", "arm", "xlarge" if "xlarge" in low else "large" if "large" in low else "standard"
    if "windows" in low:
        return "windows", arch, vcpu or 2
    if vcpu:
        return "linux", arch, vcpu
    if any(re.match(r"^ubuntu-(latest|\d+\.\d+)(-arm)?$", part) for part in low.split(",")):
        return "linux", arch, 4 if public_repo else 2
    return None


def provider_rate(prices, shape):
    """USD per minute for this shape at a provider. (rate or None, whether the page lists that exact size)"""
    family, arch, size = shape
    if family == "macos":
        classes, order = prices.get("macos") or {}, ["standard", "xlarge", "large"]
        for cls in order[order.index(size):]:   # the next size up when a provider doesn't sell this one
            if cls in classes:
                return classes[cls], cls == size
        return None, False
    sizes = prices.get("windows" if family == "windows" else arch)
    if not sizes:
        return None, False
    sizes = {int(k): v for k, v in sizes.items()}
    if size in sizes:
        return sizes[size], True
    if size < min(sizes):
        return sizes[min(sizes)], False   # the smallest machine on offer
    base = max(k for k in sizes if k <= size)
    return sizes[base] * size / base, False


def price_of(label, overrides, default_price, public_repo):
    """Return (USD per minute or None, note)."""
    for fragment, rate in overrides:
        if fragment.lower() in label.lower():
            return rate, "from --price"
    for part in label.lower().split(","):
        for pattern, rate in GITHUB_HOSTED_PRICES:
            if re.match(pattern, part):
                if public_repo:
                    return 0.0, "free: standard runner on a public repo"
                return rate, "GitHub list price"
    if default_price is not None:
        return default_price, "from --default-price"
    return None, "unpriced: pass --price"


# ---------------------------------------------------------------- analysis

def job_record(raw_job):
    started, done = parse_ts(raw_job.get("started_at")), parse_ts(raw_job.get("completed_at"))
    job_created = parse_ts(raw_job.get("created_at")) or started
    ran = raw_job.get("conclusion") not in (None, "skipped") and started and done
    setup = work = 0.0
    steps = []
    for step in raw_job.get("steps") or []:
        dur = seconds(parse_ts(step.get("started_at")), parse_ts(step.get("completed_at")))
        if dur is None or step.get("conclusion") == "skipped":
            continue
        is_setup = bool(SETUP_PATTERNS.search(step.get("name") or ""))
        setup, work = (setup + dur, work) if is_setup else (setup, work + dur)
        steps.append({"name": step.get("name") or "?", "seconds": dur, "setup": is_setup,
                      "conclusion": step.get("conclusion")})
    run_seconds = seconds(started, done) if ran else None
    return {
        "id": raw_job.get("id"),
        "name": raw_job.get("name") or "?",
        "group": group_name(raw_job.get("name")),
        "conclusion": raw_job.get("conclusion") or "?",
        "created": job_created, "started": started, "done": done,
        "ran": bool(ran),
        "queue": seconds(job_created, started) if ran else None,
        "run_seconds": run_seconds,
        "setup": setup if ran else None,
        "work": work if ran else None,
        "billed_min": math.ceil(run_seconds / 60) if run_seconds else 0,
        "label": runner_label(raw_job),
        "steps": steps,
    }


def build(runs_raw, jobs_by_run):
    """Turn API objects into flat run and job records with the timings we need."""
    runs = []
    for raw in runs_raw:
        created = parse_ts(raw.get("run_started_at")) or parse_ts(raw.get("created_at"))
        if created is None:
            continue
        head_repo = (raw.get("head_repository") or {}).get("full_name") or ""
        prs = raw.get("pull_requests") or []
        run = {
            "id": raw["id"],
            "workflow": raw.get("name") or raw.get("path") or "?",
            "workflow_id": raw.get("workflow_id"),
            "event": raw.get("event") or "?",
            "conclusion": raw.get("conclusion") or "?",
            "attempt": raw.get("run_attempt") or 1,
            "sha": raw.get("head_sha") or "",
            "branch": f"{head_repo}:{raw.get('head_branch') or '?'}",
            "branch_short": raw.get("head_branch") or "?",
            "pr": prs[0].get("number") if prs else None,
            "actor": ((raw.get("triggering_actor") or raw.get("actor") or {}).get("login")) or None,
            "created": created,
            "url": raw.get("html_url") or "",
            "jobs": [],
        }
        run["jobs"] = [job_record(raw_job) for raw_job in jobs_by_run.get(raw["id"], [])]
        ends = [j["done"] for j in run["jobs"] if j["done"]]
        run["end"] = max(ends) if ends else parse_ts(raw.get("updated_at")) or created
        run["wall"] = seconds(created, run["end"])
        run["billed_min"] = sum(j["billed_min"] for j in run["jobs"])
        runs.append(run)
    names = defaultdict(Counter)
    for raw in runs_raw:
        names[raw.get("workflow_id")][raw.get("name") or "?"] += 1
    paths = {raw.get("workflow_id"): os.path.basename(raw.get("path") or "") for raw in runs_raw}
    for run in runs:   # a workflow whose name changes per run (run-name with a SHA) is shown by its file
        if len(names[run["workflow_id"]]) > 1 and paths.get(run["workflow_id"]):
            run["workflow"] = paths[run["workflow_id"]]
    runs.sort(key=lambda r: r["created"])
    return runs


def analyze(runs, info, args, window=None, required=None):
    public = info.get("private") is False
    required_set = set(required or ())
    overrides = []
    for item in args.price or []:
        fragment, _, rate = item.rpartition("=")
        overrides.append((fragment, float(rate)))

    def cost_of(job):
        rate, _ = price_of(job["label"], overrides, args.default_price, public)
        return None if rate is None else rate * job["billed_min"]

    def total_cost(items):
        costs = [r["cost"] for r in items]
        return None if any(c is None for c in costs) else sum(costs)

    for r in runs:
        job_costs = [cost_of(j) for j in r["jobs"] if j["ran"]]
        r["cost"] = None if any(c is None for c in job_costs) else sum(job_costs)
    default_branch = info.get("default_branch")
    first, last = window or (runs[0]["created"], max(r["end"] for r in runs))
    span_days = max((last - first).total_seconds() / 86400, 1 / 24)
    total_billed = sum(r["billed_min"] for r in runs)
    result = {
        "repo": info.get("full_name") or args.repo, "public": public,
        "window": {"from": first.isoformat(), "to": last.isoformat(), "days": round(span_days, 2)},
        "runs": len(runs), "billed_minutes": total_billed,
        "signals": [],
    }
    signals = result["signals"]

    # --- events and workflows
    by_event = defaultdict(lambda: {"runs": 0, "billed": 0})
    for r in runs:
        by_event[r["event"]]["runs"] += 1
        by_event[r["event"]]["billed"] += r["billed_min"]
    result["events"] = dict(by_event)

    workflows = defaultdict(list)
    for r in runs:
        event = r["event"]
        if event == "push" and default_branch:
            event = "push (default branch)" if r["branch_short"] == default_branch else "push (other branches)"
        workflows[(r["workflow"], event)].append(r)
    result["workflows"] = []
    for (name, event), items in workflows.items():
        clean = [r for r in items if r["conclusion"] in DECIDED and r["attempt"] == 1]
        decided = [r for r in items if r["conclusion"] in DECIDED]
        result["workflows"].append({
            "workflow": name, "event": event, "runs": len(items),
            "p50": pct([r["wall"] for r in clean], .5), "p90": pct([r["wall"] for r in clean], .9),
            "fail_rate": (sum(r["conclusion"] in FAILED for r in decided) / len(decided)) if decided else None,
            "billed": sum(r["billed_min"] for r in items),
        })
    result["workflows"].sort(key=lambda w: -w["billed"])

    # --- PR feedback time: one commit = every PR-event run on that sha
    pr_runs = [r for r in runs if r["event"] in PR_EVENTS]
    commits = defaultdict(list)
    for r in pr_runs:
        commits[(r["branch"], r["sha"])].append(r)
    commit_rows = []
    for (_, sha), every in commits.items():
        start = min(r["created"] for r in every)
        # only the runs the push itself started; later ones come from labels, reviews, or closing the PR
        items = [r for r in every if seconds(start, r["created"]) <= FIRST_WAVE_SECONDS]
        ran_jobs = [j for r in items for j in r["jobs"] if j["ran"]]
        conclusions = {r["conclusion"] for r in items}
        outcome = "cancelled" if "cancelled" in conclusions else "failed" if conclusions & set(FAILED) else \
            "passed" if conclusions <= {"success", "skipped", "neutral"} else "other"
        row = {"branch": items[0]["branch_short"], "pr": items[0]["pr"], "sha": sha[:12], "actor": items[0]["actor"],
               "pushed": start, "outcome": outcome, "rerun": any(r["attempt"] > 1 for r in items),
               "billed": sum(r["billed_min"] for r in every), "cost": total_cost(every),
               "wall": seconds(start, max(r["end"] for r in items)) if ran_jobs else None}
        if ran_jobs:
            # the job that finishes last is what the author actually waits for
            last_job = max(ran_jobs, key=lambda j: j["done"])
            row.update(before=seconds(start, last_job["created"]), queue=last_job["queue"],
                       setup=last_job["setup"], work=last_job["work"],
                       longest_job=max((j["queue"] or 0) + j["run_seconds"] for j in ran_jobs))
        bad = [j["done"] for r in items for j in r["jobs"] if j["conclusion"] in FAILED and j["done"]]
        row["first_failure"] = seconds(start, min(bad)) if bad else None
        if required_set:
            # the merge gate: only the jobs branch protection requires (contexts match check-run names)
            req = [j for r in items for j in r["jobs"]
                   if j["ran"] and (j["name"] in required_set or j["group"] in required_set)]
            if req:
                last_req = max(req, key=lambda j: j["done"])
                row["required_wall"] = seconds(start, last_req["done"])
                row["last_required"] = last_req["group"]
        commit_rows.append(row)
    commit_rows.sort(key=lambda c: c["pushed"])
    result["commits"] = commit_rows
    timed = [c for c in commit_rows if c["wall"] is not None and not c["rerun"] and c["outcome"] in ("passed", "failed")]
    passing = [c for c in timed if c["outcome"] == "passed"]
    failing = [c for c in timed if c["outcome"] == "failed"]
    basis = passing or timed
    walls = lambda rows: [c["wall"] for c in rows]
    result["pr_feedback"] = {
        "commits": len(timed), "p50": pct(walls(timed), .5), "p90": pct(walls(timed), .9),
        "passing": {"commits": len(passing), "p50": pct(walls(passing), .5), "p90": pct(walls(passing), .9)},
        "failing": {"commits": len(failing), "first_failure_p50": pct([c["first_failure"] for c in failing], .5)},
        "split_basis": "passing" if passing else "all",
    }
    headline = result["pr_feedback"]["passing"]["p50"] or result["pr_feedback"]["p50"]
    result["pr_feedback"]["headline_p50"] = headline
    parts = ("before", "queue", "setup", "work")
    shares = defaultdict(list)
    for c in basis:
        whole = sum(c.get(k) or 0 for k in parts)
        if whole:
            for k in parts:
                shares[k].append((c.get(k) or 0) / whole)
    result["pr_feedback"]["split_mean"] = {
        k: (sum(shares[k]) / len(shares[k]) * headline) if shares[k] and headline else None for k in parts}
    if required_set:
        req_pass = [c for c in passing if c.get("required_wall") is not None]
        result["pr_feedback"]["required"] = {
            "contexts": sorted(required_set), "commits": len(req_pass),
            "p50": pct([c["required_wall"] for c in req_pass], .5),
            "p90": pct([c["required_wall"] for c in req_pass], .9),
            "last": Counter(c["last_required"] for c in req_pass).most_common(3),
        }

    # --- duplicate work. Only steps GitHub named by their literal command ("Run pnpm lint") are compared
    # across jobs: a human-chosen name, a local composite ("Run ./…") or a marketplace action can hide
    # different inputs behind one name. Inside one job, any repeated step name counts.
    def command_step(step):
        name = step["name"]
        return (step["seconds"] >= 2 and not step["setup"] and name.startswith("Run ")
                and not name.startswith("Run ./") and not re.match(r"Run [\w.-]+/[\w.-]+@", name))

    dup_across = defaultdict(lambda: {"runs": 0, "seconds": 0.0, "groups": set()})
    dup_jobs = defaultdict(lambda: {"jobs": 0, "minutes": 0})
    repeats = defaultdict(lambda: {"jobs": 0, "seconds": 0.0})
    for r in runs:
        ran = [j for j in r["jobs"] if j["ran"]]
        for j in ran:
            counts = Counter(s["name"] for s in j["steps"] if s["seconds"] >= 5)
            for name, count in counts.items():
                if count > 1:
                    same = [s for s in j["steps"] if s["name"] == name]
                    extra = sum(sorted((s["seconds"] for s in same), reverse=True)[1:])
                    entry = repeats[(r["workflow"], j["group"], name)]
                    entry["jobs"] += 1
                    entry["seconds"] += extra
                    # only a repeated setup step is priced: a repeated deploy or build composite
                    # usually carries different inputs each time (several targets in one job)
                    if same[0]["setup"]:
                        j["repeat_step_minutes"] = j.get("repeat_step_minutes", 0.0) + extra / 60
        groups_of = defaultdict(set)
        for j in ran:
            for s in j["steps"]:
                if command_step(s):
                    groups_of[s["name"]].add(j["group"])
        for name, in_groups in groups_of.items():
            if len(in_groups) > 1:
                entry = dup_across[(r["workflow"], name)]
                entry["runs"] += 1
                entry["groups"].update(in_groups)
                times = sorted((s["seconds"] for j in ran for s in j["steps"]
                                if s["name"] == name and command_step(s)), reverse=True)
                entry["seconds"] += sum(times[1:])
        # a job whose every command also ran in a bigger job of the same run is a fold candidate;
        # "bigger" keeps one copy of mutually-identical jobs out of the count
        work_of = {j["id"]: sum(s["seconds"] for s in j["steps"] if command_step(s)) for j in ran}
        for j in ran:
            work = [s for s in j["steps"] if command_step(s)]
            bigger = {s["name"] for other in ran
                      if other["group"] != j["group"]   # matrix siblings run the same commands on purpose
                      and (work_of[other["id"]], other["group"]) > (work_of[j["id"]], j["group"])
                      for s in other["steps"] if command_step(s)}
            if work and all(s["name"] in bigger for s in work):
                j["duplicate_work"] = True
                entry = dup_jobs[(r["workflow"], j["group"])]
                entry["jobs"] += 1
                entry["minutes"] += j["billed_min"]
    dup_rows = []
    for (workflow, group), v in dup_jobs.items():
        if v["jobs"] >= 3:
            dup_rows.append({"kind": "whole job", "workflow": workflow, "what": group, "runs": v["jobs"],
                             "minutes": round(v["minutes"]),
                             "note": "every command it ran also ran in a bigger job of the same run"})
    for (workflow, name), v in dup_across.items():
        if v["runs"] >= 3 and v["seconds"] >= 60:
            dup_rows.append({"kind": "across jobs", "workflow": workflow, "what": name, "runs": v["runs"],
                             "minutes": round(v["seconds"] / 60),
                             "note": "also in " + ", ".join(sorted(v["groups"])[:4])})
    for (workflow, group, name), v in repeats.items():
        if v["jobs"] >= 3 and v["seconds"] >= 60:
            dup_rows.append({"kind": "repeated in one job", "workflow": workflow, "what": f"{group}: {name}",
                             "runs": v["jobs"], "minutes": round(v["seconds"] / 60),
                             "note": "the same step runs more than once in this job"})
    dup_rows.sort(key=lambda d: -d["minutes"])
    result["duplicates"] = dup_rows[:12]

    # --- per job
    groups = defaultdict(list)
    for r in runs:
        for j in r["jobs"]:
            if j["ran"]:
                groups[(r["workflow"], j["group"])].append((r, j))
    job_rows, matrix_rows = [], []
    for (workflow, group), items in groups.items():
        jobs = [j for _, j in items]
        ok = [j for j in jobs if j["conclusion"] == "success"]
        decided = [j for j in jobs if j["conclusion"] in DECIDED]
        costs = [cost_of(j) for j in jobs]
        per_run = Counter(r["id"] for r, _ in items)
        stages = defaultdict(set)   # a matrix job failed in a run if any of its legs did
        for r, j in items:
            stages[r["id"]].add(j["conclusion"])
        stage_decided = [c for c in stages.values() if c & set(DECIDED)]
        job_rows.append({
            "workflow": workflow, "job": group, "jobs": len(jobs),
            "legs_per_run": round(sum(per_run.values()) / len(per_run), 1),
            "queue_p50": pct([j["queue"] for j in jobs], .5), "queue_p90": pct([j["queue"] for j in jobs], .9),
            "setup_p50": pct([j["setup"] for j in ok or jobs], .5),
            "work_p50": pct([j["work"] for j in ok or jobs], .5),
            "run_p50": pct([j["run_seconds"] for j in ok or jobs], .5),
            "run_p90": pct([j["run_seconds"] for j in ok or jobs], .9),
            "run_max": max(j["run_seconds"] for j in jobs),
            "billed": sum(j["billed_min"] for j in jobs),
            "cost": None if any(c is None for c in costs) else sum(costs),
            "fail_rate": (sum(bool(c & set(FAILED)) for c in stage_decided) / len(stage_decided)) if stage_decided else None,
            "label": " + ".join(label for label, _ in Counter(j["label"] for j in jobs).most_common(3)),
        })
        legs = defaultdict(list)
        for j in jobs:
            legs[j["name"]].append(j)
        # a leg that fails while every sibling passes is the only evidence that leg is needed
        alone = Counter()
        by_run = defaultdict(list)
        for r, j in items:
            by_run[r["id"]].append(j)
        for siblings in by_run.values():
            bad = [j for j in siblings if j["conclusion"] in FAILED]
            if len(bad) == 1 and len(siblings) > 1 and all(j["conclusion"] == "success" for j in siblings if j is not bad[0]):
                alone[bad[0]["name"]] += 1
        if len(legs) > 1:
            for name, leg_jobs in legs.items():
                leg_costs = [cost_of(j) for j in leg_jobs]
                matrix_rows.append({
                    "workflow": workflow, "job": group, "leg": name, "jobs": len(leg_jobs),
                    "queue_p50": pct([j["queue"] for j in leg_jobs], .5),
                    "queue_p90": pct([j["queue"] for j in leg_jobs], .9),
                    "run_p50": pct([j["run_seconds"] for j in leg_jobs], .5),
                    "billed": sum(j["billed_min"] for j in leg_jobs),
                    "cost": None if any(c is None for c in leg_costs) else sum(leg_costs),
                    "failures": sum(j["conclusion"] in FAILED for j in leg_jobs),
                    "failed_alone": alone[name],
                    "label": leg_jobs[0]["label"],
                })
    job_rows.sort(key=lambda row: -row["billed"])
    result["jobs"] = job_rows
    heavy = {(row["workflow"], row["job"]) for row in job_rows[:6]}
    result["matrix"] = sorted((m for m in matrix_rows if (m["workflow"], m["job"]) in heavy),
                              key=lambda m: -m["billed"])[:24]

    # --- slowest steps
    step_times = defaultdict(list)
    step_class = {}
    for (workflow, group), items in groups.items():
        for _, j in items:
            if j["conclusion"] != "success":
                continue
            for s in j["steps"]:
                step_times[(workflow, group, s["name"])].append(s["seconds"])
                step_class[(workflow, group, s["name"])] = "setup" if s["setup"] else "work"
    steps = [{"workflow": k[0], "job": k[1], "step": k[2], "class": step_class[k], "n": len(v),
              "p50": pct(v, .5), "p90": pct(v, .9), "total": sum(v)} for k, v in step_times.items()]
    steps.sort(key=lambda s: -s["total"])
    result["steps"] = steps[:20]

    # --- runners and cost
    by_label = defaultdict(lambda: {"jobs": 0, "billed": 0, "raw_seconds": 0.0})
    for r in runs:
        for j in r["jobs"]:
            if j["ran"]:
                entry = by_label[j["label"]]
                entry["jobs"] += 1
                entry["billed"] += j["billed_min"]
                entry["raw_seconds"] += j["run_seconds"]
    runners, priced_cost, priced_raw_cost, unpriced_min = [], 0.0, 0.0, 0
    for label, entry in sorted(by_label.items(), key=lambda kv: -kv[1]["billed"]):
        rate, note = price_of(label, overrides, args.default_price, public)
        cost = None if rate is None else rate * entry["billed"]
        if cost is None:
            unpriced_min += entry["billed"]
        else:
            priced_cost += cost
            priced_raw_cost += rate * entry["raw_seconds"] / 60
        runners.append({"label": label, "provider": provider_of(label), "jobs": entry["jobs"],
                        "billed": entry["billed"], "raw_minutes": round(entry["raw_seconds"] / 60, 1),
                        "rate": rate, "cost": cost, "note": note})
    monthly = 30 / span_days
    result["runners"] = runners
    result["cost"] = {
        "window": priced_cost, "monthly": priced_cost * monthly,
        "window_raw": priced_raw_cost, "monthly_raw": priced_raw_cost * monthly,
        "billed_minutes_monthly": total_billed * monthly, "unpriced_minutes": unpriced_min,
    }

    # --- work that bought nothing
    raw_minutes = sum(j["run_seconds"] for r in runs for j in r["jobs"] if j["ran"]) / 60
    cancelled = [r for r in runs if r["conclusion"] == "cancelled"]
    failed = [r for r in runs if r["conclusion"] in FAILED]
    reruns = [r for r in runs if r["attempt"] > 1]

    # superseded: a newer commit's run started on the same branch while this one was still going.
    # Counts what cancelling at that moment would have saved, in billed minutes.
    lanes = defaultdict(list)
    for r in runs:
        if r["event"] in PR_EVENTS or (r["event"] == "push" and default_branch and r["branch_short"] != default_branch):
            lanes[(r["workflow_id"], r["event"], r["branch"])].append(r)
    superseded_runs, superseded_minutes, superseded_cost = 0, 0, 0.0
    for items in lanes.values():
        for older, newer in zip(items, items[1:]):
            if older["sha"] == newer["sha"] or older["conclusion"] == "cancelled" or newer["created"] >= older["end"]:
                continue
            superseded_runs += 1
            for j in older["jobs"]:
                if not j["ran"] or j["done"] <= newer["created"]:
                    continue
                kept = math.ceil(seconds(j["started"], newer["created"]) / 60) if j["started"] < newer["created"] else 0
                saved = max(0, j["billed_min"] - kept)
                j["cancel_saves"] = saved
                superseded_minutes += saved
                rate, _ = price_of(j["label"], overrides, args.default_price, public)
                superseded_cost = None if rate is None or superseded_cost is None else superseded_cost + rate * saved

    # duplicate triggers: the same workflow ran for one commit on both push and pull_request
    seen = defaultdict(lambda: defaultdict(list))
    for r in runs:
        seen[(r["workflow_id"], r["sha"])][r["event"]].append(r)
    dup_workflows, dup_runs = Counter(), []
    for events in seen.values():
        if "push" in events and (PR_EVENTS & set(events)):
            dup_workflows[events["push"][0]["workflow"]] += 1
            dup_runs.extend(events["push"])
            for r in events["push"]:
                r["duplicate"] = True
    dup_minutes = sum(r["billed_min"] for r in dup_runs)

    tiny = [j for r in runs for j in r["jobs"] if j["ran"] and j["run_seconds"] < 30]
    result["waste"] = {
        "cancelled_runs": len(cancelled), "cancelled_minutes": sum(r["billed_min"] for r in cancelled),
        "cancelled_cost": total_cost(cancelled),
        "failed_runs": len(failed), "failed_minutes": sum(r["billed_min"] for r in failed), "failed_cost": total_cost(failed),
        "rerun_runs": len(reruns),
        "superseded_runs": superseded_runs, "superseded_minutes": superseded_minutes, "superseded_cost": superseded_cost,
        "duplicate_trigger_commits": sum(dup_workflows.values()), "duplicate_trigger_minutes": dup_minutes,
        "duplicate_trigger_cost": total_cost(dup_runs), "duplicate_trigger_workflows": dict(dup_workflows),
        "rounding_minutes": round(total_billed - raw_minutes),
        "tiny_jobs": len(tiny), "tiny_jobs_minutes": sum(j["billed_min"] for j in tiny),
    }
    waste = result["waste"]

    # --- what fails, and how long until the author hears about it
    failures = defaultdict(lambda: {"count": 0, "steps": Counter(), "wait": [], "branches": Counter()})
    for r in failed:
        for j in r["jobs"]:
            if j["conclusion"] in FAILED:
                entry = failures[(r["workflow"], j["group"])]
                entry["count"] += 1
                bad = next((s["name"] for s in j["steps"] if s["conclusion"] in FAILED), "?")
                entry["steps"][bad] += 1
                entry["wait"].append(seconds(r["created"], j["done"]))
                entry["branches"][r["branch_short"]] += 1
    result["failures"] = sorted(
        ({"workflow": k[0], "job": k[1], "count": v["count"], "step": v["steps"].most_common(1)[0][0],
          "wait_p50": pct(v["wait"], .5), "top_branch": v["branches"].most_common(1)[0][0],
          "top_branch_count": v["branches"].most_common(1)[0][1]} for k, v in failures.items()),
        key=lambda f: -f["count"])[:10]

    # --- rounds per PR
    by_branch = defaultdict(list)
    for c in commit_rows:
        by_branch[c["branch"]].append(c)
    rounds = {b: len(c) for b, c in by_branch.items()}
    replaced = [c for rows in by_branch.values() for c in rows[:-1]]   # every commit but the last on its branch
    replaced_failed = [c for c in replaced if c["outcome"] == "failed"]
    replaced_passed = [c for c in replaced if c["outcome"] == "passed"]
    pr_billed = sum(r["billed_min"] for r in pr_runs)
    minutes = lambda rows: sum(c["billed"] for c in rows)
    names = {c["branch"]: (f"#{c['pr']} " if c["pr"] else "") + c["branch"] for c in commit_rows}
    result["rounds"] = {
        "prs": len(rounds), "p50": pct(list(rounds.values()), .5), "p90": pct(list(rounds.values()), .9),
        "max": max(rounds.values()) if rounds else None,
        "top": [{"pr": names[b], "rounds": n} for b, n in sorted(rounds.items(), key=lambda kv: -kv[1])[:5]],
        "pr_billed_minutes": pr_billed, "pr_cost": total_cost(pr_runs),
        "replaced_commits": len(replaced), "replaced_minutes": minutes(replaced), "replaced_cost": total_cost(replaced),
        "replaced_failed_commits": len(replaced_failed), "replaced_failed_minutes": minutes(replaced_failed),
        "replaced_passed_commits": len(replaced_passed), "replaced_passed_minutes": minutes(replaced_passed),
    }
    replaced_minutes = result["rounds"]["replaced_minutes"]

    # --- who triggers CI (PR commits, by the account that triggered them)
    actors = defaultdict(list)
    for c in commit_rows:
        if c["actor"]:
            actors[c["actor"]].append(c)
    result["actors"] = sorted(
        ({"actor": name, "prs": len({c["branch"] for c in rows}), "commits": len(rows),
          "rounds_per_pr": round(len(rows) / len({c["branch"] for c in rows}), 1),
          "fail_rate": sum(c["outcome"] == "failed" for c in rows) / len(rows),
          "billed": minutes(rows), "cost": total_cost(rows)} for name, rows in actors.items()),
        key=lambda a: -a["billed"])[:10]

    # --- signals: leads for where to look, biggest first, each to be confirmed in the workflow files
    found = []   # (billed minutes at stake, text)

    def usd(value):
        return f", {money(value)}" if value else ""

    feedback = result["pr_feedback"]
    crit = feedback["split_mean"]
    whose = "a passing PR commit" if feedback["split_basis"] == "passing" else "a PR commit"
    if args.budget_minutes and headline and headline > args.budget_minutes * 60:
        found.append((float("inf"), f"PR feedback p50 is {fmt(headline)}, over the {args.budget_minutes:g}-minute budget."))
    if (crit.get("queue") or 0) >= 20:
        found.append((pr_billed, f"The job {whose} waits on spends {fmt(crit['queue'])} waiting for a runner: look at runner capacity or provider (runners.md)."))
    if (crit.get("before") or 0) >= 60:
        found.append((pr_billed, f"The last job of {whose} is only created {fmt(crit['before'])} after the push: a `needs:` chain is serializing work. Parallel jobs cut the wait but also run on commits that would have failed early, so price both shapes (quick-wins.md)."))
    req = feedback.get("required")
    if req and req.get("p50") and headline and headline >= 1.5 * req["p50"]:
        found.append((pr_billed, f"Everything a push starts takes p50 {fmt(headline)}, but the required checks alone take {fmt(req['p50'])}: quote the required figure as the merge gate, and weigh the rest (previews, bots) as cost, not wait (do-less-per-pr.md)."))
    for d in result["duplicates"][:2]:
        found.append((d["minutes"], f"Duplicate work ({d['kind']}): `{d['what']}` in {d['workflow']}, {d['minutes']} min in the window ({d['note']}). The same name is not always the same inputs, so confirm in the YAML before pricing it (quick-wins.md)."))
    if crit.get("setup") and crit["setup"] >= 30 and crit["setup"] >= 0.35 * (crit["setup"] + (crit.get("work") or 0)):
        found.append((pr_billed / 2, f"Setup is {share(crit['setup'], crit['setup'] + crit['work'])} of the job {whose} waits on ({fmt(crit['setup'])}): check caches and installs (caching.md)."))
    if superseded_runs:
        found.append((superseded_minutes, f"{superseded_runs} runs kept going after a newer commit was pushed to the same branch; cancelling them then would have saved {superseded_minutes} billed minutes{usd(superseded_cost)}: add or fix `concurrency` with cancel-in-progress for PRs (quick-wins.md)."))
    if dup_workflows:
        found.append((dup_minutes, f"{', '.join(dup_workflows)} ran on both `push` and `pull_request` for the same commit ({dup_minutes} billed minutes{usd(waste['duplicate_trigger_cost'])}): limit `push` to the default branch (quick-wins.md)."))
    if len(tiny) >= 20 and waste["tiny_jobs_minutes"] >= 0.01 * total_billed:
        found.append((waste["tiny_jobs_minutes"], f"{len(tiny)} jobs ran under 30s and were each billed a full minute ({waste['tiny_jobs_minutes']} billed minutes): fold tiny jobs into another job (quick-wins.md)."))
    if total_billed and waste["cancelled_minutes"] >= 0.1 * total_billed:
        found.append((waste["cancelled_minutes"], f"Cancelled runs had already used {waste['cancelled_minutes']} billed minutes ({share(waste['cancelled_minutes'], total_billed)}){usd(waste['cancelled_cost'])}: put cheap checks first so superseded runs stop early."))
    if len(runs) >= 20 and len(reruns) >= 0.05 * len(runs):
        found.append((sum(r["billed_min"] for r in reruns), f"{len(reruns)} of {len(runs)} runs were re-run ({share(len(reruns), len(runs))}): flaky tests or infrastructure (tests-and-tools.md)."))
    for f in result["failures"][:3]:
        if f["wait_p50"] and f["wait_p50"] >= 300 and CHEAP_CHECK.search(f["job"] + " " + f["step"]):
            found.append((0, f"`{f['job']}` fails on `{f['step']}` {f['count']} times, after a p50 wait of {fmt(f['wait_p50'])}: a cheap check is reporting late, and could run before the push (quick-wins.md, fewer-rounds.md)."))
    for f in result["failures"]:
        if f["top_branch_count"] >= 10 and f["top_branch_count"] >= 0.5 * f["count"] and f["top_branch"] != default_branch:
            found.append((f["top_branch_count"] * 5, f"{f['top_branch_count']} of {f['count']} `{f['job']}` failures are on one branch ({f['top_branch']}): a PR that keeps being pushed while failing, not a flaky job. Its failure rate without that branch is much lower."))
    pricey = [r for r in runners if re.search(r"macos|windows", r["label"], re.I)]
    pricey_min = sum(r["billed"] for r in pricey)
    if total_billed and pricey_min >= 0.15 * total_billed:
        pricey_cost = None if any(r["cost"] is None for r in pricey) else sum(r["cost"] for r in pricey)
        dollars = f" and {share(pricey_cost, priced_cost)} of estimated cost ({money(pricey_cost)})" if pricey_cost and priced_cost else ""
        found.append((pricey_min * 5, f"macOS/Windows jobs are {share(pricey_min, total_billed)} of billed minutes{dollars}: check whether PRs need them (do-less-per-pr.md)."))
    for m in matrix_rows:
        siblings = [x for x in matrix_rows if (x["workflow"], x["job"]) == (m["workflow"], m["job"])]
        if m["jobs"] >= 30 and not m["failed_alone"] and m["billed"] >= 0.2 * sum(x["billed"] for x in siblings) \
                and re.search(r"macos|windows", m["label"], re.I):
            found.append((m["billed"], f"`{m['leg']}` never failed alone in {m['jobs']} jobs ({m['billed']} billed minutes{usd(m['cost'])}): it caught nothing its sibling legs missed in this window (do-less-per-pr.md)."))
        if (m["queue_p50"] or 0) >= 30 and m["jobs"] >= 5:
            found.append((0, f"`{m['leg']}` waits for a runner p50 {fmt(m['queue_p50'])}, p90 {fmt(m['queue_p90'])} on {m['label']} (runners.md)."))
    has_legs = {(m["workflow"], m["job"]) for m in matrix_rows}
    for row in job_rows[:12]:
        if (row["workflow"], row["job"]) not in has_legs and ((row["queue_p50"] or 0) >= 30 or (row["queue_p90"] or 0) >= 120):
            found.append((0, f"`{row['job']}` waits for a runner p50 {fmt(row['queue_p50'])}, p90 {fmt(row['queue_p90'])} on {row['label']} (runners.md)."))
        if row["run_p50"] and row["run_max"] >= max(3600, 4 * row["run_p50"]):
            found.append((0, f"`{row['job']}` once ran {fmt(row['run_max'])} against a p50 of {fmt(row['run_p50'])}: check it has `timeout-minutes` (quick-wins.md)."))
        if (row["work_p50"] or 0) <= 2 and (row["setup_p50"] or 0) >= 30:
            found.append((row["billed"], f"`{row['job']}` spends its whole run (p50 {fmt(row['run_p50'])}) on setup and does no work of its own ({row['billed']} billed minutes): what it installs reaches no other job unless passed on as a cache or artifact (quick-wins.md)."))
    rounds_info = result["rounds"]
    if rounds_info["prs"] >= 5 and (rounds_info["p50"] >= 3 or (pr_billed and replaced_minutes >= 0.5 * pr_billed)):
        found.append((replaced_minutes, f"PRs take p50 {rounds_info['p50']:g} CI rounds and {share(replaced_minutes, pr_billed)} of PR minutes went to commits that were later replaced: cut rounds per PR and make early rounds cheap (fewer-rounds.md)."))
    busy = [a for a in result["actors"] if a["prs"] >= 5 and a["rounds_per_pr"] >= 3]
    if busy:
        found.append((sum(a["billed"] for a in busy) / 2, "Most CI rounds per PR: " + "; ".join(
            f"{a['actor']} {a['rounds_per_pr']:g} rounds, CI failed on {100 * a['fail_rate']:.0f}% of commits" for a in busy[:4])
            + " (fewer-rounds.md)."))
    if unpriced_min:
        found.append((float("inf"), f"{unpriced_min} billed minutes ran on runners with no known price: rerun with --price LABEL=USD_PER_MIN to get a cost."))
    # the window ends in silence when its last days hold almost none of the minutes
    tail, busy_until = 0, last
    for r in sorted(runs, key=lambda r: r["created"], reverse=True):
        tail += r["billed_min"]
        if tail >= 0.02 * total_billed:
            busy_until = r["created"]
            break
    quiet_days = seconds(busy_until, last) / 86400
    result["quiet_days"] = quiet_days if quiet_days >= 2 and quiet_days >= 0.25 * span_days else 0
    if result["quiet_days"]:
        found.append((float("inf"), f"The last {quiet_days:.1f} of the window's {span_days:.1f} days hold under 2% of the billed minutes "
                      f"(activity drops off after {busy_until:%Y-%m-%d}). If the repository has gone quiet, the monthly figures "
                      "describe a pace that has stopped; if it was a holiday, they understate a normal month. "
                      "Look at the active stretch and the quiet stretch separately before quoting a month."))
    found.sort(key=lambda item: -item[0])
    signals.extend(text for _, text in found)
    result["_idle_jobs"] = [(row["workflow"], row["job"]) for row in job_rows
                            if (row["work_p50"] or 0) <= 2 and (row["setup_p50"] or 0) >= 30]
    result["_quiet_legs"] = [(m["workflow"], m["leg"]) for m in matrix_rows
                             if m["jobs"] >= 30 and not m["failed_alone"] and re.search(r"macos|windows", m["label"], re.I)]
    return result


def flaky_jobs(source, runs, result):
    """Read the earlier attempts of re-run runs: what failed and then passed with no code change, and its cost."""
    reruns = [r for r in runs if r["attempt"] > 1]
    todo = [(r, k) for r in reruns for k in range(max(1, r["attempt"] - 3), r["attempt"])]
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(lambda item: source.attempt_jobs(item[0]["id"], item[1]), todo))
    earlier, seen = [], {j["id"] for r in reruns for j in r["jobs"]}
    rows = defaultdict(lambda: {"count": 0, "steps": Counter(), "billed": 0})
    for (r, _), raw in zip(todo, fetched):
        final = {j["name"]: j["conclusion"] for j in r["jobs"]}
        for raw_job in raw or []:
            j = job_record(raw_job)
            if not j["ran"] or j["id"] in seen:   # a job that was not re-run is listed again under its old id
                continue
            seen.add(j["id"])
            j["flaky"] = j["conclusion"] in FAILED and final.get(j["name"]) == "success"
            earlier.append((r, j))
            if j["flaky"] and j["run_seconds"] >= 15:   # a gate that fails in seconds only reports another job's failure
                row = rows[(r["workflow"], j["group"])]
                row["count"] += 1
                row["billed"] += j["billed_min"]
                row["steps"][next((s["name"] for s in j["steps"] if s["conclusion"] in FAILED), "?")] += 1
    result["flaky"] = {
        "reruns": len(reruns), "attempts_read": sum(raw is not None for raw in fetched),
        "earlier_billed_minutes": sum(j["billed_min"] for _, j in earlier),
        "jobs": sorted(({"workflow": k[0], "job": k[1], "count": v["count"], "billed": v["billed"],
                         "step": v["steps"].most_common(1)[0][0]} for k, v in rows.items()), key=lambda row: -row["count"]),
    }
    flaky = sum(row["count"] for row in result["flaky"]["jobs"])
    if flaky >= 3:
        top = result["flaky"]["jobs"][0]
        result["signals"].insert(0, f"{flaky} job executions failed and then passed on a re-run of the same commit, "
                                 f"{top['count']} of them `{top['job']}` at `{top['step']}`: flaky, so fix or quarantine "
                                 "them (tests-and-tools.md).")
    return earlier


def estimate_savings(runs, result, info, args, providers, earlier=()):
    """Price each fix against the jobs it would remove, in order, so no minute is saved twice."""
    public = info.get("private") is False
    overrides = [(item.rpartition("=")[0], float(item.rpartition("=")[2])) for item in args.price or []]
    monthly = 30 / max(result["window"]["days"], 1 / 24)
    jobs = []
    # earlier attempts of re-run runs are billed too, though no other table counts them
    for r, j in [(r, j) for r in runs for j in r["jobs"]] + list(earlier):
        if j["ran"]:
            rate, _ = price_of(j["label"], overrides, args.default_price, public)
            if rate is not None:
                j["left"], j["rate"], j["shape"] = float(j["billed_min"]), rate, shape_of(j["label"], public)
                j["rate_today"], j["shape_today"] = rate, j["shape"]
                jobs.append((r, j))
    current = sum(j["left"] * j["rate"] for _, j in jobs) * monthly
    earlier_cost = sum(j["left"] * j["rate"] for _, j in earlier if "rate" in j) * monthly
    items = []

    def take(name, tier, how, minutes_of, where="quick-wins.md"):
        minutes = dollars = 0.0
        for r, j in jobs:
            cut = min(j["left"], minutes_of(r, j))
            if cut > 0:
                j["left"] -= cut
                minutes += cut
                dollars += cut * j["rate"]
        if dollars * monthly >= 0.5:
            items.append({"fix": name, "tier": tier, "minutes_monthly": round(minutes * monthly),
                          "saves_monthly": dollars * monthly, "how": how, "read": where})

    take("Stop running the same workflow on both push and pull_request", "safe",
         "every job of the push runs that duplicated a pull_request run on the same commit",
         lambda r, j: j["left"] if r.get("duplicate") else 0)
    take("Cancel runs when a newer commit is pushed", "safe",
         "the billed minutes each superseded run used after the newer commit arrived",
         lambda r, j: j.get("cancel_saves", 0))
    idle = set(result.pop("_idle_jobs", []))
    take("Remove jobs that only install and pass nothing on", "safe",
         "every run of jobs whose steps are all setup: " + ", ".join(sorted(job for _, job in idle)),
         lambda r, j: j["left"] if (r["workflow"], j["group"]) in idle else 0)
    dup_names = sorted({d["what"] for d in result.get("duplicates", []) if d["kind"] == "whole job"})
    if dup_names:
        take("Skip jobs whose commands a bigger job of the same run also ran", "check first",
             "the whole billed minutes of those executions (" + ", ".join(dup_names) + "); the same step "
             "name is not always the same inputs, so read both jobs first",
             lambda r, j: j["left"] if j.get("duplicate_work") else 0)
    take("Run a repeated setup step once per job", "check first",
         "the later executions of a setup step that runs twice in one job, in raw seconds at the job's rate "
         "(see Duplicate work; repeated deploy/build composites are not counted — their inputs usually differ); "
         "per-minute rounding can make the billed saving smaller than this",
         lambda r, j: j.get("repeat_step_minutes", 0))
    take("Fix the jobs that fail and then pass on a re-run", "check first",
         "the failed executions that a re-run of the same commit turned green (see Flaky jobs)",
         lambda r, j: j["left"] if j.get("flaky") else 0, "tests-and-tools.md")

    def reprice(name, tier, how, new_rate_of, where):
        minutes = dollars = 0.0
        for r, j in jobs:
            new_rate = new_rate_of(j)
            if new_rate is not None and new_rate < j["rate"] and j["left"] > 0:
                minutes += j["left"]
                dollars += j["left"] * (j["rate"] - new_rate)
                j["rate"] = new_rate
        if dollars * monthly >= 0.5:
            items.append({"fix": name, "tier": tier, "minutes_monthly": round(minutes * monthly),
                          "saves_monthly": dollars * monthly, "how": how, "read": where})

    cold = {(c["workflow"], c["job"]) for c in result.get("cache", [])
            if c["logs"] and (c["jobs_with_miss"] >= 0.5 * c["logs"] or not (c["hits"] or c["misses"]))}

    def install_minutes(r, j):
        if (r["workflow"], j["group"]) not in cold:
            return 0
        return 0.6 * sum(s["seconds"] for s in j["steps"]
                         if s["setup"] and s["seconds"] >= 20 and INSTALL_STEP.search(s["name"])) / 60

    take("Make dependency caches hit", "check first",
         "60% of install-step time in jobs whose sampled logs show a cache miss or no cache at all; "
         "assumes a warm install takes 40% of a cold one, so measure after the first fix",
         install_minutes, "caching.md")
    after_safe = sum(j["left"] * j["rate"] for _, j in jobs) * monthly
    quiet = set(result.pop("_quiet_legs", []))
    take("Run macOS/Windows legs that never failed alone after merge instead of on every PR", "your decision",
         "every pull_request run of: " + ", ".join(sorted(leg for _, leg in quiet)) + "; they would still run on the default branch",
         lambda r, j: j["left"] if r["event"] in PR_EVENTS and (r["workflow"], j["name"]) in quiet else 0,
         "do-less-per-pr.md")
    after_pipeline = sum(j["left"] * j["rate"] for _, j in jobs) * monthly
    pipeline_rows = len(items)

    # runner changes come after every pipeline fix, so the first headline number never depends on them
    def slim_rate(j):
        if j["shape"] and j["shape"][0] == "linux" and j["run_seconds"] < 30 and not public:
            j["shape"] = None   # stays on the 1-vCPU runner whatever else moves
            return SLIM_RATE
        return None

    reprice("Run Linux jobs shorter than 30 seconds on GitHub's 1-vCPU runner", "check first",
            f"those jobs at ${SLIM_RATE} a minute (`ubuntu-slim`) instead of today's rate; it runs unprivileged, "
            "so not for jobs that need Docker", slim_rate, "runners.md")
    def arm_rate(j):
        if not j["shape"] or j["shape"][:2] != ("linux", "x64"):
            return None
        here = provider_of(j["label"])
        prices = next((prices for name, prices in providers.items() if name.startswith(here)), None)
        if not prices:
            return None
        x64, _ = provider_rate(prices, j["shape"])
        arm, _ = provider_rate(prices, ("linux", "arm", j["shape"][2]))
        if not x64 or not arm or arm >= x64:
            return None
        j["shape"] = ("linux", "arm", j["shape"][2])
        return j["rate"] * arm / x64

    reprice("Move Linux x64 jobs to arm64 runners at the same provider", "check first",
            "every Linux x64 job at that provider's arm64 list rate; assumes they all run on arm64, which needs arm "
            "builds of native dependencies and container images, so try one workflow first", arm_rate, "runners.md")
    for item in items[pipeline_rows:]:
        item["runner_change"] = True
    after_fixes = sum(j["left"] * j["rate"] for _, j in jobs) * monthly

    # the same jobs at other providers' list prices: each provider takes the jobs it lists a machine for
    today = sum(j["billed_min"] * j["rate_today"] for _, j in jobs) * monthly
    rows, family_costs = [], {}
    for name, prices in providers.items():
        cost_after = cost_today = 0.0
        moved, exact = set(), True
        def rate_for(shape):
            if not shape:
                return None, True
            rate, is_exact = provider_rate(prices, shape)
            if rate is None and shape[:2] == ("linux", "arm"):   # no arm64 there: the job can still run on x64
                rate, is_exact = provider_rate(prices, ("linux", "x64", shape[2]))
            return rate, is_exact

        by_family = Counter()
        for _, j in jobs:
            rate, is_exact = rate_for(j["shape"])
            if j["shape"]:
                by_family[j["shape"][0]] += j["left"] * (j["rate"] if rate is None else rate)
            rate_today, _ = rate_for(j["shape_today"])
            cost_today += j["billed_min"] * (j["rate_today"] if rate_today is None else rate_today)
            if rate is None:
                cost_after += j["left"] * j["rate"]
                continue
            moved.add({"linux": "Linux", "windows": "Windows", "macos": "macOS"}[j["shape"][0]])
            exact = exact and is_exact
            cost_after += j["left"] * rate
        if moved and not prices.get("self_run"):
            family_costs[name] = by_family
        if moved:
            rows.append({"provider": name, "moves": ", ".join(sorted(moved)), "today": cost_today * monthly,
                         "after_fixes": cost_after * monthly, "saves_after_fixes": after_fixes - cost_after * monthly,
                         "scaled": not exact, "self_run": bool(prices.get("self_run")),
                         "note": prices.get("note", ""), "url": prices.get("url", "")})
    rows.sort(key=lambda row: row["after_fixes"])
    # the cheapest managed provider can differ per operating system, so take the lowest for each
    stay = Counter()
    for _, j in jobs:
        if j["shape"]:
            stay[j["shape"][0]] += j["left"] * j["rate"]
    runner_saving = sum(max(0.0, cost - min(costs[family] for costs in family_costs.values()))
                        for family, cost in stay.items() if family_costs) * monthly
    result["savings"] = {
        "current_monthly": current, "items": items,
        "after_pipeline_monthly": after_pipeline, "after_fixes_monthly": after_fixes,
        "runner_monthly": runner_saving,
        "keep_runner_monthly": current - after_pipeline, "earlier_attempts_monthly": earlier_cost,
        "total_monthly": current - after_fixes + runner_saving,
        "projected_monthly": after_fixes - runner_saving,
        "monthly_today": today, "providers": rows,
        "prices_read": "2026-11-18 (GitHub, Blacksmith, Ubicloud, Sengi; others 2026-11-17)"
                       if providers is PROVIDER_PRICES else "from --providers",
        "quiet_days": result.get("quiet_days", 0),
        "unpriced_share": result["cost"]["unpriced_minutes"] / result["billed_minutes"] if result["billed_minutes"] else 0,
    }
    # wait: what the longest single job would take if nothing had to wait for another job
    passing = [c for c in result["commits"] if c["outcome"] == "passed" and not c["rerun"] and c.get("longest_job")]
    if passing:
        result["savings"]["wait_now"] = result["pr_feedback"]["headline_p50"]
        result["savings"]["wait_if_parallel"] = pct([c["longest_job"] for c in passing], .5)


def cache_evidence(source, runs, result, per_job, top_jobs):
    """Read a few recent job logs per heavy job and count cache hits and misses."""
    wanted = {(row["workflow"], row["job"]) for row in result["jobs"][:top_jobs]}
    picks = defaultdict(list)
    for r in reversed(runs):
        for j in r["jobs"]:
            key = (r["workflow"], j["group"])
            if key in wanted and j["conclusion"] == "success" and len(picks[key]) < per_job:
                picks[key].append(j)
    todo = [(key, j) for key, jobs in picks.items() for j in jobs]
    with ThreadPoolExecutor(max_workers=6) as pool:
        logs = list(pool.map(lambda item: source.log(item[1]["id"]), todo))
    rows = defaultdict(lambda: {"logs": 0, "hits": 0, "misses": 0, "jobs_with_miss": 0,
                                "task_cached": 0, "task_total": 0, "docker_cached": 0})
    for (key, _), text in zip(todo, logs):
        if not text:
            continue
        text = ANSI.sub("", text)   # color codes would break the line patterns below
        row = rows[key]
        row["logs"] += 1
        hits, misses = len(CACHE_HIT.findall(text)), len(CACHE_MISS.findall(text))
        row["hits"] += hits
        row["misses"] += misses
        row["jobs_with_miss"] += 1 if misses else 0
        for pattern in (TURBO_SUMMARY, NX_SUMMARY):
            for cached, total in pattern.findall(text):
                row["task_cached"] += int(cached)
                row["task_total"] += int(total)
        row["docker_cached"] += len(re.findall(r"^\S*\s*#\d+ CACHED", text, re.M))
    result["cache"] = [dict(workflow=k[0], job=k[1], **v) for k, v in rows.items()]
    if not result["cache"] and source.log_errors:
        result["log_error"] = source.log_errors[0]
    install = {(row["workflow"], row["job"]): row["setup_p50"] for row in result["jobs"]}
    for row in result["cache"]:
        key = (row["workflow"], row["job"])
        if row["logs"] and row["jobs_with_miss"] >= 0.5 * row["logs"]:
            result["signals"].append(f"`{row['job']}` missed a cache in {row['jobs_with_miss']} of {row['logs']} sampled logs: fix the cache key or where it is saved (caching.md).")
        if row["task_total"] >= 10 and row["task_cached"] < 0.2 * row["task_total"]:
            result["signals"].append(f"`{row['job']}` replayed {row['task_cached']} of {row['task_total']} turbo/nx tasks from cache: the task cache is not shared between runs (caching.md).")
        if row["logs"] and row["jobs_with_miss"] >= 0.5 * row["logs"]:
            continue
        if row["logs"] and not row["hits"] and not row["misses"] and (install.get(key) or 0) >= 45:
            result["signals"].append(f"`{row['job']}` spends p50 {fmt(install[key])} on setup and its logs show no cache restore at all (caching.md).")


# ---------------------------------------------------------------- report

def savings_headline(sv):
    """The two numbers a reader wants first: what the fixes save on today's runners, and with a runner move."""
    keep = sv["keep_runner_monthly"]
    extra = f", {money(sv['earlier_attempts_monthly'])} of it earlier attempts of re-run runs" \
        if sv.get("earlier_attempts_monthly", 0) >= 1 else ""
    lines = [f"> **Estimated savings** (of an estimated {money(sv['current_monthly'])} a month{extra})",
             f"> - **Keeping your runners: about {money(keep)} a month** "
             f"({share(keep, sv['current_monthly'])}, {money(keep * 12)} a year)"]
    if sv["total_monthly"] - keep >= 1:
        lines.append(f"> - **Switching runners too: about {money(sv['total_monthly'])} a month** "
                     f"({share(sv['total_monthly'], sv['current_monthly'])}, {money(sv['total_monthly'] * 12)} a year), "
                     "with jobs on the cheapest machine type and the lowest managed list price in the provider table")
    else:
        lines.append("> - **Switching runners too: no further saving.** No managed runner lists a lower price "
                     "for these jobs than what they run on now")
    if sv.get("quiet_days"):
        lines.append(f"> - The last {sv['quiet_days']:.1f} days of this window were almost idle, so a month at this pace "
                     "may not be what comes next. See the first signal")
    if sv.get("unpriced_share", 0) >= 0.05:
        lines.append(f"> - These numbers leave out {100 * sv['unpriced_share']:.0f}% of billed minutes, which ran on runners "
                     "with no known price. Pass --price LABEL=USD_PER_MIN for them and rerun")
    return "\n".join(lines)


def render(result, args):
    out = []
    w, cost, waste, rounds, feedback = (result[k] for k in ("window", "cost", "waste", "rounds", "pr_feedback"))
    billed = result["billed_minutes"]
    out.append(f"# CI audit: {result['repo']}\n")
    out.append(f"{result['runs']} completed runs from {w['from'][:10]} to {w['to'][:10]} ({w['days']:g} days). "
               "Durations are medians (p50) unless marked. Billed minutes round every job up to a whole minute, "
               "the way GitHub bills.\n")

    top = result.get("savings")
    if top and top["total_monthly"] >= 1:
        out.append(savings_headline(top) + "\n")
    out.append("## Summary\n")
    lines = []
    if feedback["commits"]:
        ok, bad = feedback["passing"], feedback["failing"]
        if ok["commits"]:
            lines.append(f"- **PR feedback time, commits that pass** (push to all checks done): p50 **{fmt(ok['p50'])}**, "
                         f"p90 **{fmt(ok['p90'])}** over {ok['commits']} commits. This is the wait for a green PR.")
        req = feedback.get("required")
        if req and req["commits"]:
            last = ", ".join(f"{name} ({share(n, req['commits'])})" for name, n in req["last"])
            lines.append(f"- **Required checks only** ({len(req['contexts'])} contexts from branch rules or "
                         f"--required-checks): p50 **{fmt(req['p50'])}**, p90 {fmt(req['p90'])} over {req['commits']} "
                         f"passing commits — the merge gate. Finishes last: {last}.")
        lines.append(f"- **All PR commits:** p50 {fmt(feedback['p50'])}, p90 {fmt(feedback['p90'])} over {feedback['commits']} commits."
                     + (f" {share(bad['commits'], feedback['commits'])} of them fail, and hear about it p50 "
                        f"{fmt(bad['first_failure_p50'])} after the push." if bad["commits"] else ""))
        per_workflow = [x for x in result["workflows"] if x["event"] in PR_EVENTS and x["runs"] >= 20 and x["p50"]]
        if len(per_workflow) > 1:
            lines.append("- **By workflow** (runs that finished, p50 / p90): " + "; ".join(
                f"{x['workflow']} {fmt(x['p50'])} / {fmt(x['p90'])}" for x in per_workflow[:5])
                + ". If some of these are not checks a PR waits on (previews, comments), rerun with --workflow for the ones that are.")
        crit = feedback["split_mean"]
        total = sum(v or 0 for v in crit.values())
        which = "a passing commit's" if feedback["split_basis"] == "passing" else "a commit's"
        lines.append(f"- **Where {which} wait goes** (average shares of the p50, following the job that finishes last): "
                     f"before that job is created {fmt(crit['before'])} ({share(crit['before'] or 0, total)}), "
                     f"queue {fmt(crit['queue'])} ({share(crit['queue'] or 0, total)}), "
                     f"setup {fmt(crit['setup'])} ({share(crit['setup'] or 0, total)}), "
                     f"work {fmt(crit['work'])} ({share(crit['work'] or 0, total)}).")
    else:
        lines.append("- No pull_request runs in the window, so there is no PR feedback time to report.")
    lines.append(f"- **Billed minutes:** {billed:,} in the window, about **{cost['billed_minutes_monthly']:,.0f} a month**.")
    if cost["unpriced_minutes"] < billed:
        note = " Public repo: standard GitHub-hosted runners are free." if result["public"] else \
            " At the rates in the Runners table (GitHub list prices as read 2026-11, or your --price values), " \
            "before any included minutes."
        lines.append(f"- **Estimated cost:** {money(cost['window'])} in the window, about **{money(cost['monthly'])} a month**.{note}")
        third_party = any(r["rate"] is not None and r["note"] != "GitHub list price" and not r["note"].startswith("free")
                          for r in result["runners"])
        if third_party and cost.get("monthly_raw") and cost["monthly_raw"] <= 0.9 * cost["monthly"]:
            lines.append(f"- **If the provider bills by the second** rather than rounding each job up to a minute: "
                         f"about **{money(cost['monthly_raw'])} a month**. Confirm the rounding rule on an invoice "
                         "before using either number; the rounding and tiny-job rows vanish under per-second billing.")
    if cost["unpriced_minutes"]:
        lines.append(f"- {cost['unpriced_minutes']:,} billed minutes are on runners with no known price (see Runners).")
    if rounds["prs"]:
        lines.append(f"- **CI rounds per PR:** p50 {rounds['p50']:g}, p90 {rounds['p90']:g}, max {rounds['max']} "
                     f"across {rounds['prs']} PR branches.")
    out.append("\n".join(lines) + "\n")

    sv = result.get("savings")
    if sv and sv["current_monthly"]:
        out.append("## Estimated savings\n")
        if sv["total_monthly"] >= 1:
            head = ["Fix", "Tier", "Billed min/mo", "Saves/mo", "What is counted"]
            row = lambda i: [i["fix"], i["tier"], f"{i['minutes_monthly']:,}", money(i["saves_monthly"]), i["how"]]
            keep_rows = [row(i) for i in sv["items"] if not i.get("runner_change")]
            move_rows = [row(i) for i in sv["items"] if i.get("runner_change")]
            if sv["runner_monthly"] >= 1:
                move_rows.append(["Move jobs to the lowest managed list price", "your decision", "-", money(sv["runner_monthly"]),
                                  "the minutes left after every row above, with Linux, Windows and macOS jobs each at the "
                                  "lowest managed list price for that system in the provider table"])
            out.append("**Keeping your runners**\n")
            out.append((table(head, keep_rows) if keep_rows else "Nothing the script can price from the data alone.") + "\n")
            if move_rows:
                out.append("**Switching runners too** (on top of the rows above)\n")
                out.append(table(head, move_rows) + "\n")
            out.append("Each row is priced on what the rows above it leave, so they add up without counting a minute twice. "
                       "These are estimates from this window at list prices: confirm each one in the workflow files before "
                       "promising it.\n")
        else:
            out.append(f"Nothing the script can price mechanically. Current estimate: {money(sv['current_monthly'])} a month.\n")
        if sv.get("wait_if_parallel") and sv.get("wait_now") and sv["wait_if_parallel"] <= 0.75 * sv["wait_now"]:
            out.append(f"**Wait:** a passing commit waits p50 {fmt(sv['wait_now'])} today. Its longest single job takes p50 "
                       f"{fmt(sv['wait_if_parallel'])}, which is what the wait would be if no job had to wait for another. "
                       "Check which `needs:` edges are real dependencies before quoting it.\n")
        if sv["providers"]:
            out.append(f"**The same jobs at other runners' list prices** (read {sv['prices_read']}; "
                       f"today {money(sv['monthly_today'])}/mo, {money(sv['after_fixes_monthly'])}/mo after the fixes). "
                       "Each row moves the jobs that provider lists a machine for and leaves the rest where they are. "
                       "The savings figure above takes the lowest managed price for each operating system, which can mean "
                       "more than one provider:\n")
            out.append(table(["Provider", "Moves", "Today's usage /mo", "After the fixes /mo", "Saves /mo after fixes", "Note"], [
                [row["provider"] + (" ~" if row["scaled"] else ""), row["moves"], money(row["today"]), money(row["after_fixes"]),
                 "-" if abs(row["saves_after_fixes"]) < 0.5 else money(row["saves_after_fixes"]) if row["saves_after_fixes"] > 0
                 else "costs " + money(-row["saves_after_fixes"]) + " more",
                 row["note"]] for row in sv["providers"]]) + "\n")
            out.append("~ means a size was scaled from, or replaced by, the nearest one the provider lists. macOS machines differ "
                       "between providers, so they are matched by class, not by core count. Not counted: free tiers, plan fees, "
                       "cache and storage charges, per-second billing, and machines that are faster or slower than today's. "
                       "Open each pricing page before quoting a number, and trial one workflow before moving.\n")
        out.append("Not priced here: fewer CI rounds per PR, faster tools, sharding and test selection. "
                   "They need a measurement or a decision first.\n")

    out.append("## Signals\n")
    out.append("Leads to confirm in the workflow files, not conclusions.\n")
    out.append("\n".join(f"- {s}" for s in result["signals"]) + "\n" if result["signals"]
               else "- Nothing crossed a threshold. Read the tables below for smaller wins.\n")

    out.append("## Workflows\n")
    out.append(table(["Workflow", "Event", "Runs", "p50", "p90", "Fail rate", "Billed min", "Share"], [
        [x["workflow"], x["event"], x["runs"], fmt(x["p50"]), fmt(x["p90"]),
         "-" if x["fail_rate"] is None else f"{100 * x['fail_rate']:.0f}%", x["billed"], share(x["billed"], billed)]
        for x in result["workflows"][:15]]) + "\n")

    out.append("## Jobs\n")
    out.append("Matrix legs and shards are folded into one row. Queue is the wait for a runner; setup and work "
               "are sums of step times, classified by step name.\n")
    out.append(table(["Workflow / job", "Jobs", "Legs/run", "Queue p50", "Queue p90", "Setup", "Work", "Run p50",
                      "Run p90", "Fail rate", "Billed min", "Share", "Est. cost", "Runner"], [
        [f"{x['workflow']} / {x['job']}", x["jobs"], f"{x['legs_per_run']:g}", fmt(x["queue_p50"]), fmt(x["queue_p90"]),
         fmt(x["setup_p50"]), fmt(x["work_p50"]), fmt(x["run_p50"]), fmt(x["run_p90"]),
         "-" if x["fail_rate"] is None else f"{100 * x['fail_rate']:.0f}%", x["billed"], share(x["billed"], billed),
         "-" if x["cost"] is None else money(x["cost"]), x["label"]]
        for x in result["jobs"][:25]]) + "\n")

    if result["matrix"]:
        out.append("## Matrix legs\n")
        out.append(table(["Workflow / job", "Leg", "Jobs", "Queue p50", "Run p50", "Billed min", "Est. cost",
                          "Failures", "Failed alone", "Runner"], [
            [f"{m['workflow']} / {m['job']}", m["leg"], m["jobs"], fmt(m["queue_p50"]), fmt(m["run_p50"]), m["billed"],
             "-" if m["cost"] is None else money(m["cost"]), m["failures"], m["failed_alone"], m["label"]]
            for m in result["matrix"]]) + "\n")
        out.append("\"Failed alone\" counts runs where this leg failed and every sibling leg passed: the times "
                   "it caught something the others did not.\n")

    out.append("## Slowest steps\n")
    out.append(table(["Workflow / job", "Step", "Class", "Runs", "p50", "p90", "Total"], [
        [f"{s['workflow']} / {s['job']}", s["step"], s["class"], s["n"], fmt(s["p50"]), fmt(s["p90"]), fmt(s["total"])]
        for s in result["steps"]]) + "\n")

    out.append("## Runners and cost\n")
    out.append(table(["Runner labels", "Provider", "Jobs", "Billed min", "Raw min", "USD/min", "Est. cost", "Note"], [
        [r["label"], r["provider"], r["jobs"], r["billed"], r["raw_minutes"],
         "-" if r["rate"] is None else f"{r['rate']:g}", "-" if r["cost"] is None else money(r["cost"]), r["note"]]
        for r in result["runners"]]) + "\n")

    out.append("## Work that bought nothing\n")
    usd = lambda value: "-" if value is None else money(value)
    out.append(table(["What", "Count", "Billed min", "Share of all", "Est. cost"], [
        ["Runs still going when a newer commit superseded them (what cancelling then would have saved)",
         waste["superseded_runs"], waste["superseded_minutes"], share(waste["superseded_minutes"], billed), usd(waste["superseded_cost"])],
        ["Commits run on both push and pull_request (the push runs)", waste["duplicate_trigger_commits"],
         waste["duplicate_trigger_minutes"], share(waste["duplicate_trigger_minutes"], billed), usd(waste["duplicate_trigger_cost"])],
        ["Cancelled runs", waste["cancelled_runs"], waste["cancelled_minutes"], share(waste["cancelled_minutes"], billed), usd(waste["cancelled_cost"])],
        ["Failed runs", waste["failed_runs"], waste["failed_minutes"], share(waste["failed_minutes"], billed), usd(waste["failed_cost"])],
        ["Jobs under 30s, each billed a full minute", waste["tiny_jobs"], waste["tiny_jobs_minutes"], share(waste["tiny_jobs_minutes"], billed), "-"],
        ["Per-minute rounding across all jobs (billed minus raw)", "-", waste["rounding_minutes"], share(waste["rounding_minutes"], billed), "-"],
        ["Runs that were re-run", waste["rerun_runs"], "-", share(waste["rerun_runs"], result["runs"]) + " of runs", "-"],
    ]) + "\n")
    out.append("Failed runs are not all waste: some caught a real bug. The rows overlap, so don't add them up.\n")

    if result.get("duplicates"):
        out.append("## Duplicate work\n")
        out.append("The same command in more than one job of a run, or the same step twice in one job. "
                   "Only steps named by their literal command are compared across jobs, but the same name can "
                   "still hide different inputs: confirm each row in the YAML before pricing it. Minutes are "
                   "billed minutes for whole jobs and raw step time otherwise.\n")
        out.append(table(["Kind", "Workflow", "What", "Affected runs", "Window min", "Evidence"], [
            [d["kind"], d["workflow"], d["what"], d["runs"], d["minutes"], d["note"]]
            for d in result["duplicates"]]) + "\n")

    if result["failures"]:
        out.append("## What fails\n")
        out.append("Wait is from the push to the moment the failing job finished.\n")
        out.append(table(["Workflow / job", "Failures", "Usual failing step", "Wait p50", "Most on one branch"], [
            [f"{f['workflow']} / {f['job']}", f["count"], f["step"], fmt(f["wait_p50"]),
             f"{f['top_branch_count']} on {f['top_branch']}"] for f in result["failures"]]) + "\n")

    flaky = result.get("flaky")
    if flaky and flaky["reruns"]:
        out.append("## Flaky jobs\n")
        if not flaky["attempts_read"]:
            out.append(f"{flaky['reruns']} runs were re-run, but their earlier attempts could not be read "
                       "(saved data from before this was collected, no access, or the API rate limit), so "
                       "flakiness is understated here and their billed minutes are missing from the totals.\n")
        else:
            out.append(f"{flaky['reruns']} runs were re-run. Their earlier attempts used {flaky['earlier_billed_minutes']:,} billed "
                       "minutes that no other table here counts; the savings estimate does include them.\n")
            if flaky["jobs"]:
                out.append(table(["Workflow / job", "Failed, then passed on re-run", "Usual failing step", "Billed min lost"], [
                    [f"{f['workflow']} / {f['job']}", f["count"], f["step"], f["billed"]] for f in flaky["jobs"][:12]]) + "\n")
                out.append("Same commit, no code change, different result: that is the definition of flaky. "
                           "The step tells you where to look; the test names are in the failed attempt's log.\n")
            else:
                out.append("No job failed and then passed on a re-run: the re-runs were of runs that failed again or were cancelled.\n")

    if rounds["prs"]:
        out.append("## CI rounds per PR\n")
        also = lambda value: "" if value is None else f", {money(value)}"
        out.append(f"- {rounds['prs']} PR branches; commits that triggered CI per branch: p50 {rounds['p50']:g}, "
                   f"p90 {rounds['p90']:g}, max {rounds['max']}.\n"
                   f"- {rounds['replaced_commits']} commits were later replaced by another commit on the same branch. They used "
                   f"{rounds['replaced_minutes']:,} of {rounds['pr_billed_minutes']:,} PR billed minutes "
                   f"({share(rounds['replaced_minutes'], rounds['pr_billed_minutes'])}{also(rounds['replaced_cost'])}).\n"
                   f"- Of those, {rounds['replaced_failed_commits']} had failed CI ({rounds['replaced_failed_minutes']:,} min) and "
                   f"{rounds['replaced_passed_commits']} had passed CI and were replaced anyway "
                   f"({rounds['replaced_passed_minutes']:,} min).\n"
                   "- Most rounds: " + ", ".join(f"{t['pr']} ({t['rounds']})" for t in rounds["top"]) + ".\n")

    if len(result["actors"]) > 1:
        out.append("## Who triggers PR runs\n")
        out.append(table(["Account", "PR branches", "Commits", "Rounds per PR", "Commits whose CI failed", "Billed min", "Est. cost"], [
            [a["actor"], a["prs"], a["commits"], f"{a['rounds_per_pr']:g}", f"{100 * a['fail_rate']:.0f}%", a["billed"],
             "-" if a["cost"] is None else money(a["cost"])]
            for a in result["actors"]]) + "\n")

    if "cache" in result:
        out.append("## Cache evidence from logs\n")
        if result["cache"]:
            out.append(table(["Workflow / job", "Logs read", "Restores hit", "Restores missed", "Logs with a miss",
                              "Task cache (turbo/nx)", "Docker CACHED layers"], [
                [f"{c['workflow']} / {c['job']}", c["logs"], c["hits"], c["misses"], c["jobs_with_miss"],
                 f"{c['task_cached']}/{c['task_total']}" if c["task_total"] else "-", c["docker_cached"] or "-"]
                for c in result["cache"]]) + "\n")
            out.append("A sample of recent successful jobs, counted from log lines. No hits and no misses means "
                       "the job restores no cache this script recognizes.\n")
        else:
            why = result.get("log_error") or "they expire, or the token lacks access"
            out.append(f"No logs could be read ({why}).\n")
    return "\n".join(out)


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="owner/name (default: the repo of the current directory)")
    parser.add_argument("--days", type=int, default=14, help="look back this many days (default 14)")
    parser.add_argument("--max-runs", type=int, default=300, help="newest runs to read (default 300)")
    parser.add_argument("--event", help="only runs for this event, e.g. pull_request")
    parser.add_argument("--branch", help="only runs for this branch")
    parser.add_argument("--workflow", help="only workflows whose name or file contains this text")
    parser.add_argument("--price", action="append", metavar="LABEL=USD_PER_MIN",
                        help="price for runners whose labels contain LABEL; repeatable")
    parser.add_argument("--default-price", type=float, help="USD per minute for any runner with no known price")
    parser.add_argument("--providers", help="JSON file of provider prices to compare against, replacing the built-in table")
    parser.add_argument("--budget-minutes", type=float, help="PR feedback budget; exit 3 when p50 is over it")
    parser.add_argument("--required-checks", metavar="A,B",
                        help="required check contexts for the merge-gate wait (default: read from branch rules)")
    parser.add_argument("--logs", type=int, default=4, help="logs to read per heavy job for cache evidence (0 = none)")
    parser.add_argument("--log-jobs", type=int, default=8, help="how many of the heaviest jobs to read logs for")
    parser.add_argument("--json", help="also write the full result as JSON to this path")
    parser.add_argument("--md", help="write the Markdown report here instead of stdout")
    parser.add_argument("--save-dir", help="save the raw API responses here")
    parser.add_argument("--from-dir", help="analyze a directory written by --save-dir instead of calling the API")
    args = parser.parse_args()

    if args.from_dir and not os.path.isfile(os.path.join(args.from_dir, "runs.json")):
        sys.exit(f"{args.from_dir} has no runs.json. Point --from-dir at a directory written by --save-dir.")
    if not args.from_dir and not shutil.which("gh") and not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        sys.exit(NO_ACCESS)
    if not args.from_dir and not args.repo:
        args.repo = ""
        if shutil.which("gh"):
            proc = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                                  capture_output=True, text=True)
            args.repo = proc.stdout.strip() if proc.returncode == 0 else ""
        if not args.repo:   # gh repo view needs GraphQL, which some tokens and proxies don't allow
            remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
            match = re.search(r"[:/]([^/:@]+)/([^/]+?)(?:\.git)?/?$", remote)
            args.repo = f"{match.group(1)}/{match.group(2)}" if match else ""
        if not args.repo:
            sys.exit("Could not tell which repo this is. Run from inside its checkout, or pass --repo owner/name.")

    source = Source(args.repo, args.from_dir, args.save_dir)
    try:
        info = source.repo_info()
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        runs_raw = source.runs(since, args.max_runs, branch=args.branch)
    except (RuntimeError, FileNotFoundError) as error:
        sys.exit(f"Could not read workflow runs: {error}")
    started = lambda r: parse_ts(r.get("run_started_at")) or parse_ts(r.get("created_at"))
    runs_raw = sorted((r for r in runs_raw if started(r)), key=started, reverse=True)
    if args.from_dir and runs_raw:   # apply the same limits to saved data, counting back from its newest run
        cutoff = started(runs_raw[0]) - timedelta(days=args.days)
        runs_raw = [r for r in runs_raw if started(r) >= cutoff
                    and (not args.branch or r.get("head_branch") == args.branch)][:args.max_runs]
    capped = len(runs_raw) >= args.max_runs
    window = (min(map(started, runs_raw)), max(parse_ts(r.get("updated_at")) or started(r) for r in runs_raw)) if runs_raw else None
    if args.workflow:
        needle = args.workflow.lower()
        runs_raw = [r for r in runs_raw if needle in (r.get("name") or "").lower() or needle in (r.get("path") or "").lower()]
    if args.event:
        runs_raw = [r for r in runs_raw if r.get("event") == args.event]
    if not runs_raw:
        sys.exit("No completed workflow runs matched. Widen --days, or check the filters.")

    print(f"Reading jobs for {len(runs_raw)} runs...", file=sys.stderr)
    quota = None if args.from_dir else source.quota()
    if quota and quota[0] < len(runs_raw):   # an estimate: runs already in --save-dir and skipped runs need no request
        print(f"This token has {quota[0]} of {quota[1]} API requests left until {quota[2]:%H:%M} UTC, fewer than the runs to read. "
              "Whatever cannot be read is left out; rerun with the same --save-dir after that time to fetch the rest.", file=sys.stderr)
    unread = []

    def jobs_of(run):
        try:
            return source.jobs(run)
        except RuntimeError as error:
            unread.append((run["id"], str(error).splitlines()[-1][:120]))
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(jobs_of, runs_raw))
    resets = ""
    if source.rate_limited:
        quota = source.quota()
        resets = f" (it resets at {quota[2]:%H:%M} UTC)" if quota else " (it resets hourly)"
    if unread:
        print(f"Could not read jobs for {len(unread)} runs (first: {unread[0][1]}). They are left out; "
              "rerun with the same --save-dir to fetch only what is missing.", file=sys.stderr)
        if len(unread) > len(runs_raw) / 2:
            if source.rate_limited:
                sys.exit(f"The GitHub API rate limit ran out before half the runs were read{resets}. Rerun then with "
                         "the same --save-dir to fetch only what is missing, or pass a smaller --max-runs.")
            sys.exit("More than half the runs could not be read. Check `gh auth status` and network access.")
        if source.rate_limited:
            print(f"The GitHub API rate limit ran out mid-fetch{resets}.", file=sys.stderr)
    runs_raw, jobs = zip(*[(r, j) for r, j in zip(runs_raw, jobs) if j is not None])
    if unread:   # project months from the runs that were read, not from days the fetch skipped
        window = (min(map(started, runs_raw)), max(parse_ts(r.get("updated_at")) or started(r) for r in runs_raw))
    required = [s.strip() for s in (args.required_checks or "").split(",") if s.strip()]
    if not required:
        required = source.branch_rules(info.get("default_branch") or "main")
    runs = build(runs_raw, {r["id"]: j for r, j in zip(runs_raw, jobs)})
    result = analyze(runs, info, args, window, required)
    if unread:
        result["signals"].insert(0, f"{len(unread)} runs could not be read and are missing from every number here; "
                                 "the window and the monthly projection cover only the runs that were read.")
        if source.rate_limited:
            result["signals"].insert(1, f"The GitHub API rate limit ran out mid-fetch{resets}: rerun later "
                                     "with the same --save-dir to fetch only what is missing, then re-analyze.")
    if capped:
        result["signals"].append(f"Hit --max-runs {args.max_runs}: the window is shorter than --days {args.days}. "
                                 "Raise --max-runs to cover more.")
    if args.logs > 0:
        cache_evidence(source, runs, result, args.logs, args.log_jobs)

    providers = PROVIDER_PRICES
    if args.providers:
        with open(args.providers, encoding="utf-8") as handle:
            providers = json.load(handle)
    estimate_savings(runs, result, info, args, providers, flaky_jobs(source, runs, result))

    if not args.from_dir:
        print(f"{source.requests} GitHub API requests"
              + (f"; {source.reused} runs reused from {args.save_dir}" if source.reused else ""), file=sys.stderr)
    report = render(result, args)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as handle:
            handle.write(report)
    else:
        print(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, default=str)

    feedback = result["pr_feedback"]["headline_p50"]
    if args.budget_minutes and feedback and feedback > args.budget_minutes * 60:
        print(f"Over budget: PR feedback p50 {fmt(feedback)} > {args.budget_minutes:g}m", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
