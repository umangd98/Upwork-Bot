"""Heartbeat: poll records, periodic summary, startup message, failure alerts."""
import os
import time
import unittest
from unittest import mock

import test_flow  # fake SDK + temp DB + helpers
from test_flow import AUTH, FILTERS, GOOD, job

import config
import db
import filters as search_filters
import heartbeat
import pipeline
import scheduler
from dashboard import app as flask_app

HB_FILTERS = {**FILTERS, "apply": {**FILTERS["apply"], "heartbeat_hours": 6, "error_alert_hours": 2}}


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        heartbeat.reset()
        self.sent = []
        for p in (mock.patch("events.post", side_effect=lambda x: self.sent.append(x) or True),
                  mock.patch("api_routes._filters", return_value=HB_FILTERS)):
            p.start()
            self.addCleanup(p.stop)
        config.AUTO_APPLY_ENABLED = False
        config.APPLY_BACKEND = "api"

    def kinds(self):
        return [p["event"] for p in self.sent]

    def age_last_beat(self, hours):
        heartbeat._state["last_beat_at"] = time.time() - hours * 3600


class TestRecording(Base):
    def test_poll_records_and_snapshot(self):
        heartbeat.record_poll(10, 3, 3, 4.26)
        snap = heartbeat.snapshot()
        self.assertTrue(snap["last_poll_ok"])
        self.assertEqual(snap["last_poll_seconds"], 4.3)
        self.assertEqual(snap["since_last_heartbeat"], {"polls": 1, "errors": 0, "matched": 10, "new": 3, "handled": 3})
        heartbeat.record_poll(0, 0, 0, 1.0, error="boom")
        snap = heartbeat.snapshot()
        self.assertFalse(snap["last_poll_ok"])
        self.assertEqual(snap["last_error"], "boom")
        self.assertEqual(snap["since_last_heartbeat"]["polls"], 2)
        self.assertEqual(snap["since_last_heartbeat"]["errors"], 1)

    def test_status_endpoint_includes_the_heartbeat(self):
        heartbeat.record_poll(5, 1, 1, 2.0)
        body = flask_app.test_client().get("/api/status", headers=AUTH).get_json()
        self.assertTrue(body["heartbeat"]["last_poll_ok"])
        self.assertIsNotNone(body["heartbeat"]["last_poll_at"])
        self.assertEqual(body["heartbeat"]["since_last_heartbeat"]["matched"], 5)


class TestPeriodicSummary(Base):
    def test_not_sent_before_the_interval(self):
        self.age_last_beat(5.9)
        self.assertFalse(heartbeat.maybe_beat(HB_FILTERS))
        self.assertEqual(self.sent, [])

    def test_sent_when_due_with_the_periods_numbers_then_window_resets(self):
        db.add_application("a", "approval", "A", job("a", amount=4000), "L", 4000.0)
        db.add_application("b", "auto", "B", job("b", amount=900), "L", 900.0, status="skipped")
        heartbeat.record_poll(40, 5, 5, 3.0)
        self.age_last_beat(6.1)
        self.assertTrue(heartbeat.maybe_beat(HB_FILTERS))
        p = self.sent[-1]
        self.assertEqual(p["event"], "heartbeat")
        self.assertEqual(p["window"], {"polls": 1, "errors": 0, "matched": 40, "new": 5, "handled": 5})
        self.assertEqual(p["outcomes"], {"pending": 1, "skipped": 1})
        self.assertEqual(p["pending_total"], 1)
        self.assertEqual(p["auto_apply"], "dry-run (nothing is sent)")
        self.assertEqual((p["daily_cap"], p["applied_in_last_24h"]), (3, 0))      # the test filters use a cap of 3
        self.assertTrue(24 > p["period_hours"] >= 6.0)
        # window resets and the next beat is a full interval away
        self.assertEqual(heartbeat.snapshot()["since_last_heartbeat"]["polls"], 0)
        self.assertFalse(heartbeat.maybe_beat(HB_FILTERS))

    def test_failed_delivery_keeps_the_window_for_the_next_try(self):
        heartbeat.record_poll(4, 1, 1, 1.0)
        self.age_last_beat(7)
        with mock.patch("events.post", return_value=False):
            self.assertFalse(heartbeat.maybe_beat(HB_FILTERS))
        self.assertEqual(heartbeat.snapshot()["since_last_heartbeat"]["matched"], 4)

    def test_zero_hours_turns_it_off_but_force_still_works(self):
        off = {**HB_FILTERS, "apply": {**HB_FILTERS["apply"], "heartbeat_hours": 0}}
        self.age_last_beat(100)
        self.assertFalse(heartbeat.maybe_beat(off))
        self.assertTrue(heartbeat.maybe_beat(off, force=True))

    def test_auto_apply_state_is_reported(self):
        config.AUTO_APPLY_ENABLED = True
        heartbeat.maybe_beat(HB_FILTERS, force=True)
        self.assertEqual(self.sent[-1]["auto_apply"], "ON")


class TestStartupAndErrors(Base):
    def test_started_message_resets_state_and_reports_settings(self):
        heartbeat.record_poll(9, 9, 9, 1.0)
        self.assertTrue(heartbeat.send_started(HB_FILTERS))
        p = self.sent[-1]
        self.assertEqual(p["event"], "started")
        self.assertEqual((p["poll_minutes"], p["backend"]), (config.POLL_INTERVAL_MINUTES, "api"))
        self.assertEqual(heartbeat.snapshot()["since_last_heartbeat"]["polls"], 0)

    def test_error_alerts_are_throttled(self):
        self.assertTrue(heartbeat.alert_error("SearchError: login expired " + "x" * 900, HB_FILTERS))
        self.assertFalse(heartbeat.alert_error("again", HB_FILTERS))
        self.assertEqual(self.kinds(), ["poll_error"])
        self.assertLessEqual(len(self.sent[0]["error"]), 500)
        heartbeat._state["last_error_alert_at"] = time.time() - 2.1 * 3600
        self.assertTrue(heartbeat.alert_error("later", HB_FILTERS))

    def test_a_broken_webhook_never_crashes_the_bot(self):
        with mock.patch("events.post", side_effect=RuntimeError("n8n down")):
            self.assertFalse(heartbeat.send_started(HB_FILTERS))
            self.assertFalse(heartbeat.maybe_beat(HB_FILTERS, force=True))

    def test_scheduler_schedules_the_started_message(self):
        added = []

        class FakeScheduler:
            def add_listener(self, *a, **k): pass
            def add_job(self, func, **k): added.append(k.get("id"))
            def start(self): pass
        with mock.patch.object(scheduler, "BackgroundScheduler", FakeScheduler):
            scheduler.start_scheduler()
        self.assertIn("heartbeat_started", added)
        self.assertIn("upwork_poll", added)


class TestPollCycle(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch("config.load_filters", return_value=HB_FILTERS),
                  mock.patch("applied_jobs.check_jobs", return_value=True),
                  mock.patch("applied_jobs.refresh_recent", return_value=True),
                  mock.patch("notifier.notify_jobs", return_value=0),
                  mock.patch("cover_letter.draft", return_value=dict(GOOD))):
            p.start()
            self.addCleanup(p.stop)

    def test_successful_poll_is_recorded(self):
        with mock.patch("filters.search_jobs", return_value=[job("p1", amount=4000), job("p2", amount=4000)]):
            scheduler.poll_cycle()
        w = heartbeat.snapshot()["since_last_heartbeat"]
        self.assertEqual((w["polls"], w["matched"], w["new"], w["handled"], w["errors"]), (1, 2, 2, 2, 0))
        self.assertTrue(heartbeat.snapshot()["last_poll_ok"])
        self.assertNotIn("poll_error", self.kinds())

    def test_a_quiet_poll_is_still_recorded(self):
        with mock.patch("filters.search_jobs", return_value=[]):
            scheduler.poll_cycle()
        snap = heartbeat.snapshot()
        self.assertEqual(snap["since_last_heartbeat"]["polls"], 1)
        self.assertTrue(snap["last_poll_ok"])

    def test_search_failure_is_recorded_and_alerted_once(self):
        err = search_filters.SearchError("Upwork rejected the job search: token expired")
        with mock.patch("filters.search_jobs", side_effect=err):
            scheduler.poll_cycle()
            scheduler.poll_cycle()
        snap = heartbeat.snapshot()
        self.assertFalse(snap["last_poll_ok"])
        self.assertIn("token expired", snap["last_error"])
        self.assertEqual(snap["since_last_heartbeat"]["errors"], 2)
        self.assertEqual(self.kinds().count("poll_error"), 1)           # throttled

    def test_a_due_heartbeat_goes_out_with_the_poll(self):
        self.age_last_beat(6.5)
        with mock.patch("filters.search_jobs", return_value=[]):
            scheduler.poll_cycle()
        self.assertEqual(self.kinds(), ["heartbeat"])


class TestSearchFailuresAreNotSilent(unittest.TestCase):
    def _search(self, responses):
        calls = iter(responses)

        def fake(client, query, variables):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("filters.get_client", return_value=object()), \
             mock.patch("filters.execute_graphql", side_effect=fake):
            return search_filters.search_jobs()

    def test_first_page_exception_raises(self):
        with self.assertRaises(search_filters.SearchError):
            self._search([ConnectionError("down")])

    def test_first_page_graphql_error_without_data_raises(self):
        with self.assertRaisesRegex(search_filters.SearchError, "token expired"):
            self._search([{"errors": [{"message": "token expired"}]}])

    def test_an_empty_but_valid_result_is_fine(self):
        page = {"data": {"marketplaceJobPostingsSearch": {"edges": [], "pageInfo": {"hasNextPage": False}}}}
        self.assertEqual(self._search([page]), [])

    def test_a_later_page_failure_keeps_what_was_found(self):
        node = {"id": "1", "title": "n8n automation", "description": "d", "ciphertext": "~0x",
                "amount": {"rawValue": "1500"}, "client": {"totalHires": 5, "totalPostedJobs": 5,
                "totalSpent": {"rawValue": "9000"}, "totalReviews": 5, "totalFeedback": 5.0},
                "publishedDateTime": "2999-01-01T00:00:00Z",
                "skills": [{"prettyName": "n8n"}]}
        page1 = {"data": {"marketplaceJobPostingsSearch": {"edges": [{"node": node}],
                 "pageInfo": {"hasNextPage": True, "endCursor": "50"}}}}
        found = self._search([page1, ConnectionError("down")])
        self.assertEqual([j["id"] for j in found], ["1"])


if __name__ == "__main__":
    unittest.main()
