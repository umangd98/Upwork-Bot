"""Offline tests for the auto-apply / approval flow. Run: python -m unittest -v"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The Upwork SDK isn't needed offline.
for name in ("upwork", "upwork.routers"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["upwork"].Config = object
sys.modules["upwork"].Client = object
sys.modules["upwork.routers"].graphql = types.SimpleNamespace(Api=None)

import config  # noqa: E402

_TMP = tempfile.mkdtemp()
config.DB_FILE = os.path.join(_TMP, "jobs.db")
config.BOT_API_SECRET = "s3cret"
config.N8N_WEBHOOK_URL = "https://n8n.test/hook"

import applier  # noqa: E402
import cover_letter  # noqa: E402
import db  # noqa: E402
import pipeline  # noqa: E402
from dashboard import app as flask_app  # noqa: E402

FILTERS = {"apply": {"daily_cap": 3, "auto_below_budget": 3000, "hourly_bid": 40,
                     "milestone_split": [50, 50], "max_per_cycle": 20},
           "scoring": {}}
AUTH = {"Authorization": "Bearer s3cret"}


def job(jid, amount=None, hourly=None, title="n8n automation"):
    j = {"id": jid, "title": title, "description": "Build n8n flows. " * 20,
         "job_url": f"https://upwork.test/{jid}", "client": {"location": {"country": "United States"}}}
    if amount is not None:
        j["amount"] = {"rawValue": str(amount), "displayValue": f"{amount:,.0f}"}
    if hourly is not None:
        # Real hourly jobs report a fixed "amount" of 0; it is not a $0 budget.
        j["amount"] = {"rawValue": "0", "displayValue": "0"}
        j["hourlyBudgetMin"] = {"rawValue": str(hourly[0])}
        j["hourlyBudgetMax"] = {"rawValue": str(hourly[1])}
    return j


GOOD = {"apply": True, "skip_reason": "", "estimated_weeks": 1, "cover_letter": "Hi there, letter.",
        "screening_questions_in_post": []}


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        self.sent = []
        self.applied = []
        patches = [
            mock.patch("events.post", side_effect=lambda p: self.sent.append(p) or True),
            mock.patch("cover_letter.draft", return_value=dict(GOOD)),
            mock.patch("applier.apply", side_effect=self._fake_apply),
            mock.patch("api_routes._filters", return_value=FILTERS),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        config.AUTO_APPLY_ENABLED = True
        self.client = flask_app.test_client()

    def _fake_apply(self, app, j, filters):
        self.applied.append(app["job_id"])
        return f"P-{app['job_id']}"

    def events(self):
        return [e["event"] for e in self.sent]


class TestRouting(Base):
    def test_mode_rules(self):
        c = FILTERS["apply"]
        self.assertEqual(pipeline.choose_mode(job("a", amount=2999), c), "auto")
        self.assertEqual(pipeline.choose_mode(job("b", amount=3000), c), "approval")
        self.assertEqual(pipeline.choose_mode(job("c", amount=5000), c), "approval")
        self.assertEqual(pipeline.choose_mode(job("d", hourly=(30, 60)), c), "auto")
        self.assertEqual(pipeline.choose_mode(job("e"), c), "approval")

    def test_bid_rules(self):
        c = FILTERS["apply"]
        self.assertEqual(pipeline.choose_bid(job("a", amount=1200), c), 1200)
        self.assertEqual(pipeline.choose_bid(job("b", hourly=(30, 60)), c), 40)
        self.assertEqual(pipeline.choose_bid(job("c", hourly=(20, 35)), c), 35)

    def test_auto_job_applies_and_alerts(self):
        pipeline.process_jobs([job("j1", amount=1500)], FILTERS)
        self.assertEqual(self.applied, ["j1"])
        self.assertEqual(self.events(), ["applied"])
        ev = self.sent[0]
        self.assertIn("Build n8n flows", ev["description"])      # description reaches Slack
        self.assertEqual(ev["cover_letter"], "Hi there, letter.")  # letter reaches Slack
        self.assertEqual(ev["mode"], "auto")

    def test_dry_run_never_submits(self):
        config.AUTO_APPLY_ENABLED = False
        pipeline.process_jobs([job("j1", amount=1500)], FILTERS)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.events(), ["dry_run"])
        self.assertEqual(db.get_application("j1")["status"], "pending")

    def test_over_threshold_waits_for_approval(self):
        pipeline.process_jobs([job("big", amount=4000)], FILTERS)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.events(), ["needs_approval"])
        r = self.client.post("/api/apply/big", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.applied, ["big"])
        self.assertEqual(self.events(), ["needs_approval", "applied"])

    def test_cap_blocks_auto_and_button(self):
        pipeline.process_jobs([job(f"a{i}", amount=1000) for i in range(3)], FILTERS)
        self.assertEqual(len(self.applied), 3)
        pipeline.process_jobs([job("a4", amount=1000)], FILTERS)
        self.assertEqual(len(self.applied), 3)
        self.assertEqual(self.events()[-1], "cap_reached")
        r = self.client.post("/api/apply/a4", headers=AUTH)
        self.assertEqual(r.status_code, 409)
        self.assertIn("cap", r.get_json()["message"].lower())
        self.assertEqual(len(self.applied), 3)

    def test_double_click_applies_once(self):
        pipeline.process_jobs([job("big", amount=4000)], FILTERS)
        self.client.post("/api/apply/big", headers=AUTH)
        r = self.client.post("/api/apply/big", headers=AUTH)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.applied, ["big"])

    def test_model_skip_sends_nothing(self):
        with mock.patch("cover_letter.draft", return_value={**GOOD, "apply": False, "skip_reason": "tutoring"}):
            handled = pipeline.process_jobs([job("t1", amount=1000)], FILTERS)
        self.assertEqual(handled, {"t1"})
        self.assertEqual(self.sent, [])
        self.assertEqual(db.get_application("t1")["status"], "skipped")

    def test_draft_failure_is_retried_not_marked_handled(self):
        with mock.patch("cover_letter.draft", side_effect=RuntimeError("api down")):
            handled = pipeline.process_jobs([job("x1", amount=1000)], FILTERS)
        self.assertEqual(handled, set())
        self.assertIsNone(db.get_application("x1"))

    def test_apply_failure_reports_and_can_retry(self):
        with mock.patch("applier.apply", side_effect=applier.ApplyError("not allowed")):
            pipeline.process_jobs([job("f1", amount=1000)], FILTERS)
        self.assertEqual(self.events(), ["apply_failed"])
        self.assertEqual(self.sent[0]["error"], "not allowed")
        self.assertEqual(db.get_application("f1")["status"], "failed")
        self.assertEqual(db.applications_in_window(24), 0)  # failures don't use the cap
        r = self.client.post("/api/apply/f1", headers=AUTH)  # retry via button
        self.assertEqual(r.status_code, 200)

    def test_duplicate_job_not_reprocessed(self):
        pipeline.process_jobs([job("d1", amount=1000)], FILTERS)
        pipeline.process_jobs([job("d1", amount=1000)], FILTERS)
        self.assertEqual(self.applied, ["d1"])

    def test_per_cycle_limit(self):
        f = {**FILTERS, "apply": {**FILTERS["apply"], "max_per_cycle": 2, "daily_cap": 99}}
        handled = pipeline.process_jobs([job(f"m{i}", amount=1000) for i in range(5)], f)
        self.assertEqual(len(handled), 2)


class TestApi(Base):
    def setUp(self):
        super().setUp()
        pipeline.process_jobs([job("big", amount=4000)], FILTERS)

    def test_requires_secret(self):
        self.assertEqual(self.client.post("/api/apply/big").status_code, 403)
        bad = {"Authorization": "Bearer nope"}
        self.assertEqual(self.client.post("/api/apply/big", headers=bad).status_code, 403)
        self.assertEqual(self.applied, [])

    def test_fails_closed_without_configured_secret(self):
        config.BOT_API_SECRET = ""
        try:
            self.assertEqual(self.client.post("/api/apply/big", headers={"Authorization": "Bearer "}).status_code, 403)
        finally:
            config.BOT_API_SECRET = "s3cret"

    def test_edit_letter_then_apply_uses_edit(self):
        r = self.client.post("/api/letter/big", headers=AUTH, json={"letter": "My edited letter"})
        self.assertEqual(r.status_code, 200)
        seen = {}
        def capture(app, j, filters):
            seen["letter"] = app["letter"]
            return "P1"
        with mock.patch("applier.apply", side_effect=capture):
            self.client.post("/api/apply/big", headers=AUTH)
        self.assertEqual(seen["letter"], "My edited letter")

    def test_letter_validation(self):
        self.assertEqual(self.client.post("/api/letter/big", headers=AUTH, json={"letter": " "}).status_code, 400)
        self.assertEqual(self.client.post("/api/letter/big", headers=AUTH, json={"letter": "x" * 5001}).status_code, 400)
        self.assertEqual(self.client.post("/api/letter/nope", headers=AUTH, json={"letter": "x"}).status_code, 404)

    def test_cannot_edit_after_applied(self):
        self.client.post("/api/apply/big", headers=AUTH)
        r = self.client.post("/api/letter/big", headers=AUTH, json={"letter": "late edit"})
        self.assertEqual(r.status_code, 409)

    def test_skip_then_cannot_apply_without_retry_semantics(self):
        r = self.client.post("/api/skip/big", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.get_application("big")["status"], "skipped")
        self.assertEqual(self.events()[-1], "skipped")
        r = self.client.post("/api/apply/big", headers=AUTH)
        self.assertEqual(r.status_code, 409)   # skipped jobs can't be claimed
        self.assertEqual(self.applied, [])

    def test_notify_zero_suppresses_event_on_button_clicks(self):
        before = len(self.sent)
        self.client.post("/api/apply/big?notify=0", headers=AUTH)
        self.assertEqual(self.applied, ["big"])
        self.assertEqual(len(self.sent), before)     # n8n updates the message itself
        pipeline.process_jobs([job("big2", amount=4000)], FILTERS)
        before = len(self.sent)
        self.client.post("/api/skip/big2?notify=0", headers=AUTH)
        self.assertEqual(len(self.sent), before)

    def test_get_application_matches_event_shape(self):
        r = self.client.get("/api/application/big", headers=AUTH).get_json()
        self.assertEqual(r["event"], "needs_approval")
        self.assertIn("Build n8n flows", r["description"])
        self.assertEqual(r["cover_letter"], "Hi there, letter.")
        self.assertEqual(r["daily_cap"], 3)
        self.client.post("/api/apply/big?notify=0", headers=AUTH)
        self.assertEqual(self.client.get("/api/application/big", headers=AUTH).get_json()["event"], "applied")

    def test_get_application_and_status(self):
        s = self.client.get("/api/status", headers=AUTH).get_json()
        self.assertEqual(s["applied_in_last_24h"], 0)
        self.assertEqual(s["daily_cap"], 3)
        self.assertEqual(s["backend"], "api")
        self.assertEqual(s["by_status"], {"pending": 1})


class TestPayloadAndGate(Base):
    def test_milestones_split_5050_and_sum_to_bid(self):
        app = {"job_id": "z", "letter": "L", "bid": 2001.0}
        p = applier.build_input(app, job("z", amount=2001), FILTERS)
        self.assertEqual([m["amount"] for m in p["milestones"]], [1000.5, 1000.5])
        self.assertEqual(sum(m["amount"] for m in p["milestones"]), 2001.0)

    def test_hourly_has_no_milestones(self):
        p = applier.build_input({"job_id": "h", "letter": "L", "bid": 40.0}, job("h", hourly=(30, 60)), FILTERS)
        self.assertNotIn("milestones", p)

    def _model_reply(self, **fields):
        body = {"content": [{"type": "text", "text": json.dumps({**GOOD, **fields})}]}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        return resp

    def test_scope_gate_direct(self):
        config.ANTHROPIC_API_KEY = "k"
        mock.patch.stopall()  # restore the real cover_letter.draft
        with mock.patch("cover_letter.requests.post", return_value=self._model_reply(estimated_weeks=3)):
            r = cover_letter.draft(job("g", amount=1200), {"scoring": {}})
        self.assertFalse(r["apply"])
        self.assertIn("buys", r["skip_reason"])
        with mock.patch("cover_letter.requests.post", return_value=self._model_reply(estimated_weeks=1)):
            r = cover_letter.draft(job("g", amount=1200), {"scoring": {}})
        self.assertTrue(r["apply"])
        with mock.patch("cover_letter.requests.post", return_value=self._model_reply(estimated_weeks=9)):
            r = cover_letter.draft(job("g", amount=5000), {"scoring": {}})
        self.assertTrue(r["apply"])  # above the tiers a human decides

    def test_garbage_model_reply_raises(self):
        config.ANTHROPIC_API_KEY = "k"
        mock.patch.stopall()
        bad = mock.Mock()
        bad.json.return_value = {"content": [{"type": "text", "text": "sorry, no JSON"}]}
        bad.raise_for_status.return_value = None
        with mock.patch("cover_letter.requests.post", return_value=bad):
            with self.assertRaises(ValueError):
                cover_letter.draft(job("g", amount=1200), {"scoring": {}})


if __name__ == "__main__":
    unittest.main()
