"""Tests for the MCP fallback: queue -> Claude session -> result, plus contract checks."""
import json
import os
import re
import unittest
from unittest import mock

import test_flow  # sets up the temp DB, fake SDK and shared helpers
from test_flow import AUTH, FILTERS, GOOD, job

import config
import db
import pipeline
from dashboard import app as flask_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class FakeMcp:
    """Stands in for the Upwork MCP (manage_proposals create + confirm_preview)."""
    def __init__(self, **overrides):
        self.created, self.confirmed, self.overrides = [], [], overrides
        self.n = 0

    def create(self, params):
        self.created.append(params)
        self.n += 1
        preview = {
            "can_apply": True, "connects_cost": 20, "preview_id": f"pv{self.n}",
            "charged_amount": params["charged_amount"], "cover_letter": params["cover_letter"],
            "milestones": params.get("milestones"), "screening_questions": [],
        }
        preview.update(self.overrides)
        return preview

    def confirm(self, preview_id):
        self.confirmed.append(preview_id)
        return {"data": {"createJobProposal": {"status": "SUCCESS", "newProposalId": f"P-{preview_id}"}}}


def session_loop(client, mcp, limit=3):
    """A faithful implementation of claude_loop/mcp_apply_loop.md."""
    items = client.get("/api/queue", headers=AUTH).get_json()["items"]
    done = []
    for item in items[:limit]:
        jid, params = item["job_id"], item["params"]
        if client.post(f"/api/queue/{jid}/claim", headers=AUTH).status_code == 409:
            continue
        prev = mcp.create(params)
        problems = []
        if prev["screening_questions"]:
            problems.append("screening")
        if not prev["can_apply"]:
            problems.append("cannot apply")
        if prev["charged_amount"] != params["charged_amount"]:
            problems.append("bid differs")
        if prev["cover_letter"] != params["cover_letter"]:
            problems.append("letter differs")
        if params.get("milestones") and prev["milestones"] != params["milestones"]:
            problems.append("milestones differ")
        if prev["connects_cost"] > 40:
            problems.append("connects too high")
        if problems:
            client.post(f"/api/result/{jid}", headers=AUTH, json={
                "ok": False, "error": "; ".join(problems),
                "screening_questions": prev["screening_questions"]})
            done.append((jid, False))
            continue
        out = mcp.confirm(prev["preview_id"])
        pid = out["data"]["createJobProposal"]["newProposalId"]
        client.post(f"/api/result/{jid}", headers=AUTH, json={"ok": True, "proposal_id": pid})
        done.append((jid, True))
    return done


class McpBase(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.DB_FILE):
            os.remove(config.DB_FILE)
        self.sent, self.api_calls = [], []
        for p in (
            mock.patch("events.post", side_effect=lambda x: self.sent.append(x) or True),
            mock.patch("cover_letter.draft", return_value=dict(GOOD)),
            mock.patch("applier.apply", side_effect=lambda *a: self.api_calls.append(a) or "X"),
            mock.patch("api_routes._filters", return_value=FILTERS),
        ):
            p.start()
            self.addCleanup(p.stop)
        config.AUTO_APPLY_ENABLED = True
        config.APPLY_BACKEND = "mcp"
        self.addCleanup(setattr, config, "APPLY_BACKEND", "api")
        self.client = flask_app.test_client()

    def events(self):
        return [e["event"] for e in self.sent]

    def queue(self):
        return self.client.get("/api/queue", headers=AUTH).get_json()["items"]


class TestQueueFlow(McpBase):
    def test_auto_job_is_queued_not_submitted_by_the_bot(self):
        pipeline.process_jobs([job("j1", amount=1500)], FILTERS)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.events(), ["queued"])
        self.assertEqual(db.get_application("j1")["status"], "queued")
        self.assertEqual(len(self.queue()), 1)

    def test_full_round_trip_to_applied(self):
        pipeline.process_jobs([job("j1", amount=1500)], FILTERS)
        mcp = FakeMcp()
        self.assertEqual(session_loop(self.client, mcp), [("j1", True)])
        self.assertEqual(db.get_application("j1")["status"], "applied")
        self.assertEqual(db.get_application("j1")["proposal_id"], "P-pv1")
        self.assertEqual(self.events(), ["queued", "applied"])
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.api_calls, [])

    def test_params_are_exact_for_fixed_and_hourly(self):
        pipeline.process_jobs([job("fx", amount=2001), job("hr", hourly=(30, 60))], FILTERS)
        items = {i["job_id"]: i["params"] for i in self.queue()}
        fx, hr = items["fx"], items["hr"]
        self.assertEqual(fx["job_reference"], "fx")
        self.assertEqual(fx["charged_amount"], 2001.0)
        self.assertEqual(fx["payment_mode"], "by_milestone")
        self.assertEqual([m["amount"] for m in fx["milestones"]], [1000.5, 1000.5])
        self.assertEqual(hr["charged_amount"], 40.0)
        self.assertNotIn("milestones", hr)
        self.assertEqual(hr["cover_letter"], GOOD["cover_letter"])

    def test_approval_job_waits_then_queues_on_click_with_edited_letter(self):
        pipeline.process_jobs([job("big", amount=4000)], FILTERS)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.events(), ["needs_approval"])
        self.client.post("/api/letter/big", headers=AUTH, json={"letter": "Edited in Slack"})
        r = self.client.post("/api/apply/big?notify=0", headers=AUTH)
        self.assertEqual(r.get_json()["status"], "queued")
        self.assertEqual(self.queue()[0]["params"]["cover_letter"], "Edited in Slack")
        self.assertEqual(self.events(), ["needs_approval"])        # n8n updates the message itself
        session_loop(self.client, FakeMcp())
        self.assertEqual(db.get_application("big")["status"], "applied")

    def test_dry_run_queues_nothing(self):
        config.AUTO_APPLY_ENABLED = False
        pipeline.process_jobs([job("j1", amount=1500)], FILTERS)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.events(), ["dry_run"])

    def test_clicking_apply_twice_queues_once(self):
        pipeline.process_jobs([job("big", amount=4000)], FILTERS)
        self.client.post("/api/apply/big", headers=AUTH)
        r = self.client.post("/api/apply/big", headers=AUTH)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.events().count("queued"), 1)

    def test_queued_and_submitting_count_toward_the_cap(self):
        pipeline.process_jobs([job(f"a{i}", amount=1000) for i in range(3)], FILTERS)   # cap is 3
        self.assertEqual(db.applications_in_window(24), 3)
        pipeline.process_jobs([job("a4", amount=1000)], FILTERS)
        self.assertEqual(self.events()[-1], "cap_reached")
        self.assertEqual(len(self.queue()), 3)
        r = self.client.post("/api/apply/a4", headers=AUTH)
        self.assertEqual(r.status_code, 409)
        # a failed submission frees its slot
        self.client.post("/api/queue/a0/claim", headers=AUTH)
        self.client.post("/api/result/a0", headers=AUTH, json={"ok": False, "error": "boom"})
        self.assertEqual(db.applications_in_window(24), 2)
        self.assertEqual(self.client.post("/api/apply/a4", headers=AUTH).status_code, 200)

    def test_queue_is_oldest_first_and_loop_handles_three_per_run(self):
        pipeline.process_jobs([job(f"q{i}", amount=1000) for i in range(3)], FILTERS)
        f = {**FILTERS, "apply": {**FILTERS["apply"], "daily_cap": 10}}
        pipeline.process_jobs([job("q3", amount=1000), job("q4", amount=1000)], f)
        self.assertEqual([i["job_id"] for i in self.queue()], ["q0", "q1", "q2", "q3", "q4"])
        done = session_loop(self.client, FakeMcp())
        self.assertEqual([d[0] for d in done], ["q0", "q1", "q2"])
        self.assertEqual(len(self.queue()), 2)


class TestFailuresAndGuards(McpBase):
    def _queue_one(self, jid="j1", amount=1500):
        pipeline.process_jobs([job(jid, amount=amount)], FILTERS)

    def test_screening_questions_are_reported_to_slack_and_not_confirmed(self):
        self._queue_one()
        mcp = FakeMcp(screening_questions=["Why you?"])
        self.assertEqual(session_loop(self.client, mcp), [("j1", False)])
        self.assertEqual(mcp.confirmed, [])
        self.assertEqual(self.events()[-1], "apply_failed")
        self.assertEqual(self.sent[-1]["screening_questions"], ["Why you?"])
        self.assertEqual(db.get_application("j1")["status"], "failed")

    def test_mismatched_previews_are_never_confirmed(self):
        for overrides in ({"charged_amount": 999}, {"cover_letter": "other"},
                          {"milestones": [{"amount": 1, "description": "x"}]},
                          {"can_apply": False}, {"connects_cost": 41}):
            with self.subTest(overrides=overrides):
                self.setUp()
                self._queue_one()
                mcp = FakeMcp(**overrides)
                self.assertEqual(session_loop(self.client, mcp), [("j1", False)])
                self.assertEqual(mcp.confirmed, [])

    def test_failed_job_can_be_retried_from_slack(self):
        self._queue_one()
        session_loop(self.client, FakeMcp(can_apply=False))
        r = self.client.post("/api/apply/j1?notify=0", headers=AUTH)
        self.assertEqual(r.get_json()["status"], "queued")
        self.assertEqual(session_loop(self.client, FakeMcp()), [("j1", True)])
        self.assertEqual(db.get_application("j1")["status"], "applied")

    def test_claim_is_exclusive(self):
        self._queue_one()
        self.assertEqual(self.client.post("/api/queue/j1/claim", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.post("/api/queue/j1/claim", headers=AUTH).status_code, 409)
        self.assertEqual(self.queue(), [])          # claimed items leave the queue
        self.assertEqual(self.client.post("/api/queue/nope/claim", headers=AUTH).status_code, 409)

    def test_a_second_loop_cannot_double_submit(self):
        self._queue_one()
        first, second = FakeMcp(), FakeMcp()
        session_loop(self.client, first)
        session_loop(self.client, second)
        self.assertEqual(len(first.confirmed) + len(second.confirmed), 1)

    def test_result_rules(self):
        self._queue_one()
        post = lambda body: self.client.post("/api/result/j1", headers=AUTH, json=body)
        self.assertEqual(post({"ok": True}).status_code, 409)               # needs proposal_id
        self.assertEqual(post({"ok": True, "proposal_id": "P1"}).status_code, 200)
        self.assertEqual(post({"ok": True, "proposal_id": "P2"}).status_code, 409)  # already applied
        self.assertEqual(db.get_application("j1")["proposal_id"], "P1")
        self.assertEqual(self.client.post("/api/result/nope", headers=AUTH, json={"ok": False}).status_code, 409)

    def test_new_endpoints_require_the_secret(self):
        self._queue_one()
        for method, path in (("get", "/api/queue"), ("post", "/api/queue/j1/claim"), ("post", "/api/result/j1")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 403, path)
            bad = {"Authorization": "Bearer wrong"}
            self.assertEqual(getattr(self.client, method)(path, headers=bad).status_code, 403, path)
        self.assertEqual(db.get_application("j1")["status"], "queued")

    def test_status_reports_backend_and_counts(self):
        self._queue_one()
        s = self.client.get("/api/status", headers=AUTH).get_json()
        self.assertEqual(s["backend"], "mcp")
        self.assertEqual(s["by_status"], {"queued": 1})

    def test_api_backend_still_submits_directly(self):
        config.APPLY_BACKEND = "api"
        pipeline.process_jobs([job("d1", amount=1000)], FILTERS)
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(self.queue(), [])


class TestPollCycle(McpBase):
    def test_full_cycle_dedupes_and_retries_transient_failures(self):
        import scheduler
        jobs = [job("c1", amount=1000), job("c2", amount=4000), job("c3", amount=1200)]
        calls = {"n": 0}

        def flaky_draft(j, f):
            if j["id"] == "c3" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("anthropic down")
            return dict(GOOD)

        with mock.patch("filters.search_jobs", return_value=jobs), \
             mock.patch("cover_letter.draft", side_effect=flaky_draft), \
             mock.patch("config.load_filters", return_value={**FILTERS}), \
             mock.patch("notifier.notify_jobs", return_value=0), \
             mock.patch("applied_jobs.check_jobs", return_value=True) as check, \
             mock.patch("applied_jobs.refresh_recent", return_value=True) as recent:
            scheduler.poll_cycle()
            self.assertEqual(check.call_args[0][0], ["c1", "c2", "c3"])   # asks Upwork about these jobs
            self.assertEqual(recent.call_count, 1)
            self.assertEqual(sorted(i["job_id"] for i in self.queue()), ["c1"])    # c3 failed to draft
            self.assertEqual(self.events(), ["queued", "needs_approval"])
            self.assertEqual(db.filter_unseen(["c1", "c2", "c3"]), {"c3"})        # only c3 retried
            scheduler.poll_cycle()                                                # next cycle
            self.assertEqual(sorted(i["job_id"] for i in self.queue()), ["c1", "c3"])
            self.assertEqual(db.filter_unseen(["c1", "c2", "c3"]), set())
            scheduler.poll_cycle()                                                # nothing new
            self.assertEqual(self.events(), ["queued", "needs_approval", "queued"])


class TestContracts(unittest.TestCase):
    """The loop doc and the n8n workflow may only call endpoints that exist."""
    def _match(self, path, method):
        adapter = flask_app.url_map.bind("localhost")
        adapter.match(path, method=method)   # raises NotFound / MethodNotAllowed

    def test_loop_doc_endpoints_exist(self):
        text = read("claude_loop", "mcp_apply_loop.md")
        paths = set(re.findall(r"\$BOT_URL(/api/[A-Za-z0-9_/<>-]+)", text))
        self.assertGreaterEqual(len(paths), 3)       # queue, claim, result
        for path in paths:
            method = "POST" if path.endswith("/claim") or "/result" in path else "GET"
            self._match(re.sub(r"<[^>]+>", "abc", path), method)

    def test_n8n_workflow_calls_existing_endpoints(self):
        wf = json.loads(read("n8n", "upwork_slack_workflow.json"))
        bot_nodes = [n for n in wf["nodes"] if n["name"].startswith("Bot:")]
        self.assertGreaterEqual(len(bot_nodes), 5)
        for n in bot_nodes:
            url = n["parameters"]["url"].split("REPLACE_BOT_URL", 1)[1]
            path = re.sub(r"\{\{.*?\}\}", "abc", url).split("?")[0]
            self._match(path, n["parameters"]["method"])

    def test_n8n_workflow_is_wired_consistently(self):
        wf = json.loads(read("n8n", "upwork_slack_workflow.json"))
        names = {n["name"] for n in wf["nodes"]}
        for src, outs in wf["connections"].items():
            self.assertIn(src, names)
            for branch in outs["main"]:
                for link in branch:
                    self.assertIn(link["node"], names)
        route = next(n for n in wf["nodes"] if n["name"] == "Route")
        self.assertEqual(len(route["parameters"]["rules"]["values"]), len(wf["connections"]["Route"]["main"]))

    def test_secrets_not_committed_in_workflow_or_docs(self):
        for rel in ("n8n/upwork_slack_workflow.json", "claude_loop/mcp_apply_loop.md", ".env.example"):
            text = read(*rel.split("/"))
            self.assertNotRegex(text, r"eyJ[A-Za-z0-9_-]{20,}")          # JWTs
            self.assertNotRegex(text, r"xox[bpa]-[0-9A-Za-z-]{10,}")      # Slack tokens
            self.assertNotRegex(text, r"sk-ant-[A-Za-z0-9_-]{20,}")       # Anthropic keys


if __name__ == "__main__":
    unittest.main()


class TestSendTestEvent(unittest.TestCase):
    def test_sends_every_sample_event_without_touching_the_db(self):
        import send_test_event
        sent = []
        with mock.patch("events.post", side_effect=lambda p: sent.append(p) or True):
            code = send_test_event.main([])
        self.assertEqual(code, 0)
        self.assertEqual([p["event"] for p in sent],
                         ["applied", "needs_approval", "dry_run", "cap_reached", "apply_failed", "queued"])
        self.assertTrue(all(p["title"].startswith("[TEST]") for p in sent))
        self.assertEqual(sent[4]["screening_questions"], ["(test) Why are you a fit?"])


class TestProbeScript(unittest.TestCase):
    def test_prints_nested_types_for_create_job_proposal(self):
        import io
        from contextlib import redirect_stdout
        import check_apply_api as probe

        def ref(name, kind="INPUT_OBJECT"):
            return {"name": name, "kind": kind, "ofType": None}

        def wrap_list(name):
            return {"name": None, "kind": "LIST", "ofType": ref(name)}

        types = {
            "CreateJobProposalInput": [("jobReference", ref("String", "SCALAR")),
                                       ("milestones", wrap_list("CreateProposalMilestone")),
                                       ("selectedContractor", ref("CreateProposalSelectedContractor"))],
            "CreateProposalMilestone": [("amount", ref("Float", "SCALAR")), ("description", ref("String", "SCALAR"))],
            "CreateProposalSelectedContractor": [("id", ref("ID", "SCALAR")), ("oDeskUserID", ref("String", "SCALAR"))],
        }

        def fake(client, query, variables):
            if "__type(name: \"Mutation\")" in query:
                return {"data": {"__type": {"fields": [
                    {"name": "createJobProposal", "args": [{"name": "input", "type": ref("CreateJobProposalInput")}]},
                    {"name": "withdrawContractProposal", "args": []}]}}}
            name = query.split('name: "')[1].split('"')[0]
            return {"data": {"__type": {"kind": "INPUT_OBJECT", "enumValues": None, "inputFields": [
                {"name": n, "type": t} for n, t in types[name]]}}}

        buf = io.StringIO()
        with mock.patch.object(probe, "get_client", return_value=object()), \
             mock.patch.object(probe, "execute_graphql", side_effect=fake), redirect_stdout(buf):
            code = probe.main()
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("milestones: CreateProposalMilestone[]", out)
        self.assertRegex(out, r"amount: Float")
        self.assertRegex(out, r"oDeskUserID: String")
        self.assertIn("withdrawContractProposal  (not needed)", out)

    def test_reports_when_the_mutation_is_missing(self):
        import io
        from contextlib import redirect_stdout
        import check_apply_api as probe
        fake = lambda c, q, v: {"data": {"__type": {"fields": [{"name": "otherMutation", "args": []}]}}}
        buf = io.StringIO()
        with mock.patch.object(probe, "get_client", return_value=object()), \
             mock.patch.object(probe, "execute_graphql", side_effect=fake), redirect_stdout(buf):
            self.assertEqual(probe.main(), 1)
        self.assertIn("NOT possible", buf.getvalue())
