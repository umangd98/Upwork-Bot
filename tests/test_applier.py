"""applier.apply against a fake Upwork GraphQL client."""
import os
import unittest
from unittest import mock

import test_flow  # fake SDK + temp DB + helpers
from test_flow import AUTH, FILTERS, job

import applier
import config
import db
import pipeline
from dashboard import app as flask_app

LETTER = 'Hi there,\n\nA "quoted" line with a backslash \\ and an emoji ✅.\n\nSarvesh'


def stored_app(bid=1500.0, letter=LETTER):
    return {"job_id": "123", "letter": letter, "bid": bid}


class Fake:
    def __init__(self, response):
        self.response, self.calls = response, []

    def __call__(self, client, query, variables):
        self.calls.append((query, variables))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


OK = {"data": {"createJobProposal": {"status": "SUCCESS", "error": None, "newProposalId": "P99"}}}


class TestApply(unittest.TestCase):
    def setUp(self):
        config.UPWORK_AGENCY_ORG_ID = "AGENCY1"
        config.UPWORK_FREELANCER_ID = "FREE1"
        config.UPWORK_FREELANCER_ODESK_ID = "dhaninfo"
        patch = mock.patch("applier.get_client", return_value=object())
        patch.start()
        self.addCleanup(patch.stop)

    def run_apply(self, response, app=None, j=None):
        fake = Fake(response)
        with mock.patch("applier.execute_graphql", side_effect=fake):
            return fake, applier.apply(app or stored_app(), j or job("123", amount=1500), FILTERS)

    def test_success_returns_proposal_id(self):
        _, pid = self.run_apply(OK)
        self.assertEqual(pid, "P99")

    def test_sends_variables_not_an_inlined_literal(self):
        fake, _ = self.run_apply(OK)
        query, variables = fake.calls[0]
        self.assertIn("$input: CreateJobProposalInput!", query)
        self.assertNotIn("Hi there", query)                    # letter is never spliced into the query
        self.assertEqual(variables["input"]["coverLetter"], LETTER)   # newlines/quotes intact

    def test_fixed_job_payload(self):
        fake, _ = self.run_apply(OK)
        p = fake.calls[0][1]["input"]
        self.assertEqual(p["jobReference"], "123")
        self.assertEqual(p["chargedAmount"], 1500.0)
        self.assertEqual(p["agencyOrgId"], "AGENCY1")
        self.assertEqual(p["teamOrgId"], "AGENCY1")
        self.assertEqual(p["selectedContractor"], {"id": "FREE1", "oDeskUserID": "dhaninfo"})
        self.assertEqual(p["sri"], {"percent": 0, "frequency": 0})
        self.assertEqual([m["amount"] for m in p["milestones"]], [750.0, 750.0])
        self.assertTrue(all(m["description"] for m in p["milestones"]))
        self.assertEqual(p["estimatedDuration"], 4)
        self.assertNotIn("boostBidAmount", p)                  # never boosts

    def test_hourly_job_payload_has_no_milestones_or_duration(self):
        fake, _ = self.run_apply(OK, stored_app(bid=40.0), job("123", hourly=(30, 60)))
        p = fake.calls[0][1]["input"]
        self.assertEqual(p["chargedAmount"], 40.0)
        self.assertNotIn("milestones", p)
        self.assertNotIn("estimatedDuration", p)

    def test_contractor_fields_are_optional(self):
        config.UPWORK_FREELANCER_ODESK_ID = ""
        config.UPWORK_FREELANCER_ID = ""
        fake, _ = self.run_apply(OK)
        self.assertNotIn("selectedContractor", fake.calls[0][1]["input"])

    def test_graphql_errors_become_apply_errors(self):
        with self.assertRaisesRegex(applier.ApplyError, "not authorized"):
            self.run_apply({"errors": [{"message": "not authorized"}]})

    def test_non_success_status_reports_upworks_message(self):
        with self.assertRaisesRegex(applier.ApplyError, "VJ-JA-10"):
            self.run_apply({"data": {"createJobProposal": {"status": "FAILURE", "error": "VJ-JA-10 already applied"}}})

    def test_missing_proposal_id_is_not_treated_as_success(self):
        with self.assertRaises(applier.ApplyError):
            self.run_apply({"data": {"createJobProposal": {"status": "SUCCESS", "newProposalId": None}}})

    def test_empty_response_and_network_errors(self):
        with self.assertRaises(applier.ApplyError):
            self.run_apply({"data": None})
        with self.assertRaisesRegex(applier.ApplyError, "request failed"):
            self.run_apply(ConnectionError("down"))


class TestHourlyZeroAmountRegression(unittest.TestCase):
    """Hourly jobs carry amount.rawValue == "0": never a $0 fixed budget, bid or milestone."""

    def setUp(self):
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        self.sent = []
        for p in (mock.patch("events.post", side_effect=lambda x: self.sent.append(x) or True),
                  mock.patch("api_routes._filters", return_value=FILTERS)):
            p.start()
            self.addCleanup(p.stop)
        config.AUTO_APPLY_ENABLED = False

    def test_helpers_ignore_zero_amount(self):
        import scoring
        hourly = job("h", hourly=(30, 60))
        self.assertEqual(hourly["amount"]["rawValue"], "0")
        self.assertIsNone(scoring.fixed_budget(hourly))
        self.assertEqual(scoring.fixed_budget(job("f", amount=1500)), 1500.0)
        self.assertEqual(pipeline.choose_bid(hourly, FILTERS["apply"]), 40)
        self.assertEqual(pipeline.choose_mode(hourly, FILTERS["apply"]), "auto")

    def test_scope_gate_does_not_apply_to_hourly(self):
        import cover_letter
        reply = mock.Mock()
        reply.raise_for_status.return_value = None
        reply.json.return_value = {"content": [{"type": "text", "text":
            '{"apply": true, "estimated_weeks": 4, "cover_letter": "Hi", "screening_questions_in_post": []}'}]}
        config.ANTHROPIC_API_KEY = "k"
        with mock.patch("cover_letter.requests.post", return_value=reply):
            r = cover_letter.draft(job("h", hourly=(30, 60)), {"scoring": {}})
        self.assertTrue(r["apply"], r.get("skip_reason"))      # was: "scope needs ~4 weeks but $0 buys 1"

    def test_hourly_payloads_have_no_milestones_and_a_real_rate(self):
        h = job("h", hourly=(30, 60))
        a = {"job_id": "h", "letter": "L", "bid": pipeline.choose_bid(h, FILTERS["apply"])}
        self.assertNotIn("milestones", applier.build_input(a, h, FILTERS))
        self.assertNotIn("estimatedDuration", applier.build_input(a, h, FILTERS))
        mcp = applier.build_mcp_params(a, h, FILTERS)
        self.assertEqual(mcp["charged_amount"], 40.0)
        self.assertNotIn("milestones", mcp)

    def test_slack_budget_text_for_hourly_and_fixed(self):
        import events
        self.assertEqual(events._budget(job("h", hourly=(30, 60))), "$30–$60/hr")
        self.assertEqual(events._budget(job("f", amount=1500)), "$1,500 fixed")

    def test_full_pipeline_for_an_hourly_job(self):
        with mock.patch("cover_letter.draft", return_value=dict(test_flow.GOOD)):
            pipeline.process_jobs([job("h", hourly=(30, 60))], FILTERS)
        row = db.get_application("h")
        self.assertEqual((row["mode"], row["bid"], row["status"]), ("auto", 40.0, "pending"))
        self.assertEqual(self.sent[-1]["budget"], "$30–$60/hr")


class TestApplicationsEndpoint(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        self.client = flask_app.test_client()
        for p in (mock.patch("events.post", return_value=True),
                  mock.patch("cover_letter.draft", side_effect=self._draft),
                  mock.patch("api_routes._filters", return_value=FILTERS)):
            p.start()
            self.addCleanup(p.stop)
        config.AUTO_APPLY_ENABLED = False

    def _draft(self, j, f):
        if j["id"] == "skipme":
            return {"apply": False, "skip_reason": "tutoring job", "cover_letter": "x"}
        return {"apply": True, "skip_reason": "", "estimated_weeks": 1, "cover_letter": "L"}

    def test_lists_pending_and_skipped_with_reasons(self):
        pipeline.process_jobs([job("a", amount=4000), job("skipme", amount=1000)], FILTERS)
        pending = self.client.get("/api/applications?status=pending", headers=AUTH).get_json()["items"]
        skipped = self.client.get("/api/applications?status=skipped", headers=AUTH).get_json()["items"]
        self.assertEqual([i["job_id"] for i in pending], ["a"])
        self.assertEqual(skipped[0]["error"], "tutoring job")
        self.assertEqual(len(self.client.get("/api/applications", headers=AUTH).get_json()["items"]), 2)

    def test_requires_secret_and_clamps_limit(self):
        self.assertEqual(self.client.get("/api/applications").status_code, 403)
        self.assertEqual(self.client.get("/api/applications?limit=99999", headers=AUTH).status_code, 200)


if __name__ == "__main__":
    unittest.main()
