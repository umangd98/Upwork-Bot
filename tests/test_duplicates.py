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
        applied_jobs._cache.update(ids=set(), titles={})
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
            {"node": {"marketplaceJobPosting": {"id": 111, "content": {"title": "A"}},
                      "auditDetails": {"createdDateTime": {"rawValue": "1792390854363"}}}},
            {"node": {"marketplaceJobPosting": {"id": "222"}}},
            {"node": {"marketplaceJobPosting": None}}, {"node": None}]}}}
        got = applied_jobs.parse_proposals(good)
        self.assertEqual([(p["job_id"], p["title"], p["created_ms"]) for p in got],
                         [("111", "A", 1792390854363), ("222", "", None)])
        self.assertEqual(applied_jobs.parse(good), {"111", "222"})
        for odd in (None, {}, {"data": None}, {"data": {"vendorProposals": None}}):
            self.assertEqual(applied_jobs.parse_proposals(odd), [])

    def _fake_upwork(self, proposals=None, errors=None):
        """Behaves like Upwork: status_eq is REQUIRED and matches ONE status; ids filter applies."""
        calls = []

        def fake(client, query, variables):
            calls.append((query, variables))
            assert query.count("__type") <= 1, "Upwork rejects several __type lookups in one query"
            flt = variables["filter"]
            if variables["pagination"]["first"] > getattr(self, "max_page", 10**6):
                return {"errors": [{"message": "pagination.first is too large"}]}
            if "status_eq" not in flt:
                return {"errors": [{"message": "Field 'status_eq' has coerced Null value for NonNull type"}]}
            if errors:
                return {"errors": errors}
            wanted_ids = flt.get("jobPostingIds_any")
            edges = []
            for p in (proposals or []):
                job_id, title = p[0], p[1]
                status = p[2] if len(p) > 2 else "Accepted"
                if status != flt["status_eq"] or (wanted_ids is not None and job_id not in wanted_ids):
                    continue
                edges.append({"node": {"marketplaceJobPosting": {"id": job_id, "content": {"title": title}},
                                       "auditDetails": {"createdDateTime": {"rawValue": str(int(__import__("time").time() * 1000))}}}})
            return {"data": {"vendorProposals": {"totalCount": len(edges), "edges": edges}}}
        return fake, calls

    def _patched(self, fake):
        applied_jobs._cache.update(ids=set(), titles={})
        config.UPWORK_AGENCY_ORG_ID = "AGENCY1"
        return (mock.patch("applied_jobs.get_client", return_value=object()),
                mock.patch("applied_jobs.execute_graphql", side_effect=fake))

    def test_check_jobs_asks_upwork_about_exactly_these_jobs_once_per_status(self):
        fake, calls = self._fake_upwork(proposals=[("55", "T")])
        p1, p2 = self._patched(fake)
        with p1, p2:
            self.assertTrue(applied_jobs.check_jobs(["55", "66"]))
        self.assertEqual([v["filter"]["status_eq"] for q, v in calls], applied_jobs.STATUSES_ALL)
        for q, v in calls:
            self.assertEqual(v["filter"]["jobPostingIds_any"], ["55", "66"])
            self.assertEqual(v["filter"]["organizationId_eq"], "AGENCY1")
            self.assertEqual(v["sort"], {"field": "CREATEDDATETIME", "sortOrder": "DESC"})   # confirmed values
            self.assertEqual(v["pagination"], {"first": 25})
            self.assertIn("$filter: VendorProposalFilter!", q)
        self.assertTrue(applied_jobs.is_applied("55"))
        self.assertFalse(applied_jobs.is_applied("66"))

    def test_check_jobs_stops_early_when_every_job_is_accounted_for(self):
        fake, calls = self._fake_upwork(proposals=[("55", "T")])
        p1, p2 = self._patched(fake)
        with p1, p2:
            self.assertTrue(applied_jobs.check_jobs(["55"]))
        self.assertEqual(len(calls), 1)                       # found under "Accepted": no more queries

    def test_a_proposal_in_any_status_counts_as_applied(self):
        for status in applied_jobs.STATUSES_ALL:
            with self.subTest(status=status):
                applied_jobs._cache.update(ids=set(), titles={})
                fake, _ = self._fake_upwork(proposals=[("77", "T", status)])
                p1, p2 = self._patched(fake)
                with p1, p2:
                    applied_jobs.check_jobs(["77"])
                self.assertTrue(applied_jobs.is_applied("77"), status)

    def test_check_jobs_chunks_large_batches(self):
        fake, calls = self._fake_upwork()
        p1, p2 = self._patched(fake)
        with p1, p2:
            self.assertTrue(applied_jobs.check_jobs([str(i) for i in range(60)]))
        sizes = [len(v["filter"]["jobPostingIds_any"]) for q, v in calls]
        n = len(applied_jobs.STATUSES_ALL)
        self.assertEqual(sizes, [25] * n + [25] * n + [10] * n)

    def test_check_jobs_failures_are_harmless(self):
        fake, _ = self._fake_upwork(errors=[{"message": "bad argument"}])
        p1, p2 = self._patched(fake)
        with p1, p2:
            self.assertFalse(applied_jobs.check_jobs(["1"]))
        applied_jobs._cache.update(ids=set(), titles={})
        with mock.patch("applied_jobs.get_client", side_effect=RuntimeError("no token")):
            self.assertFalse(applied_jobs.check_jobs(["1"]))
            self.assertFalse(applied_jobs.refresh_recent())
        self.assertTrue(applied_jobs.check_jobs([]))            # nothing to ask is fine
        self.assertFalse(applied_jobs.is_applied("1"))

    def test_recent_titles_and_similarity_window(self):
        fake, calls = self._fake_upwork(proposals=[("9", "AI Business Process Automation Engineer")])
        p1, p2 = self._patched(fake)
        with p1, p2:
            self.assertTrue(applied_jobs.refresh_recent())
        self.assertEqual([v["filter"]["status_eq"] for q, v in calls], applied_jobs.STATUSES_RECENT)
        hit = applied_jobs.similar_applied("ai business process AUTOMATION engineer!")
        self.assertEqual(hit["job_id"], "9")
        self.assertIsNone(applied_jobs.similar_applied("Something else"))
        old = applied_jobs._cache["titles"]["ai business process automation engineer"]
        old["created_ms"] = 1_000_000_000_000                    # year 2001: outside the window
        self.assertIsNone(applied_jobs.similar_applied("AI Business Process Automation Engineer"))

    def test_recent_titles_fall_back_to_a_smaller_page_if_the_size_is_capped(self):
        self.max_page = 25                                     # Upwork rejects anything bigger
        fake, calls = self._fake_upwork(proposals=[("9", "Capped Job")])
        p1, p2 = self._patched(fake)
        with p1, p2:
            self.assertTrue(applied_jobs.refresh_recent())
        sizes = [v["pagination"]["first"] for q, v in calls]
        self.assertEqual(sizes[:2], [50, 25])                  # tried 50, retried with 25
        self.assertIsNotNone(applied_jobs.similar_applied("Capped Job"))

    def test_title_match_forces_approval_and_warns(self):
        applied_jobs._cache.update(titles={"n8n automation x1": {"job_id": "OLD", "title": "n8n automation x1",
                                                                 "created_ms": None}})
        pipeline.process_jobs([job("x1", amount=1000)], FILTERS)    # cheap job would normally auto-apply
        row = db.get_application("x1")
        self.assertEqual((row["mode"], row["status"]), ("approval", "pending"))
        self.assertIn("Possible repost", row["error"])
        self.assertIn("OLD", row["error"])
        self.assertEqual(self.sent[-1]["event"], "needs_approval")
        self.assertIn("Possible repost", self.sent[-1]["error"])    # shown in Slack
        self.assertEqual(self.api_calls if hasattr(self, "api_calls") else [], [])

    def test_check_proposals_script(self):
        import io
        from contextlib import redirect_stdout
        import check_apply_api
        import check_proposals_api as probe
        ref = lambda n, k="INPUT_OBJECT": {"name": n, "kind": k, "ofType": None}
        shapes = {"VendorProposalFilter": [("jobPostingIds_any", ref("ID", "SCALAR"))],
                  "VendorProposalSortAttribute": [("field", ref("String", "SCALAR"))],
                  "Pagination": [("first", ref("Int", "SCALAR"))]}
        fake_up, _ = self._fake_upwork(proposals=[("77", "Some job")])

        def fake(c, q, v):
            if 'name: "Query"' in q:
                return {"data": {"__type": {"fields": [{"name": "vendorProposals", "args": [
                    {"name": "filter", "type": ref("VendorProposalFilter")},
                    {"name": "sortAttribute", "type": ref("VendorProposalSortAttribute")},
                    {"name": "pagination", "type": ref("Pagination")}]}]}}}
            if "inputFields" in q:
                name = q.split('name: "')[1].split('"')[0]
                return {"data": {"__type": {"kind": "INPUT_OBJECT", "enumValues": None,
                                            "inputFields": [{"name": n, "type": t} for n, t in shapes[name]]}}}
            return fake_up(c, q, v)
        applied_jobs._cache.update(ids=set(), titles={})
        config.UPWORK_AGENCY_ORG_ID = "AGENCY1"
        buf = io.StringIO()
        with mock.patch.object(probe, "get_client", return_value=object()), \
             mock.patch.object(probe, "execute_graphql", side_effect=fake), \
             mock.patch.object(check_apply_api, "execute_graphql", side_effect=fake), \
             mock.patch("applied_jobs.get_client", return_value=object()), \
             mock.patch("applied_jobs.execute_graphql", side_effect=fake), redirect_stdout(buf):
            code = probe.main(["77"])
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        for expected in ("jobPostingIds_any: ID", "Sort the bot will use", "CREATEDDATETIME",
                         "Recent-proposals lookup OK", "recognised as already applied: True"):
            self.assertIn(expected, out)


class TestDebugEndpoints(Base):
    def test_require_the_secret(self):
        for path in ("/api/debug/schema?type=SortOrder", "/api/debug/proposals?job_ids=1"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
            self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer x"}).status_code, 403, path)

    def test_schema_returns_upworks_raw_answer_and_validates_the_name(self):
        raw = {"data": {"__type": {"kind": "ENUM", "name": "SortOrder", "enumValues": [{"name": "ASC"}]}}}
        with mock.patch("applied_jobs.execute_graphql", return_value=raw) as q, \
             mock.patch("applied_jobs.get_client", return_value=object()):
            r = self.client.get("/api/debug/schema?type=SortOrder", headers=AUTH)
        self.assertEqual(r.get_json()["result"], raw)
        self.assertEqual(q.call_args[0][2], {"name": "SortOrder"})        # passed as a variable
        for bad in ("", "x y", "a{b}", "Robert');DROP", "A" * 200):
            r = self.client.get("/api/debug/schema", headers=AUTH, query_string={"type": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_proposals_diagnostic_shows_sort_choice_errors_and_findings(self):
        fake, calls = TestAlreadyAppliedOnUpwork._fake_upwork(self, proposals=[("5", "T")])
        p1, p2 = TestAlreadyAppliedOnUpwork._patched(self, fake)
        with p1, p2:
            body = self.client.get("/api/debug/proposals?job_ids=5,6", headers=AUTH).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sort"], {"field": "CREATEDDATETIME", "sortOrder": "DESC"})
        self.assertEqual(body["filter_sent"]["jobPostingIds_any"], ["5", "6"])
        self.assertEqual([(p["job_id"], p["status"]) for p in body["found"]], [("5", "Accepted")])
        self.assertEqual(set(body["by_status"]), set(applied_jobs.STATUSES_ALL))   # JSON keys come back sorted
        self.assertEqual(body["by_status"]["Accepted"]["jobs"], ["5"])
        self.assertTrue(all(v["errors"] is None for v in body["by_status"].values()))

    def test_the_lookup_never_uses_introspection(self):
        fake, calls = TestAlreadyAppliedOnUpwork._fake_upwork(self)
        p1, p2 = TestAlreadyAppliedOnUpwork._patched(self, fake)
        with p1, p2:
            applied_jobs.check_jobs(["1"])
            applied_jobs.refresh_recent()
        self.assertTrue(calls)
        self.assertTrue(all("__type" not in q for q, v in calls))

    def test_first_parameter_is_passed_and_validated(self):
        fake, calls = TestAlreadyAppliedOnUpwork._fake_upwork(self)
        p1, p2 = TestAlreadyAppliedOnUpwork._patched(self, fake)
        with p1, p2:
            body = self.client.get("/api/debug/proposals?first=7", headers=AUTH).get_json()
            bad = self.client.get("/api/debug/proposals?first=abc", headers=AUTH)
        self.assertEqual(body["first"], 7)
        self.assertEqual({v["pagination"]["first"] for q, v in calls}, {7})
        self.assertEqual(bad.status_code, 400)

    def test_upwork_failures_become_502_not_crashes(self):
        with mock.patch("applied_jobs.get_client", side_effect=RuntimeError("no token")):
            self.assertEqual(self.client.get("/api/debug/schema?type=X", headers=AUTH).status_code, 502)
            self.assertEqual(self.client.get("/api/debug/proposals", headers=AUTH).status_code, 502)


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
