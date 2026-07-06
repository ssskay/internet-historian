#!/usr/bin/env python3
"""Internet Historian — quietly preserves the web things you love, forever.

A patient, Internet-Archive-only archiving queue. It optimizes for never losing
a URL, not for speed. A periodic launchd-driven "drain" claims whatever SPN2
capture slots the Wayback Machine offers, submits, polls, and exits. Throttling
is treated as normal weather, not failure.

Everything lives in this one file: CLI + queue + SPN2 client.
"""

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 has no stdlib tomllib
    try:
        import tomli as tomllib  # type: ignore  # pip3 install tomli
    except ModuleNotFoundError:
        sys.exit(
            "Internet Historian needs Python 3.11+ (for tomllib).\n"
            f"You're running Python {sys.version.split()[0]}. Either upgrade Python, or run:\n"
            "    pip3 install --break-system-packages tomli"
        )
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

# Optional pretty terminal output. Installed via the `[pretty]` extra
# (`pipx install "internet-historian[pretty]"`). If rich isn't importable, the
# plain-text renderers are used instead — zero new required dependencies.
try:
    from rich.console import Console as _RichConsole
    from rich.table import Table as _RichTable
    from rich.text import Text as _RichText
except ModuleNotFoundError:  # rich not installed — plain output only
    _RichConsole = _RichTable = _RichText = None

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_NAME = "internet-historian"
__version__ = "0.2.0"
ROOT = Path(__file__).resolve().parent

# Wikimedia API etiquette: identify the client with a descriptive User-Agent
# that includes a contact/repo. Used by the `discover` command (Wikipedia +
# Wikidata). No API keys are involved — these are public read endpoints.
WIKI_USER_AGENT = (
    f"internet-historian/{__version__} "
    "(https://github.com/ssskay/internet-historian; sara@sarakay.me)"
)


def _platform_state_dir() -> Path:
    """Per-user state directory, used when there's no repo-local checkout.

    macOS:   ~/Library/Application Support/internet-historian
    Windows: %LOCALAPPDATA%\\internet-historian
    Linux/other POSIX: $XDG_DATA_HOME/internet-historian (default
                       ~/.local/share/internet-historian)
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def _resolve_state():
    """Decide where config + runtime state live.

    Precedence: a repo-local file/dir wins if it already exists (back-compat
    with an in-place git checkout — an existing install keeps using its
    queue.db, logs, and config.toml). Otherwise fall back to the per-user
    platform directory. Nothing is moved or migrated; `status` reports the
    resolved locations so it's always clear where state actually is.
    """
    state = _platform_state_dir()
    repo_config = ROOT / "config.toml"
    repo_data = ROOT / "data"
    repo_logs = ROOT / "logs"
    config_path = repo_config if repo_config.exists() else state / "config.toml"
    data_dir = repo_data if repo_data.is_dir() else state / "data"
    logs_dir = repo_logs if repo_logs.is_dir() else state / "logs"
    return config_path, data_dir, logs_dir


STATE_DIR = _platform_state_dir()
CONFIG_PATH, DATA_DIR, LOGS_DIR = _resolve_state()
DB_PATH = DATA_DIR / "queue.db"
LOG_PATH = LOGS_DIR / "historian.log"

# The current OS user owns the Keychain items and the launchd job. Nothing is
# hardcoded to any one person, so this repo works for whoever installs it.
USER = getpass.getuser()
KEYCHAIN_ACCOUNT = os.environ.get("IA_KEYCHAIN_ACCOUNT", USER)

LABEL = "com.internet-historian.drain"
PLIST_NAME = f"{LABEL}.plist"
# Older single-user installs used a personalized label / Keychain account;
# setup cleans these up and migrates keys to the current user's account.
LEGACY_LABELS = ["com.sara.internet-historian"]
LEGACY_KEYCHAIN_ACCOUNTS = ["sara"]

SPN2 = "https://web.archive.org"

log = logging.getLogger("historian")

# SPN2 error taxonomy (from the official API doc, verified 2026).
# Throttle/transient: retry forever, never count toward death.
THROTTLE_ERRORS = {
    "error:user-session-limit",
    "error:too-many-daily-captures",
    "error:proxy-error",
    "error:soft-time-limit-exceeded",
    "error:capture-location-error",
    "error:browsing-timeout",
}
# Candidate-dead: the target answered and the answer is bad.
DEAD_ERRORS = {
    "error:invalid-url",
    "error:not-found",
    "error:invalid-host-resolution",
    "error:blocked-url",
    "error:forbidden",
}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  collection TEXT NOT NULL DEFAULT 'default',
  status TEXT NOT NULL DEFAULT 'queued',
  job_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  attempts_today INTEGER NOT NULL DEFAULT 0,
  attempts_today_date TEXT,
  dead_strikes INTEGER NOT NULL DEFAULT 0,
  last_dead_strike_at TEXT,
  last_attempt_at TEXT,
  next_attempt_at TEXT,
  archive_url TEXT,
  last_error TEXT,
  dead_reason TEXT,
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    return iso(now())


def today_str() -> str:
    return now().date().isoformat()


def parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def human_age(s) -> str:
    dt = parse_iso(s)
    if not dt:
        return "?"
    delta = now() - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _keychain(service: str, account=None):
    account = account or KEYCHAIN_ACCOUNT
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        return r.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        # CalledProcessError: key not in Keychain. FileNotFoundError: no
        # `security` binary (non-macOS) — fall through to env vars.
        return None


def _keychain_any(service: str):
    """Look up a secret under the current account, then any legacy account."""
    val = _keychain(service)
    if val:
        return val
    for acct in LEGACY_KEYCHAIN_ACCOUNTS:
        val = _keychain(service, account=acct)
        if val:
            return val
    return None


def _store_key(service: str, value: str):
    """Store (or update) a secret in the login Keychain under the current user."""
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w", value],
        check=True,
    )


def read_credentials():
    """Return (access_key, secret_key) or (None, None) — never exits."""
    ak = os.environ.get("IA_ACCESS_KEY") or _keychain_any("ia-s3-access")
    sk = os.environ.get("IA_SECRET_KEY") or _keychain_any("ia-s3-secret")
    return (ak or None), (sk or None)


def get_credentials():
    """Return (access_key, secret_key). Env vars win (for testing), then Keychain.

    Exits with a helpful message if neither source has the keys.
    """
    ak, sk = read_credentials()
    if not ak or not sk:
        sys.stderr.write(
            "ERROR: Internet Archive S3 keys not found.\n"
            "  Run `internet-historian setup` to walk through adding them,\n"
            "  or store them in the macOS Keychain yourself (get them at\n"
            "  https://archive.org/account/s3.php while logged in):\n"
            f"    security add-generic-password -a {KEYCHAIN_ACCOUNT} -s ia-s3-access -w <ACCESS_KEY>\n"
            f"    security add-generic-password -a {KEYCHAIN_ACCOUNT} -s ia-s3-secret -w <SECRET_KEY>\n"
            "  Or set IA_ACCESS_KEY / IA_SECRET_KEY in the environment for testing.\n"
        )
        sys.exit(1)
    return ak, sk


def make_session(creds=None) -> requests.Session:
    ak, sk = creds or get_credentials()
    s = requests.Session()
    s.headers.update(
        {"Accept": "application/json", "Authorization": f"LOW {ak}:{sk}"}
    )
    return s


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "spn2": {"if_not_archived_within": "30d", "per_url_daily_attempt_cap": 5},
    "drain": {
        "interval_seconds": 600,
        "batch_headroom": 2,
        "submitted_timeout_minutes": 90,
    },
    "backoff": {"base_seconds": 300, "max_seconds": 86400},
    "death": {"confirmations": 3, "spacing_hours": 24},
    # Per-collection periodic recapture, e.g. {"chiikawa": {"refresh_days": 30}}.
    # Empty by default: nothing recaptures unless explicitly listed.
    "collections": {},
}


def load_config():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            cfg.setdefault(section, {})
            cfg[section].update(values)
    return cfg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=5
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def db_connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

_STRIP_PARAMS = {"fbclid", "gclid", "gclsrc", "dclid", "msclkid"}


def normalize_url(url: str) -> str:
    """Strip fragment, drop utm_*/click-id params, lowercase scheme+host."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in _STRIP_PARAMS)
    ]
    query = urlencode(kept)
    return urlunsplit((scheme, host, parts.path, query, ""))


# ---------------------------------------------------------------------------
# SPN2 client
# ---------------------------------------------------------------------------


class Transient(Exception):
    """A failure on our own HTTP request that should always be retried."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _get_json(session, url, **kw):
    try:
        r = session.get(url, timeout=30, **kw)
    except requests.RequestException as e:
        raise Transient(f"GET {url} failed: {e}")
    if r.status_code == 429 or r.status_code >= 500:
        ra = r.headers.get("Retry-After")
        raise Transient(f"GET {url} HTTP {r.status_code}", retry_after=ra)
    try:
        return r.json()
    except ValueError:
        raise Transient(f"GET {url} non-JSON (HTTP {r.status_code})")


def system_ok(session):
    """Return (ok: bool, raw: dict)."""
    data = _get_json(session, f"{SPN2}/save/status/system")
    return data.get("status") == "ok", data


def user_status(session):
    """Return (available, processing). Cache-buster required."""
    t = f"{int(time.time() * 1000)}{random.randint(0, 9999)}"
    data = _get_json(session, f"{SPN2}/save/status/user", params={"_t": t})
    return int(data.get("available", 0)), int(data.get("processing", 0))


def submit(session, target_url, cfg):
    """Submit a capture.

    Returns one of:
      ('submitted', job_id)  -- a capture job is running; poll it
      ('already', message)   -- server-side dedup: a recent snapshot exists,
                                SPN2 declined to recapture. This is a
                                preservation SUCCESS, not a failure.
      ('error', err_text)    -- immediate error (no job spawned)

    Raises Transient on our-side HTTP failures (429/5xx/timeout/etc.).
    """
    form = {
        "url": target_url,
        "if_not_archived_within": cfg["spn2"]["if_not_archived_within"],
        "skip_first_archive": "1",
        "js_behavior_timeout": "0",
    }
    try:
        r = session.post(f"{SPN2}/save/", data=form, timeout=60)
    except requests.RequestException as e:
        raise Transient(f"POST /save/ failed: {e}")
    if r.status_code == 429 or r.status_code >= 500:
        raise Transient(
            f"POST /save/ HTTP {r.status_code}", retry_after=r.headers.get("Retry-After")
        )
    try:
        data = r.json()
    except ValueError:
        raise Transient(f"POST /save/ non-JSON (HTTP {r.status_code})")
    job_id = data.get("job_id")
    if job_id:
        return "submitted", job_id
    msg = data.get("message") or ""
    ml = msg.lower()
    # Dedup: "The same snapshot had been made N hours ago. You can make new
    # capture of this URL after 720 hours." -> the target is already preserved.
    if "you can make new capture" in ml or "same snapshot had been made" in ml:
        return "already", msg
    # Immediate error (no job spawned): surface status_ext/message.
    err = data.get("status_ext") or msg or json.dumps(data)
    return "error", err


def wayback_lookup(session, target_url):
    """Return (archive_url, timestamp) for the closest existing snapshot, or
    (None, None). Best-effort; never raises."""
    try:
        r = session.get(
            "https://archive.org/wayback/available",
            params={"url": target_url},
            timeout=30,
        )
        data = r.json()
    except (requests.RequestException, ValueError):
        return None, None
    snap = (data.get("archived_snapshots") or {}).get("closest") or {}
    url = snap.get("url")
    if url and snap.get("available"):
        return url, snap.get("timestamp")
    return None, None


def poll_jobs(session, job_ids):
    """Batch-poll job statuses. Return {job_id: status_dict}."""
    if not job_ids:
        return {}
    try:
        r = session.post(
            f"{SPN2}/save/status",
            data={"job_ids": ",".join(job_ids)},
            timeout=30,
        )
    except requests.RequestException as e:
        raise Transient(f"POST /save/status failed: {e}")
    if r.status_code == 429 or r.status_code >= 500:
        raise Transient(f"POST /save/status HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise Transient(f"POST /save/status non-JSON (HTTP {r.status_code})")
    out = {}
    items = data if isinstance(data, list) else [data]
    for item in items:
        jid = item.get("job_id")
        if jid:
            out[jid] = item
    return out


# ---------------------------------------------------------------------------
# Error classification & routing
# ---------------------------------------------------------------------------


def classify(err_text: str) -> str:
    """Return 'throttle', 'daily', 'dead', or 'unknown'."""
    e = (err_text or "").lower()
    if "too-many-daily-captures" in e:
        return "daily"
    for code in THROTTLE_ERRORS:
        if code in e:
            return "throttle"
    for code in DEAD_ERRORS:
        if code in e:
            return "dead"
    return "unknown"


def backoff_seconds(attempts: int, cfg) -> float:
    base = cfg["backoff"]["base_seconds"]
    mx = cfg["backoff"]["max_seconds"]
    delay = min(base * (2 ** max(0, attempts)), mx)
    jitter = delay * 0.2 * (2 * random.random() - 1)  # +/-20%
    return max(1.0, delay + jitter)


def _requeue(conn, row_id, attempts, cfg, err, retry_after=None):
    if retry_after:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = backoff_seconds(attempts, cfg)
    else:
        delay = backoff_seconds(attempts, cfg)
    nxt = iso(now() + timedelta(seconds=delay))
    conn.execute(
        "UPDATE urls SET status='queued', next_attempt_at=?, last_error=?, "
        "job_id=NULL, updated_at=? WHERE id=?",
        (nxt, err, now_iso(), row_id),
    )
    conn.commit()
    return nxt


def route_error(conn, row, err_text, cfg, dead_conf, dead_spacing):
    """Apply the failure taxonomy to a single row and persist the result."""
    kind = classify(err_text)
    rid = row["id"]
    url = row["url"]

    if kind == "daily":
        # Don't count as a strike; try again tomorrow.
        tomorrow = now().replace(hour=0, minute=5, second=0, microsecond=0) + timedelta(days=1)
        conn.execute(
            "UPDATE urls SET status='queued', next_attempt_at=?, last_error=?, "
            "job_id=NULL, updated_at=? WHERE id=?",
            (iso(tomorrow), err_text, now_iso(), rid),
        )
        conn.commit()
        log.info("%s daily-cap hit; deferring to tomorrow (no strike)", url)
        return "throttle"

    if kind in ("throttle", "unknown"):
        nxt = _requeue(conn, rid, row["attempts"], cfg, err_text)
        log.info("%s transient (%s): %s; retry at %s", url, kind, err_text, nxt)
        return "throttle"

    # kind == 'dead' -> candidate-dead; apply spacing.
    last = parse_iso(row["last_dead_strike_at"])
    strikes = row["dead_strikes"]
    counted = False
    if last is None or (now() - last) >= timedelta(hours=dead_spacing):
        strikes += 1
        counted = True
        conn.execute(
            "UPDATE urls SET dead_strikes=?, last_dead_strike_at=?, updated_at=? WHERE id=?",
            (strikes, now_iso(), now_iso(), rid),
        )
        conn.commit()

    if strikes >= dead_conf:
        conn.execute(
            "UPDATE urls SET status='dead', dead_reason=?, last_error=?, "
            "job_id=NULL, updated_at=? WHERE id=?",
            (err_text, err_text, now_iso(), rid),
        )
        conn.commit()
        log.warning("%s marked DEAD after %d strike(s): %s", url, strikes, err_text)
        return "dead"

    nxt = _requeue(conn, rid, row["attempts"], cfg, err_text)
    log.info(
        "%s candidate-dead (%s, strike %d/%d%s); retry at %s",
        url,
        err_text,
        strikes,
        dead_conf,
        "" if counted else ", within spacing so not counted",
        nxt,
    )
    return "candidate-dead"


# ---------------------------------------------------------------------------
# Subcommand: check  (Phase 0)
# ---------------------------------------------------------------------------


def cmd_check(args):
    session = make_session()
    try:
        avail, proc = user_status(session)
        print(json.dumps({"available": avail, "processing": proc}))
    except Transient as e:
        print(f"user status error: {e}", file=sys.stderr)
        return 1
    try:
        ok, raw = system_ok(session)
        print(json.dumps({"system": raw}))
    except Transient as e:
        print(f"system status error: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommand: add  (Phase 1)
# ---------------------------------------------------------------------------


def _confirm(question, default=False):
    """Yes/no prompt. Non-interactive stdin returns `default` (never blocks)."""
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _collect_urls(args):
    urls = list(args.urls or [])
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    return urls


def _insert_urls(conn, raw_urls, collection):
    """Normalize, validate, and queue a batch of URLs. Returns (added, skipped).

    Shared by `add`, `discover`, and bookmarks import so they all normalize the
    same way and log consistently. Already-tracked URLs (or unparseable ones)
    count as skipped, never as errors.
    """
    added = skipped = 0
    for r in raw_urls:
        norm = normalize_url(r)
        if not urlsplit(norm).netloc:
            log.warning("skipping unparseable url: %r", r)
            skipped += 1
            continue
        try:
            conn.execute(
                "INSERT INTO urls (url, collection, status, added_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?)",
                (norm, collection, now_iso(), now_iso()),
            )
            conn.commit()
            added += 1
            log.info("added %s [collection=%s]", norm, collection)
        except sqlite3.IntegrityError:
            skipped += 1
            existing = conn.execute(
                "SELECT status FROM urls WHERE url=?", (norm,)
            ).fetchone()
            log.info("already tracked, status=%s: %s", existing["status"], norm)
    return added, skipped


def cmd_add(args):
    if getattr(args, "bookmarks", None):
        return cmd_add_bookmarks(args)
    raw = _collect_urls(args)
    if not raw:
        print("Nothing to add (give URLs, --file, or --bookmarks).", file=sys.stderr)
        return 1
    conn = db_connect()
    added, skipped = _insert_urls(conn, raw, args.collection)
    total_queued = conn.execute(
        "SELECT COUNT(*) c FROM urls WHERE status IN ('queued','submitted')"
    ).fetchone()["c"]
    conn.close()
    print(f"added {added}, skipped {skipped}, {total_queued} in flight (queued+submitted)")
    return 0


# ---------------------------------------------------------------------------
# Bookmarks import  (add --bookmarks)
# ---------------------------------------------------------------------------


class _BookmarksParser(HTMLParser):
    """Parse the Netscape bookmarks HTML every browser exports.

    The format nests folders as `<DT><H3>Name</H3>` followed by a `<DL>` block;
    links are `<DT><A HREF="...">Title</A>`. We track the folder stack so each
    link carries the tuple of ancestor folder names, which lets `--folder` scope
    the import to a single subtree.
    """

    def __init__(self):
        super().__init__()
        self.links = []          # list of (folder_path_tuple, href)
        self._folders = []       # current ancestor folder names
        self._capture = None     # 'h3', 'a', or None
        self._h3_text = ""
        self._href = None

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "h3":
            self._capture = "h3"
            self._h3_text = ""
        elif t == "a":
            self._href = dict(attrs).get("href")
            self._capture = "a"

    def handle_data(self, data):
        if self._capture == "h3":
            self._h3_text += data

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "h3" and self._capture == "h3":
            self._folders.append(self._h3_text.strip())
            self._capture = None
        elif t == "a" and self._capture == "a":
            if self._href:
                self.links.append((tuple(self._folders), self._href))
            self._href = None
            self._capture = None
        elif t == "dl" and self._folders:
            # Closing a folder's <DL>. The outermost (root) <DL> has no matching
            # <H3>, so the empty-stack guard absorbs its trailing </DL>.
            self._folders.pop()


def parse_bookmarks(html, folder=None):
    """Return the list of hrefs in a Netscape bookmarks export.

    If `folder` is given, only links whose ancestor path contains a folder of
    that name (case-insensitive) — i.e. the whole subtree under it — are kept.
    Only http(s) links are returned; javascript:/place: bookmarks are dropped.
    """
    p = _BookmarksParser()
    p.feed(html)
    want = folder.lower() if folder else None
    out = []
    for path, href in p.links:
        if not href or not href.lower().startswith(("http://", "https://")):
            continue
        if want is not None and want not in [f.lower() for f in path]:
            continue
        out.append(unescape(href))
    return out


def cmd_add_bookmarks(args):
    path = Path(args.bookmarks)
    if not path.exists():
        print(f"Bookmarks file not found: {path}", file=sys.stderr)
        return 1
    html = path.read_text(encoding="utf-8", errors="replace")
    hrefs = parse_bookmarks(html, folder=args.folder)
    scope = f" in folder '{args.folder}'" if args.folder else ""
    if not hrefs:
        print(f"No http(s) bookmarks found{scope} in {path}.", file=sys.stderr)
        return 1
    log.info("bookmarks: parsed %d link(s) from %s%s", len(hrefs), path, scope)

    conn = db_connect()
    # Split into new vs. already-tracked (by normalized URL), preserving order
    # and de-duplicating within the file itself.
    seen = set()
    fresh, already = [], 0
    for h in hrefs:
        norm = normalize_url(h)
        if not urlsplit(norm).netloc or norm in seen:
            continue
        seen.add(norm)
        exists = conn.execute("SELECT 1 FROM urls WHERE url=?", (norm,)).fetchone()
        if exists:
            already += 1
        else:
            fresh.append(h)

    print(f"{len(fresh)} new, {already} already tracked"
          f" (from {len(hrefs)} bookmark link(s){scope}).")
    if not fresh:
        print("Nothing new to queue.")
        conn.close()
        return 0

    if not _confirm(f"Queue {len(fresh)} new URL(s) to collection '{args.collection}'?"):
        print("Cancelled; nothing queued.")
        conn.close()
        return 0

    added, skipped = _insert_urls(conn, fresh, args.collection)
    conn.close()
    print(f"added {added}, skipped {skipped}.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: discover  (Wikipedia / Wikidata as a no-LLM curation backend)
# ---------------------------------------------------------------------------

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# External links Wikipedia articles carry that are references/identifiers, not
# the subject's own web presence. Dropped from discover candidates.
_EXTLINK_JUNK_SUBSTR = (
    "archive.org", "web.archive.org", "doi.org", "jstor.org", "worldcat.org",
    "ncbi.nlm.nih.gov", "pubmed", "wikidata.org", "wikimedia.org",
    "wikipedia.org", "wiktionary.org", "wikisource.org", "wikimediafoundation",
    "viaf.org", "id.loc.gov", "loc.gov/authorities", "d-nb.info",
    "catalogue.bnf.fr", "data.bnf.fr", "idref.fr", "isni.org",
    "musicbrainz.org", "books.google", "google.com/books", "semanticscholar.org",
    "snaccooperative.org", "orcid.org", "researchgate.net", "handle.net",
    " researcherid", " isbnsearch.org", "zbmath.org",
)


def wiki_session():
    """A plain requests session with a polite Wikimedia User-Agent. No IA keys."""
    s = requests.Session()
    s.headers.update({"User-Agent": WIKI_USER_AGENT, "Accept": "application/json"})
    return s


def _wiki_api_base(lang):
    return f"https://{lang}.wikipedia.org/w/api.php"


def _strip_html(s):
    """Flatten the little `<span class="searchmatch">` markup in search snippets."""
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_search_results(data):
    """From an action=query&list=search response -> [{title, pageid, snippet}]."""
    hits = ((data or {}).get("query") or {}).get("search") or []
    out = []
    for h in hits:
        out.append({
            "title": h.get("title", ""),
            "pageid": h.get("pageid"),
            "snippet": _strip_html(h.get("snippet", "")),
        })
    return out


def parse_page_info(data):
    """From prop=pageprops|info&inprop=url -> {title, qid, url, is_disambiguation}.

    Returns None if the page is missing. Assumes formatversion=2 (pages is a list).
    """
    pages = ((data or {}).get("query") or {}).get("pages") or []
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    props = page.get("pageprops") or {}
    return {
        "title": page.get("title", ""),
        "qid": props.get("wikibase_item"),
        "url": page.get("fullurl") or page.get("canonicalurl"),
        "is_disambiguation": "disambiguation" in props,
    }


def parse_official_sites(data):
    """From a Wikidata wbgetclaims P856 response -> [official-website url, ...]."""
    claims = ((data or {}).get("claims") or {}).get("P856") or []
    out = []
    for c in claims:
        snak = c.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        val = (snak.get("datavalue") or {}).get("value")
        if isinstance(val, str) and val:
            out.append(val)
    return out


def parse_extlinks(data):
    """From prop=extlinks -> [url, ...]. Handles both formatversion shapes."""
    pages = ((data or {}).get("query") or {}).get("pages") or []
    out = []
    for page in pages:
        for e in page.get("extlinks") or []:
            url = e.get("url") if isinstance(e, dict) else None
            url = url or (e.get("*") if isinstance(e, dict) else None)
            if url:
                # Protocol-relative links (//example.com) show up here; default
                # them to https so they normalize and queue cleanly.
                if url.startswith("//"):
                    url = "https:" + url
                out.append(url)
    return out


def filter_extlinks(urls):
    """Drop reference/identifier links and de-dupe, keeping the subject's own sites."""
    out, seen = [], set()
    for u in urls:
        low = (u or "").lower()
        if not low.startswith(("http://", "https://")):
            continue
        if any(j in low for j in _EXTLINK_JUNK_SUBSTR):
            continue
        norm = normalize_url(u)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(u)
    return out


def parse_disambiguation_options(data):
    """From prop=links&plnamespace=0 -> [article title, ...]."""
    pages = ((data or {}).get("query") or {}).get("pages") or []
    out = []
    for page in pages:
        for l in page.get("links") or []:
            title = l.get("title")
            if title:
                out.append(title)
    return out


def wiki_search(session, term, lang="en"):
    data = _get_json(session, _wiki_api_base(lang), params={
        "action": "query", "list": "search", "srsearch": term,
        "srlimit": 10, "format": "json", "formatversion": 2,
    })
    return parse_search_results(data)


def wiki_page_info(session, title, lang="en"):
    data = _get_json(session, _wiki_api_base(lang), params={
        "action": "query", "prop": "pageprops|info", "inprop": "url",
        "titles": title, "format": "json", "formatversion": 2,
    })
    return parse_page_info(data)


def wiki_extlinks(session, title, lang="en"):
    data = _get_json(session, _wiki_api_base(lang), params={
        "action": "query", "prop": "extlinks", "titles": title,
        "ellimit": "max", "format": "json", "formatversion": 2,
    })
    return filter_extlinks(parse_extlinks(data))


def wiki_disambiguation_options(session, title, lang="en"):
    data = _get_json(session, _wiki_api_base(lang), params={
        "action": "query", "prop": "links", "titles": title,
        "plnamespace": 0, "pllimit": "max", "format": "json", "formatversion": 2,
    })
    return parse_disambiguation_options(data)


def wikidata_official_sites(session, qid):
    if not qid:
        return []
    data = _get_json(session, WIKIDATA_API, params={
        "action": "wbgetclaims", "entity": qid, "property": "P856", "format": "json",
    })
    return parse_official_sites(data)


def parse_selection(text, count):
    """Parse a discover selection line into a sorted list of 0-based indices.

    'a'/'all' -> every candidate. Comma/space-separated numbers -> those (1-based
    in, 0-based out). Out-of-range and garbage tokens are ignored. Empty -> [].
    """
    t = (text or "").strip().lower()
    if not t:
        return []
    if t in ("a", "all"):
        return list(range(count))
    picks = set()
    for tok in re.split(r"[,\s]+", t):
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= count:
                picks.add(n - 1)
    return sorted(picks)


def _slug(term):
    s = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
    return s or "discover"


def _discover_candidates(session, info):
    """Assemble labelled candidates for a chosen article, most-primary first.

    Order: official website(s) (Wikidata P856), then the Wikipedia article,
    then filtered external links. De-duplicated by normalized URL so an official
    site listed in both P856 and the article's external links appears once.
    """
    candidates = []
    seen = set()

    def add(url, label):
        if not url:
            return
        norm = normalize_url(url)
        if norm in seen or not urlsplit(norm).netloc:
            return
        seen.add(norm)
        candidates.append({"url": url, "label": label})

    try:
        for site in wikidata_official_sites(session, info.get("qid")):
            add(site, "official site")
    except Transient as e:
        log.warning("discover: Wikidata lookup failed: %s", e)
    add(info.get("url"), "wikipedia")
    try:
        for link in wiki_extlinks(session, info["title"]):
            add(link, "external link")
    except Transient as e:
        log.warning("discover: extlinks lookup failed: %s", e)
    return candidates


def cmd_discover(args):
    term = args.term
    lang = args.lang
    session = wiki_session()

    try:
        results = wiki_search(session, term, lang)
    except Transient as e:
        print(f"Couldn't reach Wikipedia ({e}). Try again in a moment.", file=sys.stderr)
        return 1

    if not results:
        print(f"No Wikipedia article found for {term!r}. "
              "Try a different spelling or a broader term.")
        return 0

    interactive = sys.stdin.isatty()

    # Pick the article. One hit -> use it; several -> numbered pick-list.
    if len(results) == 1 or not interactive:
        chosen_title = results[0]["title"]
        if len(results) > 1:
            print(f"Multiple matches for {term!r}; using the top hit "
                  f"({chosen_title!r}). Re-run in a terminal to choose.")
    else:
        print(f"\nMatches for {term!r}:\n")
        for i, r in enumerate(results, 1):
            snip = f" — {r['snippet']}" if r["snippet"] else ""
            print(f"  {i}. {r['title']}{snip}")
        raw = input("\nWhich one? [number, default 1]: ").strip()
        idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(results) else 0
        chosen_title = results[idx]["title"]

    try:
        info = wiki_page_info(session, chosen_title, lang)
    except Transient as e:
        print(f"Couldn't load {chosen_title!r} ({e}).", file=sys.stderr)
        return 1
    if info is None:
        print(f"Article {chosen_title!r} could not be loaded.", file=sys.stderr)
        return 1

    # Resolve a disambiguation page to a concrete article.
    if info.get("is_disambiguation"):
        try:
            options = wiki_disambiguation_options(session, chosen_title, lang)
        except Transient:
            options = []
        if options and interactive:
            print(f"\n{chosen_title!r} is a disambiguation page. Did you mean:\n")
            for i, t in enumerate(options[:20], 1):
                print(f"  {i}. {t}")
            raw = input("\nWhich one? [number, default 1]: ").strip()
            shown = options[:20]
            idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(shown) else 0
            chosen_title = shown[idx]
            try:
                info = wiki_page_info(session, chosen_title, lang)
            except Transient as e:
                print(f"Couldn't load {chosen_title!r} ({e}).", file=sys.stderr)
                return 1
            if info is None:
                print(f"Article {chosen_title!r} could not be loaded.", file=sys.stderr)
                return 1
        else:
            print(f"{chosen_title!r} is a disambiguation page; "
                  "re-run in a terminal to pick a specific article.")

    log.info("discover: chosen article %r (qid=%s)", info["title"], info.get("qid"))
    candidates = _discover_candidates(session, info)

    print(f"\nCandidates for {info['title']!r}:\n")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. [{c['label']}] {c['url']}")

    if not interactive:
        print("\n(Not a terminal — nothing queued. Re-run interactively to select.)")
        return 0

    raw = input(
        "\nQueue which? [comma-separated numbers, 'a' for all, empty to cancel]: "
    )
    picks = parse_selection(raw, len(candidates))
    if not picks:
        print("Nothing selected; nothing queued.")
        return 0

    collection = args.collection or _slug(term)
    urls = [candidates[i]["url"] for i in picks]
    conn = db_connect()
    added, skipped = _insert_urls(conn, urls, collection)
    conn.close()
    print(f"\nadded {added}, skipped {skipped} to collection '{collection}'. "
          "The background job will preserve them patiently.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: drain  (Phases 1 & 2)
# ---------------------------------------------------------------------------


def _poll_submitted(conn, session, cfg, dead_conf, dead_spacing):
    rows = conn.execute(
        "SELECT * FROM urls WHERE status='submitted' AND job_id IS NOT NULL"
    ).fetchall()
    if not rows:
        return
    by_job = {r["job_id"]: r for r in rows}
    try:
        results = poll_jobs(session, list(by_job.keys()))
    except Transient as e:
        log.warning("batch poll failed transiently: %s (will retry next drain)", e)
        return

    timeout = timedelta(minutes=cfg["drain"]["submitted_timeout_minutes"])
    for jid, row in by_job.items():
        last_at = parse_iso(row["last_attempt_at"])
        stale = last_at is not None and (now() - last_at) > timeout
        res = results.get(jid)
        if res is None:
            # Job not reported. If it's been too long, requeue.
            if stale:
                _requeue(conn, row["id"], row["attempts"], cfg, "poll: job not reported")
                log.warning("%s job %s unreported past timeout; requeued", row["url"], jid)
            continue
        status = res.get("status")
        if status == "success":
            ts = res.get("timestamp")
            orig = res.get("original_url") or row["url"]
            archive_url = f"https://web.archive.org/web/{ts}/{orig}"
            conn.execute(
                "UPDATE urls SET status='archived', archive_url=?, dead_strikes=0, "
                "last_error=NULL, job_id=NULL, updated_at=? WHERE id=?",
                (archive_url, now_iso(), row["id"]),
            )
            conn.commit()
            log.info("ARCHIVED %s -> %s", row["url"], archive_url)
        elif status == "error":
            err = res.get("status_ext") or res.get("exception") or res.get("message") or "error"
            route_error(conn, row, err, cfg, dead_conf, dead_spacing)
        else:  # pending
            if stale:
                _requeue(conn, row["id"], row["attempts"], cfg, "stuck pending past timeout")
                log.warning("%s stuck pending past timeout; requeued", row["url"])
            else:
                log.debug("%s still pending (job %s)", row["url"], jid)


def _requeue_stale_for_refresh(conn, cfg):
    """Re-queue archived rows in refresh-enabled collections past their window.

    The old snapshot's archive_url is kept, so history still shows the last good
    capture until a fresh one lands. Collections without a refresh_days setting
    (the default for everything) are never touched. IA-side dedup
    (if_not_archived_within) still applies on the eventual resubmit.
    """
    collections = cfg.get("collections") or {}
    for name, opts in collections.items():
        refresh_days = opts.get("refresh_days") if isinstance(opts, dict) else None
        if not refresh_days:
            continue
        cutoff = iso(now() - timedelta(days=float(refresh_days)))
        rows = conn.execute(
            "SELECT id, url FROM urls WHERE collection=? AND status='archived' "
            "AND updated_at < ?",
            (name, cutoff),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE urls SET status='queued', next_attempt_at=NULL, updated_at=? "
                "WHERE id=?",
                (now_iso(), r["id"]),
            )
            log.info("refresh: re-queued %s (collection=%s, older than %s days)",
                     r["url"], name, refresh_days)
        if rows:
            conn.commit()


def _pick_and_submit(conn, session, cfg, slots, dead_conf=None, dead_spacing=None):
    if dead_conf is None:
        dead_conf = cfg["death"]["confirmations"]
    if dead_spacing is None:
        dead_spacing = cfg["death"]["spacing_hours"]
    cap = cfg["spn2"]["per_url_daily_attempt_cap"]
    today = today_str()
    candidates = conn.execute(
        "SELECT * FROM urls WHERE status='queued' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY COALESCE(next_attempt_at, added_at) ASC",
        (now_iso(),),
    ).fetchall()

    submitted = 0
    for row in candidates:
        if submitted >= slots:
            break
        # Reset per-day counter on date rollover.
        attempts_today = row["attempts_today"]
        if row["attempts_today_date"] != today:
            attempts_today = 0
        if attempts_today >= cap:
            log.debug("%s hit per-url daily cap (%d); skipping", row["url"], cap)
            continue
        try:
            kind, payload = submit(session, row["url"], cfg)
        except Transient as e:
            _requeue(conn, row["id"], row["attempts"], cfg, str(e), retry_after=e.retry_after)
            log.info("%s submit transient: %s; requeued", row["url"], e)
            submitted += 1  # counts against slots: we consumed an attempt on IA's side
            continue
        # Bump attempt counters regardless of outcome.
        conn.execute(
            "UPDATE urls SET attempts=attempts+1, attempts_today=?, attempts_today_date=?, "
            "last_attempt_at=?, updated_at=? WHERE id=?",
            (attempts_today + 1, today, now_iso(), now_iso(), row["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM urls WHERE id=?", (row["id"],)).fetchone()
        if kind == "submitted":
            conn.execute(
                "UPDATE urls SET status='submitted', job_id=?, updated_at=? WHERE id=?",
                (payload, now_iso(), row["id"]),
            )
            conn.commit()
            log.info("submitted %s (job %s)", row["url"], payload)
        elif kind == "already":
            archive_url, ts = wayback_lookup(session, row["url"])
            if not archive_url:
                # We know it's preserved; store the latest-capture redirect.
                archive_url = f"https://web.archive.org/web/2/{row['url']}"
            conn.execute(
                "UPDATE urls SET status='archived', archive_url=?, dead_strikes=0, "
                "last_error=NULL, job_id=NULL, updated_at=? WHERE id=?",
                (archive_url, now_iso(), row["id"]),
            )
            conn.commit()
            log.info("ALREADY ARCHIVED (server-side dedup) %s -> %s", row["url"], archive_url)
        else:  # immediate error
            route_error(conn, row, payload, cfg, dead_conf, dead_spacing)
        submitted += 1
    return submitted


def cmd_drain(args):
    cfg = load_config()
    dead_conf = (
        args.dead_confirmations
        if args.dead_confirmations is not None
        else cfg["death"]["confirmations"]
    )
    dead_spacing = (
        args.dead_spacing_hours
        if args.dead_spacing_hours is not None
        else cfg["death"]["spacing_hours"]
    )
    conn = db_connect()
    try:
        session = make_session()
        # (a) system health
        try:
            ok, raw = system_ok(session)
        except Transient as e:
            log.warning("system status unavailable: %s; skipping this drain", e)
            return 0
        if not ok:
            log.warning("IA save system not ok (%s); skipping this drain", raw)
            return 0

        # (b) poll submitted
        _poll_submitted(conn, session, cfg, dead_conf, dead_spacing)

        # (b2) refresh: re-queue stale archived rows in refresh collections
        _requeue_stale_for_refresh(conn, cfg)

        # (c) slot budget
        try:
            avail, proc = user_status(session)
        except Transient as e:
            log.warning("user status unavailable: %s; skipping submits this drain", e)
            return 0
        slots = max(0, avail - cfg["drain"]["batch_headroom"])
        log.info("slots: available=%d processing=%d headroom=%d -> submitting up to %d",
                 avail, proc, cfg["drain"]["batch_headroom"], slots)

        # (d,e) submit
        if slots > 0:
            _pick_and_submit(conn, session, cfg, slots, dead_conf, dead_spacing)
        return 0
    except Exception:
        log.exception("drain crashed")
        return 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subcommand: setup  (Phase 3)
# ---------------------------------------------------------------------------

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>historian</string>
    <string>drain</string>
  </array>
  <key>StartInterval</key>
  <integer>{interval}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{stdout}</string>
  <key>StandardErrorPath</key>
  <string>{stderr}</string>
  <key>WorkingDirectory</key>
  <string>{workdir}</string>
</dict>
</plist>
"""


def _onboard_credentials():
    """Ensure IA S3 keys exist. If missing, walk the user through adding them.

    Returns True if credentials are present (and verified when just entered),
    False if the user couldn't be onboarded.
    """
    ak, sk = read_credentials()
    if ak and sk:
        # If the keys were found only under a legacy account, migrate them to
        # this user's account so lookups stop depending on the old name.
        if not (_keychain("ia-s3-access") and _keychain("ia-s3-secret")):
            try:
                _store_key("ia-s3-access", ak)
                _store_key("ia-s3-secret", sk)
                print(f"✓ Internet Archive keys found and migrated to account '{KEYCHAIN_ACCOUNT}'.")
            except subprocess.CalledProcessError:
                print("✓ Internet Archive keys found.")
        else:
            print("✓ Internet Archive keys found.")
        return True

    if not sys.stdin.isatty():
        print("Internet Archive keys not found and no terminal to prompt on.",
              file=sys.stderr)
        print("Add them, then re-run setup:", file=sys.stderr)
        print(f"  security add-generic-password -a {KEYCHAIN_ACCOUNT} -s ia-s3-access -w <ACCESS_KEY>",
              file=sys.stderr)
        print(f"  security add-generic-password -a {KEYCHAIN_ACCOUNT} -s ia-s3-secret -w <SECRET_KEY>",
              file=sys.stderr)
        return False

    print()
    print("Let's connect your Internet Archive account so the Wayback Machine")
    print("will accept your captures. You need free S3-style API keys:")
    print()
    print("  1. Sign in (or sign up) at https://archive.org")
    print("  2. Open https://archive.org/account/s3.php")
    print("  3. Copy your Access Key and Secret Key below.")
    print()
    print("They go straight into your macOS login Keychain — never into this repo.")
    print()
    ak = input("  Access key: ").strip()
    sk = getpass.getpass("  Secret key (hidden): ").strip()
    if not ak or not sk:
        print("No keys entered; aborting setup.", file=sys.stderr)
        return False

    print("Verifying with the Internet Archive...")
    try:
        avail, _ = user_status(make_session((ak, sk)))
    except Transient as e:
        print(f"Could not verify keys ({e}). Double-check them and re-run setup.",
              file=sys.stderr)
        return False
    if avail is None:
        print("The Archive rejected those keys. Check them and re-run setup.",
              file=sys.stderr)
        return False

    _store_key("ia-s3-access", ak)
    _store_key("ia-s3-secret", sk)
    print(f"✓ Keys verified and saved to your Keychain ({avail} capture slots available).")
    return True


def _cleanup_legacy_jobs(domain):
    """Boot out and remove any older, personalized launchd installs."""
    la = Path.home() / "Library" / "LaunchAgents"
    for legacy in LEGACY_LABELS:
        subprocess.run(["launchctl", "bootout", f"{domain}/{legacy}"],
                       capture_output=True, text=True)
        old = la / f"{legacy}.plist"
        if old.exists():
            old.unlink()
            print(f"  removed legacy job: {legacy}")


def cmd_setup(args):
    print("Internet Historian — setup\n")
    # Step 1: make sure the Archive will accept captures.
    if not _onboard_credentials():
        return 1

    cfg = load_config()
    interval = cfg["drain"]["interval_seconds"]
    # Reference the interpreter actually running this (it has `requests`) and
    # invoke the module by name (`-m historian`) rather than a checkout path, so
    # an installed package and an in-place checkout both work. WorkingDirectory
    # is the directory holding historian.py, which lets `-m historian` resolve
    # even from a bare git clone that isn't pip-installed.
    python = sys.executable
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    plist = PLIST_TEMPLATE.format(
        label=LABEL,
        python=python,
        interval=interval,
        stdout=str(LOGS_DIR / "launchd.out.log"),
        stderr=str(LOGS_DIR / "launchd.err.log"),
        workdir=str(ROOT),
    )
    dest_dir = Path.home() / "Library" / "LaunchAgents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / PLIST_NAME
    dest.write_text(plist)
    log.info("wrote launchd plist -> %s (interpreter=%s -m historian, interval=%ss)",
             dest, python, interval)
    print(f"wrote plist -> {dest}")
    print(f"  runs: {python} -m historian drain")
    print(f"  StartInterval: {interval}s")

    uid = os.getuid()
    domain = f"gui/{uid}"
    # Idempotent: bootout any existing instance first, and retire older installs.
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"],
                   capture_output=True, text=True)
    _cleanup_legacy_jobs(domain)
    boot = subprocess.run(["launchctl", "bootstrap", domain, str(dest)],
                          capture_output=True, text=True)
    if boot.returncode != 0:
        print(f"bootstrap failed ({boot.stderr.strip()}); falling back to load -w")
        subprocess.run(["launchctl", "unload", str(dest)], capture_output=True, text=True)
        load = subprocess.run(["launchctl", "load", "-w", str(dest)],
                              capture_output=True, text=True)
        if load.returncode != 0:
            print(f"launchctl load failed: {load.stderr.strip()}", file=sys.stderr)
            return 1
    # Verify.
    lst = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    if LABEL in lst.stdout:
        line = next((l for l in lst.stdout.splitlines() if LABEL in l), LABEL)
        print(f"loaded: {line.strip()}")
        print()
        print("✓ Internet Historian is now running in the background. It will quietly")
        print(f"  preserve queued URLs every {interval // 60} minutes. Nothing else to do.")
        print()
        print("Try it:")
        print("    internet-historian add https://example.com")
        print("    internet-historian status")
        print()
        print("Using Claude Code? Install the conversational skill so you can just say")
        print('"archive <a url>" in any session:')
        print("    internet-historian install-skill")
        return 0
    print("WARNING: job not visible in launchctl list", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: status  (Phase 3)
# ---------------------------------------------------------------------------


def _location_kind(p: Path) -> str:
    """Label a resolved path as living in the checkout or the per-user dir."""
    try:
        p.resolve().relative_to(ROOT)
        return "repo-local"
    except ValueError:
        return "per-user"


# ---------------------------------------------------------------------------
# Pretty (rich) rendering — used by status/diagnose when the [pretty] extra is
# installed. Everything degrades to the plain-text path when rich is absent.
# ---------------------------------------------------------------------------

_STATE_STYLE = {
    "archived": "green",
    "queued": "cyan",
    "throttled": "yellow",
    "submitted": "blue",
    "paused": "magenta",
    "dead": "red",
}


def _rich_console():
    """A rich Console, or None when the [pretty] extra isn't installed."""
    if _RichConsole is None:
        return None
    return _RichConsole(highlight=False)


def _print_banner(console):
    console.print()
    console.print("  I N T E R N E T   H I S T O R I A N  ",
                  style="bold white on dark_cyan")
    console.print("  🏛  preserving the web things you love", style="dim italic")
    console.print()


def _throttled_count(conn, collection=None):
    """Queued rows currently backing off (a future next_attempt_at)."""
    sql = ("SELECT COUNT(*) c FROM urls WHERE status='queued' "
           "AND next_attempt_at IS NOT NULL AND next_attempt_at > ?")
    params = [now_iso()]
    if collection is not None:
        sql += " AND collection=?"
        params.append(collection)
    return conn.execute(sql, params).fetchone()["c"]


def _status_counts(conn):
    by = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM urls GROUP BY status")}
    throttled = _throttled_count(conn)
    return {
        "archived": by.get("archived", 0),
        "queued": max(0, by.get("queued", 0) - throttled),
        "throttled": throttled,
        "submitted": by.get("submitted", 0),
        "paused": by.get("paused", 0),
        "dead": by.get("dead", 0),
        "total": sum(by.values()),
    }


def _summary_line(console, counts):
    labels = [
        ("archived", "archived"),
        ("queued", "queued"),
        ("throttled", "throttled (will retry)"),
        ("submitted", "submitted"),
        ("paused", "paused"),
        ("dead", "dead"),
    ]
    text = _RichText()
    first = True
    for key, label in labels:
        n = counts[key]
        # Always show archived+queued; hide the rest when zero to keep it tidy.
        if n == 0 and key not in ("archived", "queued"):
            continue
        if not first:
            text.append(" · ", style="dim")
        text.append(f"{n} ", style=f"bold {_STATE_STYLE[key]}")
        text.append(label, style=_STATE_STYLE[key])
        first = False
    console.print(text)


def _cell(n, style):
    return _RichText(str(n), style=f"bold {style}") if n else _RichText("·", style="dim")


def _render_collection_table(console, conn):
    """Per-collection table with colored state chips. No-op if the queue is empty."""
    grid = {}
    for r in conn.execute(
            "SELECT collection, status, COUNT(*) c FROM urls GROUP BY collection, status"):
        grid.setdefault(r["collection"], {})[r["status"]] = r["c"]
    if not grid:
        return
    table = _RichTable(box=None, pad_edge=False, expand=False, title_justify="left")
    table.add_column("collection", style="bold")
    for label in ("archived", "queued", "throttled", "submitted", "dead"):
        table.add_column(label, justify="right")
    for name in sorted(grid):
        g = grid[name]
        throttled = _throttled_count(conn, name)
        queued = max(0, g.get("queued", 0) - throttled)
        table.add_row(
            name,
            _cell(g.get("archived", 0), _STATE_STYLE["archived"]),
            _cell(queued, _STATE_STYLE["queued"]),
            _cell(throttled, _STATE_STYLE["throttled"]),
            _cell(g.get("submitted", 0), _STATE_STYLE["submitted"]),
            _cell(g.get("dead", 0), _STATE_STYLE["dead"]),
        )
    console.print(table)


def _render_status_rich(console, conn):
    _print_banner(console)
    _summary_line(console, _status_counts(conn))
    console.print()

    console.print(f"[dim]queue.db[/]  {DB_PATH}  [dim]({_location_kind(DB_PATH)})[/]")
    console.print(f"[dim]logs[/]      {LOGS_DIR}  [dim]({_location_kind(LOGS_DIR)})[/]")
    cfg_note = "loaded" if CONFIG_PATH.exists() else "using built-in defaults"
    console.print(f"[dim]config[/]    {CONFIG_PATH}  [dim]({cfg_note})[/]")
    console.print()
    _render_collection_table(console, conn)

    oldest = conn.execute(
        "SELECT url, added_at FROM urls WHERE status='queued' "
        "ORDER BY added_at ASC LIMIT 1"
    ).fetchone()
    if oldest:
        console.print(f"\n[dim]oldest waiting:[/] {oldest['url']} "
                      f"[dim](queued {human_age(oldest['added_at'])} ago)[/]")

    dead = conn.execute(
        "SELECT url, dead_reason FROM urls WHERE status='dead' ORDER BY updated_at DESC"
    ).fetchall()
    if dead:
        console.print(f"\n[red]Dead links ({len(dead)})[/] — the target answered and it was bad:")
        for r in dead:
            console.print(f"  [red]•[/] {r['url']}  [dim]({r['dead_reason']})[/]")

    conn.close()
    try:
        session = make_session()
        avail, proc = user_status(session)
        console.print(f"\n[dim]IA capture slots right now:[/] "
                      f"[green]{avail}[/] available, [blue]{proc}[/] processing")
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[dim](IA slot check unavailable: {e})[/]")


def _render_diagnose_rich(console, conn, rows, dead_conf):
    _print_banner(console)
    _summary_line(console, _status_counts(conn))
    console.print()
    _render_collection_table(console, conn)
    console.print()
    console.print(
        "[dim]A throttled URL is NOT a failure. IA throttles under load as a matter of "
        "course; the queue retries forever with backoff. Only a page whose own server "
        f"keeps answering badly, confirmed {dead_conf}× a day apart, is ever marked dead.[/]"
    )
    console.print()
    if not rows:
        console.print("[green]No problem URLs.[/] Everything is archived or waiting its turn.")
        return
    cat_style = {"dead": "red", "throttle": "yellow", "candidate": "yellow",
                 "transient": "cyan", "stuck": "bold red"}
    table = _RichTable(expand=True)
    table.add_column("URL", overflow="fold", style="bold")
    table.add_column("state", no_wrap=True)
    table.add_column("verdict", overflow="fold")
    for r in rows:
        verdict, cat = _diagnose_verdict(r, dead_conf)
        style = cat_style.get(cat, "white")
        table.add_row(r["url"], _RichText(r["status"], style=style),
                      _RichText(verdict, style=style))
    console.print(table)


def cmd_status(args):
    conn = db_connect()
    console = _rich_console()
    if console is not None:
        _render_status_rich(console, conn)
        return 0
    print("Internet Historian — preserving the web things you love.\n")

    print("State lives in:")
    print(f"  queue.db: {DB_PATH}  ({_location_kind(DB_PATH)})")
    print(f"  logs:     {LOGS_DIR}  ({_location_kind(LOGS_DIR)})")
    cfg_note = "loaded" if CONFIG_PATH.exists() else "not present — using built-in defaults"
    print(f"  config:   {CONFIG_PATH}  ({cfg_note})")
    print()

    by_status = conn.execute(
        "SELECT status, COUNT(*) c FROM urls GROUP BY status ORDER BY status"
    ).fetchall()
    total = sum(r["c"] for r in by_status)
    print(f"URLs tracked: {total}")
    if by_status:
        parts = ", ".join(f"{r['status']}={r['c']}" for r in by_status)
        print(f"  {parts}")

    by_coll = conn.execute(
        "SELECT collection, COUNT(*) c FROM urls GROUP BY collection ORDER BY c DESC"
    ).fetchall()
    if by_coll:
        print("\nCollections:")
        for r in by_coll:
            arch = conn.execute(
                "SELECT COUNT(*) c FROM urls WHERE collection=? AND status='archived'",
                (r["collection"],),
            ).fetchone()["c"]
            print(f"  {r['collection']}: {r['c']} ({arch} archived)")

    oldest = conn.execute(
        "SELECT url, added_at FROM urls WHERE status='queued' "
        "ORDER BY added_at ASC LIMIT 1"
    ).fetchone()
    if oldest:
        print(f"\nOldest waiting: {oldest['url']} (queued {human_age(oldest['added_at'])} ago)")

    dead = conn.execute(
        "SELECT url, dead_reason FROM urls WHERE status='dead' ORDER BY updated_at DESC"
    ).fetchall()
    if dead:
        print(f"\nDead links ({len(dead)}) — the target answered and it was bad:")
        for r in dead:
            print(f"  {r['url']}  ({r['dead_reason']})")

    errs = conn.execute(
        "SELECT url, last_error, updated_at FROM urls "
        "WHERE last_error IS NOT NULL AND status!='archived' "
        "ORDER BY updated_at DESC LIMIT 5"
    ).fetchall()
    if errs:
        print("\nMost recent errors:")
        for r in errs:
            print(f"  [{human_age(r['updated_at'])} ago] {r['url']}: {r['last_error']}")

    conn.close()

    # Live IA slots (best-effort; don't fail status if IA is unreachable).
    try:
        session = make_session()
        avail, proc = user_status(session)
        print(f"\nIA capture slots right now: {avail} available, {proc} processing")
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n(IA slot check unavailable: {e})")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: diagnose  (Phase 3)
# ---------------------------------------------------------------------------


def _diagnose_verdict(r, dead_conf):
    """Return (verdict_text, category) for a problem row. Category drives color."""
    if r["status"] == "dead":
        return (f"DEAD LINK — {r['dead_reason']} (the target itself is gone/blocked)",
                "dead")
    kind = classify(r["last_error"] or "")
    if kind in ("throttle", "daily"):
        return ("throttled (IA-side) — will retry automatically, nothing to do",
                "throttle")
    if kind == "dead":
        return (f"likely dead link (strikes: {r['dead_strikes']}/{dead_conf}) — "
                "confirming before giving up", "candidate")
    nxt = parse_iso(r["next_attempt_at"])
    if nxt and nxt > now():
        return "transient error — backing off, will retry automatically", "transient"
    return "stuck — investigate (unclassified error, not backing off)", "stuck"


def cmd_diagnose(args):
    conn = db_connect()
    cfg = load_config()
    dead_conf = cfg["death"]["confirmations"]
    rows = conn.execute(
        "SELECT * FROM urls WHERE status IN ('queued','submitted','dead') "
        "AND (last_error IS NOT NULL OR status='dead') "
        "ORDER BY updated_at DESC"
    ).fetchall()

    console = _rich_console()
    if console is not None:
        _render_diagnose_rich(console, conn, rows, dead_conf)
        conn.close()
        return 0

    print("Internet Historian — diagnosis\n")
    print("Reminder: a throttled URL is NOT a failure. IA's Save-Page-Now API")
    print("throttles under load as a matter of course; the queue retries forever")
    print("with backoff. Only a URL whose own server keeps answering badly")
    print("(404, DNS failure, blocked) ever gets marked dead — and only after")
    print(f"{dead_conf} confirmations spaced a day apart.\n")

    if not rows:
        print("No problem URLs. Everything is either archived or waiting its turn.")
        conn.close()
        return 0

    for r in rows:
        verdict, _ = _diagnose_verdict(r, dead_conf)
        print(f"• {r['url']}")
        print(f"    status={r['status']} attempts={r['attempts']} last_error={r['last_error']}")
        print(f"    -> {verdict}")
    conn.close()
    return 0


# ---------------------------------------------------------------------------
# Subcommands: pause / resume  (Phase 3)
# ---------------------------------------------------------------------------


def _select_ids(conn, args):
    if args.collection:
        return conn.execute(
            "SELECT id, url FROM urls WHERE collection=?", (args.collection,)
        ).fetchall()
    ids = []
    for u in args.urls or []:
        norm = normalize_url(u)
        row = conn.execute("SELECT id, url FROM urls WHERE url=?", (norm,)).fetchone()
        if row:
            ids.append(row)
        else:
            print(f"  not tracked: {norm}", file=sys.stderr)
    return ids


def cmd_pause(args):
    conn = db_connect()
    rows = _select_ids(conn, args)
    n = 0
    for r in rows:
        conn.execute(
            "UPDATE urls SET status='paused', job_id=NULL, updated_at=? WHERE id=?",
            (now_iso(), r["id"]),
        )
        n += 1
        log.info("paused %s", r["url"])
    conn.commit()
    conn.close()
    print(f"paused {n}")
    return 0


def cmd_resume(args):
    conn = db_connect()
    rows = _select_ids(conn, args)
    n = 0
    for r in rows:
        conn.execute(
            "UPDATE urls SET status='queued', next_attempt_at=NULL, updated_at=? "
            "WHERE id=? AND status='paused'",
            (now_iso(), r["id"]),
        )
        n += conn.total_changes and 1 or 0
        log.info("resumed %s", r["url"])
    conn.commit()
    conn.close()
    print(f"resumed {n}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: install-skill
# ---------------------------------------------------------------------------


def cmd_install_skill(args):
    """Install the Claude Code skill into ~/.claude/skills/internet-historian.

    Symlinks the packaged SKILL.md when the OS allows it (so edits to a checkout
    stay live); falls back to copying (e.g. on Windows without symlink rights).
    SKILL.md ships beside historian.py in both a checkout and an installed
    wheel, so ROOT/SKILL.md resolves either way.
    """
    src = ROOT / "SKILL.md"
    if not src.exists():
        print(f"SKILL.md not found next to historian.py ({src}).", file=sys.stderr)
        return 1
    dest_dir = Path.home() / ".claude" / "skills" / "internet-historian"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "SKILL.md"
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        dest.symlink_to(src)
        how = "symlinked"
    except OSError:
        shutil.copy2(src, dest)
        how = "copied"
    log.info("install-skill: %s %s -> %s", how, src, dest)
    print(f"✓ Skill installed ({how}): {dest}")
    print('  In any Claude Code session you can now say e.g. "archive example.com".')
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="historian",
        description="Internet Historian — quietly preserve the web things you love.",
    )
    p.add_argument("--verbose", action="store_true", help="DEBUG logging")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="show IA capture-slot availability and system health")

    a = sub.add_parser("add", help="queue URLs for preservation")
    a.add_argument("urls", nargs="*")
    a.add_argument("--collection", default="default")
    a.add_argument("--file", help="text file of URLs, one per line")
    a.add_argument("--bookmarks", help="a browser's exported bookmarks HTML file")
    a.add_argument("--folder", help="with --bookmarks: only import this folder's subtree")

    dis = sub.add_parser(
        "discover",
        help="find a subject's official pages via Wikipedia/Wikidata, then queue them",
    )
    dis.add_argument("term", help="what to look up, e.g. \"Chiikawa\"")
    dis.add_argument("--collection", default=None,
                     help="collection to queue into (defaults to a slug of TERM)")
    dis.add_argument("--lang", default="en",
                     help="Wikipedia language edition to search (default: en)")

    d = sub.add_parser("drain", help="run one archiving cycle (launchd calls this)")
    d.add_argument("--dead-confirmations", type=int, default=None)
    d.add_argument("--dead-spacing-hours", type=float, default=None)

    sub.add_parser("setup", help="install the background launchd job")
    sub.add_parser("install-skill", help="install the Claude Code skill into ~/.claude/skills")
    sub.add_parser("status", help="human-readable queue overview")
    sub.add_parser("diagnose", help="explain why problem URLs aren't archived yet")

    pa = sub.add_parser("pause", help="stop trying a URL or collection")
    pa.add_argument("urls", nargs="*")
    pa.add_argument("--collection")

    re = sub.add_parser("resume", help="re-queue a paused URL or collection")
    re.add_argument("urls", nargs="*")
    re.add_argument("--collection")

    return p


COMMANDS = {
    "check": cmd_check,
    "add": cmd_add,
    "discover": cmd_discover,
    "drain": cmd_drain,
    "setup": cmd_setup,
    "install-skill": cmd_install_skill,
    "status": cmd_status,
    "diagnose": cmd_diagnose,
    "pause": cmd_pause,
    "resume": cmd_resume,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    return COMMANDS[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
