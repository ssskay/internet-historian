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
import sqlite3
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
DB_PATH = DATA_DIR / "queue.db"
LOG_PATH = LOGS_DIR / "historian.log"
CONFIG_PATH = ROOT / "config.toml"

# The current OS user owns the Keychain items and the launchd job. Nothing is
# hardcoded to any one person, so this repo works for whoever installs it.
USER = getpass.getuser()
KEYCHAIN_ACCOUNT = os.environ.get("IA_KEYCHAIN_ACCOUNT", USER)

LABEL = "com.internet-historian.drain"
PLIST_NAME = f"{LABEL}.plist"
PLIST_PATH = ROOT / PLIST_NAME
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
    except subprocess.CalledProcessError:
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
            "  Run `python3 historian.py setup` to walk through adding them,\n"
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


def _collect_urls(args):
    urls = list(args.urls or [])
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    return urls


def cmd_add(args):
    raw = _collect_urls(args)
    if not raw:
        print("Nothing to add (give URLs and/or --file).", file=sys.stderr)
        return 1
    conn = db_connect()
    added = skipped = 0
    for r in raw:
        norm = normalize_url(r)
        if not urlsplit(norm).netloc:
            log.warning("skipping unparseable url: %r", r)
            skipped += 1
            continue
        try:
            conn.execute(
                "INSERT INTO urls (url, collection, status, added_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?)",
                (norm, args.collection, now_iso(), now_iso()),
            )
            conn.commit()
            added += 1
            log.info("added %s [collection=%s]", norm, args.collection)
        except sqlite3.IntegrityError:
            skipped += 1
            existing = conn.execute(
                "SELECT status FROM urls WHERE url=?", (norm,)
            ).fetchone()
            log.info("already tracked, status=%s: %s", existing["status"], norm)
    total_queued = conn.execute(
        "SELECT COUNT(*) c FROM urls WHERE status IN ('queued','submitted')"
    ).fetchone()["c"]
    conn.close()
    print(f"added {added}, skipped {skipped}, {total_queued} in flight (queued+submitted)")
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
    <string>{script}</string>
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
    # Use the interpreter actually running this (it has `requests`), NOT /usr/bin/python3.
    python = sys.executable
    plist = PLIST_TEMPLATE.format(
        label=LABEL,
        python=python,
        script=str(ROOT / "historian.py"),
        interval=interval,
        stdout=str(LOGS_DIR / "launchd.out.log"),
        stderr=str(LOGS_DIR / "launchd.err.log"),
        workdir=str(ROOT),
    )
    PLIST_PATH.write_text(plist)
    dest_dir = Path.home() / "Library" / "LaunchAgents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / PLIST_NAME
    dest.write_text(plist)
    print(f"wrote plist -> {dest}")
    print(f"  interpreter: {python}")
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
        print(f"    python3 {ROOT / 'historian.py'} add https://example.com")
        print(f"    python3 {ROOT / 'historian.py'} status")
        skills_dir = Path.home() / ".claude" / "skills"
        if skills_dir.exists():
            print()
            print("Using Claude Code? Install the conversational skill so you can just say")
            print('"archive <a url>" in any session:')
            print(f"    ln -sfn {ROOT} {skills_dir / 'internet-historian'}")
        return 0
    print("WARNING: job not visible in launchctl list", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: status  (Phase 3)
# ---------------------------------------------------------------------------


def cmd_status(args):
    conn = db_connect()
    print("Internet Historian — preserving the web things you love.\n")

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


def cmd_diagnose(args):
    conn = db_connect()
    cfg = load_config()
    dead_conf = cfg["death"]["confirmations"]
    rows = conn.execute(
        "SELECT * FROM urls WHERE status IN ('queued','submitted','dead') "
        "AND (last_error IS NOT NULL OR status='dead') "
        "ORDER BY updated_at DESC"
    ).fetchall()

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
        if r["status"] == "dead":
            verdict = f"DEAD LINK — {r['dead_reason']} (the target itself is gone/blocked)"
        else:
            kind = classify(r["last_error"] or "")
            if kind in ("throttle", "daily"):
                verdict = "throttled (IA-side) — will retry automatically, nothing to do"
            elif kind == "dead":
                verdict = (
                    f"likely dead link (strikes: {r['dead_strikes']}/{dead_conf}) — "
                    "confirming before giving up"
                )
            else:
                nxt = parse_iso(r["next_attempt_at"])
                if nxt and nxt > now():
                    verdict = "transient error — backing off, will retry automatically"
                else:
                    verdict = "stuck — investigate (unclassified error, not backing off)"
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

    d = sub.add_parser("drain", help="run one archiving cycle (launchd calls this)")
    d.add_argument("--dead-confirmations", type=int, default=None)
    d.add_argument("--dead-spacing-hours", type=float, default=None)

    sub.add_parser("setup", help="install the background launchd job")
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
    "drain": cmd_drain,
    "setup": cmd_setup,
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
