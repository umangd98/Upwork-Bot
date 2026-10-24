"""Already-applied guards: Upwork's proposal list, repost detection, duplicate rejections."""
import os
import unittest
from unittest import mock

import test_flow  # fake SDK + temp DB + helpers
from test_flow import AUTH, FILTERS, GOOD, job

import applied_jobs
import applier
import config
import db
import pipeline
from dashboard import app as flask_app


def reposted(jid, **kw):
    """Same posting as job('orig', ...) under a different id (a client repost)."""
    j = job(jid, **kw)
    j["title"] = "AI Business Process Automation Engineer"
    j["client"] = {"location": {"country": "United States"}, "totalSpent": {"rawValue": "77236"},
                   "totalReviews": 6}
    return j


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        applied_jobs._cache.update(ids=set(), at=0.0, ok=False)
        self.sent, self.drafts, self.api_calls = [], [], []

        def draft(j, f):
            self.drafts.append(j["id"])
            return dict(GOOD)

        for p in (mock.patch("events.post", side_effect=lambda x: self.sent.append(x) or True),
                  mock.patch("cover_letter.draft", side_effect=draft),
                  mock.patch("api_routes._filters", return_value=FILTERS),
                  mock.patch("applier.get_client", return_value=object())):
            p.start()
            self.addCleanup(p.stop)
        config.AUTO_APPLY_ENABLED = True
        config.APPLY_BACKEND = "api"
        self.client = flask_app.test_client()


class TestRepostDetection(Base):
    def test_repost_of_a_handled_job_is_skipped_without_drafting_or_alerting(self):
        pipeline.process_jobs([reposted("orig", amount=4000)], FILTERS)
        self.assertEqual(self.drafts, ["orig"])
        sent_before = len(self.sent)
        handled = pipeline.process_jobs([reposted("copy", amount=4000)], FILTERS)
        self.assertEqual(handled, {"copy"})
        self.assertEqual(self.drafts, ["orig"])                    # no letter drafted for the copy
        self.assertEqual(len(self.sent), sent_before)              # and no Slack alert
        row = db.get_application("copy")
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["error"], "Repost of job orig.")

    def test_different_client_or_title_is_not_a_repost(self):
        pipeline.process_jobs([reposted("orig", amount=4000)], FILTERS)
        other_client = reposted("c2", amount=4000)
        other_client["client"]["totalSpent"] = {"rawValue": "5"}
        other_title = reposted("c3", amount=4000)
        other_title["title"] = "Something else entirely"
        pipeline.process_jobs([other_client, other_title], FILTERS)
        self.assertEqual(self.drafts, ["orig", "c2", "c3"])

    def test_skipped_originals_do_not_block_a_later_posting(self):
        with mock.patch("cover_letter.draft", return_value={**GOOD, "apply": False, "skip_reason": "no"}):
            pipeline.process_jobs([reposted("orig", amount=4000)], FILTERS)
        pipeline.process_jobs([reposted("copy", amount=4000)], FILTERS)
        self.assertEqual(db.get_application("copy")["status"], "pending")

    def test_old_applications_do_not_count(self):
        pipeline.process_jobs([reposted("orig", amount=4000)], FILTERS)
        conn = db._connect()
        conn.execute("UPDATE applications SET created_at = '2020-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        pipeline.process_jobs([reposted("copy", amount=4000)], FILTERS)
        self.assertEqual(db.get_application("copy")["status"], "pending")


class TestAlreadyAppliedOnUpwork(Base):
    def test_job_in_the_applied_list_is_skipped(self):
        applied_jobs._cache.update(ids={"j9"}, at=1e12, ok=True)
        handled = pipeline.process_jobs([job("j9", amount=1000)], FILTERS)
        self.assertEqual(handled, {"j9"})
        self.assertEqual(self.drafts, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(db.get_application("j9")["error"], "Already applied on Upwork.")

    def test_parse_handles_real_and_odd_shapes(self):
        good = {"data": {"vendorProposals": {"edges": [
            {"node": {"marketplaceJobPosting": {"id": 111}}},
            {"node": {"marketplaceJobPosting": {"id": "222"}}},
            {"node": {"marketplaceJobPosting": None}}, {"node": None}]}}}
        self.assertEqual(applied_jobs.parse(good), {"111", "222"})
        for odd in (None, {}, {"data": None}, {"data": {"vendorProposals": None}}):
            self.assertEqual(applied_jobs.parse(odd), set())

    def test_refresh_success_caches_and_failures_are_harmless(self):
        ok = {"data": {"vendorProposals": {"edges": [{"node": {"marketplaceJobPosting": {"id": "55"}}}]}}}
        with mock.patch("applied_jobs.get_client", return_value=object()), \
             mock.patch("applied_jobs.execute_graphql", return_value=ok) as q:
            self.assertTrue(applied_jobs.refresh())
            self.assertTrue(applied_jobs.is_applied("55"))
            applied_jobs.refresh()                              # fresh cache: no second query
            self.assertEqual(q.call_count, 1)
        applied_jobs._cache.update(ids=set(), at=0.0, ok=False)
        with mock.patch("applied_jobs.get_client", return_value=object()), \
             mock.patch("applied_jobs.execute_graphql", return_value={"errors": [{"message": "no such field"}]}):
            self.assertFalse(applied_jobs.refresh())
        with mock.patch("applied_jobs.get_client", side_effect=RuntimeError("no token")):
            self.assertFalse(applied_jobs.refresh())
        self.assertFalse(applied_jobs.is_applied("55"))

    def test_check_proposals_script_prints_each_arguments_shape(self):
        import io
        from contextlib import redirect_stdout
        import check_proposals_api as probe
        ref = lambda n, k="INPUT_OBJECT": {"name": n, "kind": k, "ofType": None}
        shapes = {"VendorProposalFilter": [("status", ref("String", "SCALAR")), ("organizationId", ref("ID", "SCALAR"))],
                  "VendorProposalSortAttribute": [("field", ref("String", "SCALAR"))],
                  "Pagination": [("first", ref("Int", "SCALAR")), ("after", ref("String", "SCALAR"))]}

        def fake(c, q, v):
            if 'name: "Query"' in q:
                return {"data": {"__type": {"fields": [{"name": "vendorProposals", "args": [
                    {"name": "filter", "type": ref("VendorProposalFilter")},
                    {"name": "sortAttribute", "type": ref("VendorProposalSortAttribute")},
                    {"name": "pagination", "type": ref("Pagination")}]}]}}}
            if "__type" in q:
                name = q.split('name: "')[1].split('"')[0]
                return {"data": {"__type": {"kind": "INPUT_OBJECT", "enumValues": None,
                                            "inputFields": [{"name": n, "type": t} for n, t in shapes[name]]}}}
            return {"errors": [{"message": "Missing field argument 'filter'"}]}
        buf = io.StringIO()
        import check_apply_api  # the shared type-printing helper has its own client import
        with mock.patch.object(probe, "get_client", return_value=object()), \
             mock.patch.object(probe, "execute_graphql", side_effect=fake), \
             mock.patch.object(check_apply_api, "execute_graphql", side_effect=fake), redirect_stdout(buf):
            code = probe.main()
        out = buf.getvalue()
        self.assertEqual(code, 1)                      # the bot's current query is rejected
        for expected in ("filter: VendorProposalFilter", "organizationId: ID", "sortAttribute:", "first: Int",
                         "Bot query REJECTED"):
            self.assertIn(expected, out)


class TestDuplicateRejections(Base):
    def test_duplicate_error_detection(self):
        for text in ("VJ-JA-10", "You have already applied to this job", "Proposal already submitted"):
            self.assertTrue(applier.is_duplicate_error(text), text)
        for text in ("", "not authorized", "rate limited", None):
            self.assertFalse(applier.is_duplicate_error(text), text)

    def test_apply_raises_already_applied_for_graphql_and_status_errors(self):
        for response in ({"errors": [{"message": "VJ-JA-10: proposal exists"}]},
                         {"data": {"createJobProposal": {"status": "FAILURE", "error": "already applied"}}}):
            with mock.patch("applier.execute_graphql", return_value=response):
                with self.assertRaises(applier.AlreadyApplied):
                    applier.apply({"job_id": "1", "letter": "L", "bid": 10.0}, job("1", hourly=(30, 60)), FILTERS)

    def test_duplicate_rejection_becomes_skipped_not_failed_and_frees_the_cap(self):
        with mock.patch("applier.apply", side_effect=applier.AlreadyApplied("VJ-JA-10")):
            pipeline.process_jobs([job("d1", amount=1000)], FILTERS)
        row = db.get_application("d1")
        self.assertEqual(row["status"], "skipped")
        self.assertIn("Already applied", row["error"])
        self.assertEqual(self.sent[-1]["event"], "skipped")
        self.assertEqual(db.applications_in_window(24), 0)

    def test_button_click_on_a_duplicate_reports_it(self):
        pipeline.process_jobs([job("big", amount=4000)], FILTERS)
        with mock.patch("applier.apply", side_effect=applier.AlreadyApplied("VJ-JA-10")):
            r = self.client.post("/api/apply/big?notify=0", headers=AUTH)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Already applied", r.get_json()["message"])
        self.assertEqual(db.get_application("big")["status"], "skipped")

    def test_session_loop_reporting_a_duplicate_marks_it_skipped(self):
        config.APPLY_BACKEND = "mcp"
        self.addCleanup(setattr, config, "APPLY_BACKEND", "api")
        pipeline.process_jobs([job("m1", amount=1000)], FILTERS)
        self.client.post("/api/queue/m1/claim", headers=AUTH)
        r = self.client.post("/api/result/m1", headers=AUTH,
                             json={"ok": False, "error": "VJ-JA-10 already applied"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.get_application("m1")["status"], "skipped")
        self.assertEqual(self.sent[-1]["event"], "skipped")


if __name__ == "__main__":
    unittest.main()
