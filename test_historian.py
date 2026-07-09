"""Unit tests for Internet Historian's failure semantics.

Run: python3 -m unittest test_historian.py -v

The HTTP layer is mocked throughout — no real network calls.
"""

import contextlib
import copy
import io
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import historian


def make_cfg():
    return copy.deepcopy(historian.DEFAULTS)


def fake_response(status_code=200, json_data=None, headers=None):
    r = mock.Mock()
    r.status_code = status_code
    r.headers = headers or {}
    r.json.return_value = json_data if json_data is not None else {}
    return r


class HistorianTestCase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)  # keep test output clean
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Redirect the module's DB to an isolated temp file.
        self._orig_db = historian.DB_PATH
        self._orig_data = historian.DATA_DIR
        historian.DATA_DIR = tmp
        historian.DB_PATH = tmp / "queue.db"
        self.conn = historian.db_connect()
        self.cfg = make_cfg()

    def tearDown(self):
        self.conn.close()
        historian.DB_PATH = self._orig_db
        historian.DATA_DIR = self._orig_data
        self._tmp.cleanup()
        logging.disable(logging.NOTSET)

    # -- helpers ----------------------------------------------------------

    def insert(self, url="https://t.example/x", **over):
        cols = dict(
            collection="default",
            status="queued",
            attempts=0,
            attempts_today=0,
            attempts_today_date=None,
            dead_strikes=0,
            last_dead_strike_at=None,
            next_attempt_at=None,
        )
        cols.update(over)
        self.conn.execute(
            "INSERT INTO urls (url, collection, status, attempts, attempts_today, "
            "attempts_today_date, dead_strikes, last_dead_strike_at, next_attempt_at, "
            "added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                url, cols["collection"], cols["status"], cols["attempts"],
                cols["attempts_today"], cols["attempts_today_date"], cols["dead_strikes"],
                cols["last_dead_strike_at"], cols["next_attempt_at"],
                historian.now_iso(), historian.now_iso(),
            ),
        )
        self.conn.commit()
        return self.row(url)

    def row(self, url="https://t.example/x"):
        return self.conn.execute("SELECT * FROM urls WHERE url=?", (url,)).fetchone()

    # -- (a) 429 requeues with future next_attempt_at, zero dead strikes ---

    def test_429_requeues_without_dead_strike(self):
        row = self.insert()
        session = mock.Mock()
        # A 429 makes submit() raise Transient before it ever parses JSON.
        session.post.return_value = fake_response(status_code=429, headers={})
        historian._pick_and_submit(self.conn, session, self.cfg, slots=1)

        r = self.row()
        self.assertEqual(r["status"], "queued")
        self.assertIsNotNone(r["next_attempt_at"])
        nxt = historian.parse_iso(r["next_attempt_at"])
        self.assertGreater(nxt, historian.now())          # scheduled in the future
        self.assertEqual(r["dead_strikes"], 0)            # throttle never kills

    # -- (b) two not-found within the spacing window == one strike --------

    def test_two_notfound_within_window_counts_one_strike(self):
        self.insert()
        # First strike.
        historian.route_error(
            self.conn, self.row(), "error:not-found", self.cfg,
            dead_conf=3, dead_spacing=24,
        )
        self.assertEqual(self.row()["dead_strikes"], 1)
        # Second, immediately (well within 24h) — must NOT add a strike.
        historian.route_error(
            self.conn, self.row(), "error:not-found", self.cfg,
            dead_conf=3, dead_spacing=24,
        )
        r = self.row()
        self.assertEqual(r["dead_strikes"], 1)
        self.assertEqual(r["status"], "queued")

    # -- (c) three properly-spaced strikes -> dead ------------------------

    def test_three_spaced_strikes_marks_dead(self):
        self.insert()
        for i in range(3):
            historian.route_error(
                self.conn, self.row(), "error:invalid-host-resolution", self.cfg,
                dead_conf=3, dead_spacing=24,
            )
            # Simulate >24h passing before the next strike.
            back = historian.iso(historian.now() - timedelta(hours=25))
            self.conn.execute(
                "UPDATE urls SET last_dead_strike_at=? WHERE url=?",
                (back, "https://t.example/x"),
            )
            self.conn.commit()
        r = self.row()
        self.assertEqual(r["status"], "dead")
        self.assertIn("host-resolution", r["dead_reason"])

    # -- (d) per-URL daily cap blocks 6th attempt, resets next day --------

    def test_daily_cap_blocks_then_resets(self):
        today = historian.today_str()
        # Already at the cap (5) today.
        self.insert(attempts=5, attempts_today=5, attempts_today_date=today)
        session = mock.Mock()
        session.post.return_value = fake_response(200, {"job_id": "JOB1"})

        historian._pick_and_submit(self.conn, session, self.cfg, slots=5)
        session.post.assert_not_called()                  # capped -> never submitted
        self.assertEqual(self.row()["status"], "queued")

        # Roll the counter's date back to yesterday: cap should reset.
        self.conn.execute(
            "UPDATE urls SET attempts_today_date='2000-01-01' WHERE url=?",
            ("https://t.example/x",),
        )
        self.conn.commit()
        historian._pick_and_submit(self.conn, session, self.cfg, slots=5)
        session.post.assert_called_once()                 # now allowed
        r = self.row()
        self.assertEqual(r["status"], "submitted")
        self.assertEqual(r["job_id"], "JOB1")
        self.assertEqual(r["attempts_today"], 1)          # counter reset then bumped

    # -- bonus: daily-cap error defers to tomorrow, no strike -------------

    def test_daily_capture_error_defers_no_strike(self):
        self.insert()
        historian.route_error(
            self.conn, self.row(), "error:too-many-daily-captures", self.cfg,
            dead_conf=3, dead_spacing=24,
        )
        r = self.row()
        self.assertEqual(r["status"], "queued")
        self.assertEqual(r["dead_strikes"], 0)
        self.assertGreater(historian.parse_iso(r["next_attempt_at"]), historian.now())

    # -- bonus: server-side dedup is a success, not an error --------------

    def test_dedup_response_marks_archived(self):
        self.insert()
        session = mock.Mock()
        session.post.return_value = fake_response(
            200,
            {
                "url": "https://t.example/x",
                "job_id": None,
                "message": "The same snapshot had been made 10 hours ago. "
                "You can make new capture of this URL after 720 hours.",
            },
        )
        # No existing snapshot resolvable -> falls back to latest-capture redirect.
        session.get.return_value = fake_response(200, {"archived_snapshots": {}})
        historian._pick_and_submit(self.conn, session, self.cfg, slots=1)
        r = self.row()
        self.assertEqual(r["status"], "archived")
        self.assertIsNotNone(r["archive_url"])

    # -- periodic recapture: stale archived rows re-queue, fresh ones don't --

    def test_refresh_requeues_only_stale_rows_in_refresh_collections(self):
        cfg = make_cfg()
        cfg["collections"] = {"chiikawa": {"refresh_days": 30}}

        def archived(url, collection, days_ago, archive_url=None):
            self.insert(url, collection=collection, status="archived")
            self.conn.execute(
                "UPDATE urls SET updated_at=?, archive_url=? WHERE url=?",
                (historian.iso(historian.now() - timedelta(days=days_ago)),
                 archive_url, url),
            )
            self.conn.commit()

        # stale, in a refresh collection -> should re-queue (keeping archive_url)
        archived("https://stale", "chiikawa", 40, "https://web.archive.org/web/1/https://stale")
        # young, in a refresh collection -> stays archived
        archived("https://fresh", "chiikawa", 5)
        # ancient, but its collection has no refresh_days -> stays archived
        archived("https://other", "default", 400)

        historian._requeue_stale_for_refresh(self.conn, cfg)

        stale = self.row("https://stale")
        self.assertEqual(stale["status"], "queued")
        self.assertEqual(stale["archive_url"],
                         "https://web.archive.org/web/1/https://stale")  # last snapshot kept
        self.assertIsNone(stale["next_attempt_at"])                      # due immediately
        self.assertEqual(self.row("https://fresh")["status"], "archived")
        self.assertEqual(self.row("https://other")["status"], "archived")


class ConfigOverlayTests(unittest.TestCase):
    """config.local.toml is merged on top of config.toml at load time."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig_config = historian.CONFIG_PATH
        self._orig_local = historian.LOCAL_CONFIG_PATH
        historian.CONFIG_PATH = tmp / "config.toml"
        historian.LOCAL_CONFIG_PATH = tmp / "config.local.toml"

    def tearDown(self):
        historian.CONFIG_PATH = self._orig_config
        historian.LOCAL_CONFIG_PATH = self._orig_local
        self._tmp.cleanup()

    def test_local_overlay_merges_and_wins(self):
        historian.CONFIG_PATH.write_text(
            '[drain]\ninterval_seconds = 300\n'
            '[collections]\nchiikawa = { refresh_days = 30 }\n'
        )
        historian.LOCAL_CONFIG_PATH.write_text(
            '[drain]\ninterval_seconds = 60\n'
            '[collections]\npersonal = { refresh_days = 90 }\n'
        )
        cfg = historian.load_config()
        self.assertEqual(cfg["drain"]["interval_seconds"], 60)  # local wins
        self.assertEqual(cfg["collections"]["chiikawa"], {"refresh_days": 30})
        self.assertEqual(cfg["collections"]["personal"], {"refresh_days": 90})
        # untouched sections keep built-in defaults
        self.assertEqual(cfg["death"]["confirmations"], 3)

    def test_missing_files_fall_back_to_defaults(self):
        cfg = historian.load_config()
        self.assertEqual(cfg["drain"]["interval_seconds"], 600)

    def test_local_only_works_without_main_config(self):
        historian.LOCAL_CONFIG_PATH.write_text(
            '[collections]\npersonal = { refresh_days = 90 }\n'
        )
        cfg = historian.load_config()
        self.assertEqual(cfg["collections"]["personal"], {"refresh_days": 90})


class NonMacBehaviorTests(unittest.TestCase):
    """On Linux/Windows every dead end must be a friendly message, not a traceback."""

    def test_setup_declines_politely_off_macos(self):
        out = io.StringIO()
        with mock.patch("sys.platform", "linux"), contextlib.redirect_stdout(out):
            rc = historian.cmd_setup(mock.Mock())
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("cron", text)
        self.assertIn("IA_ACCESS_KEY", text)
        self.assertNotIn("Traceback", text)

    def test_missing_keys_message_skips_keychain_advice_off_macos(self):
        err = io.StringIO()
        with mock.patch("sys.platform", "linux"), \
                mock.patch.object(historian, "read_credentials",
                                  return_value=(None, None)), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                historian.get_credentials()
        self.assertEqual(cm.exception.code, 1)
        text = err.getvalue()
        self.assertIn("IA_ACCESS_KEY", text)
        self.assertNotIn("security add-generic-password", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
