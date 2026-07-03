"""Unit tests for Internet Historian's failure semantics.

Run: python3 -m unittest test_historian.py -v

The HTTP layer is mocked throughout — no real network calls.
"""

import copy
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
