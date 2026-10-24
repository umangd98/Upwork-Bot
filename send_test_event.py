#!/usr/bin/env python3
"""
send_test_event.py — Send sample job events to your n8n webhook to test Slack.

    python send_test_event.py                 # sends all sample events
    python send_test_event.py applied dry_run # or pick some

Uses N8N_WEBHOOK_URL and BOT_API_SECRET from .env. Nothing touches Upwork or the
database; the events are clearly marked as tests.
"""

import sys

import config
import events

SAMPLE_JOB = {
    "id": "TEST-0001",
    "title": "[TEST] n8n + AI automation for a small business",
    "description": ("This is a test job description so you can check how long text looks "
                    "in Slack. " * 12),
    "job_url": "https://www.upwork.com/",
    "amount": {"rawValue": "1500", "displayValue": "1,500"},
    "client": {"location": {"country": "United States"}, "totalSpent": {"displayValue": "$12,400"},
               "totalReviews": 14, "totalFeedback": 4.9},
    "fit_score": 82,
}
LETTER = ("Hi there,\n\nThis is a sample cover letter so you can see how it appears in Slack.\n\n"
          "Two quick questions:\n1. Which task takes the most time today?\n2. Who signs off on tools?\n\n"
          "About me: Top Rated on Upwork, 105 contracts, $100K+ earned.\n\nSarvesh")


def main(names: list[str]) -> int:
    wanted = names or ["applied", "needs_approval", "dry_run", "cap_reached", "apply_failed", "queued"]
    ok = True
    for name in wanted:
        mode = "approval" if name == "needs_approval" else "auto"
        app = {"job_id": "TEST-0001", "mode": mode, "status": "pending", "title": SAMPLE_JOB["title"],
               "bid": 1500.0, "letter": LETTER, "proposal_id": "TEST" if name == "applied" else None,
               "error": "Upwork rejected the request: (test error)" if name == "apply_failed" else None}
        payload = events.build_payload(name, app, SAMPLE_JOB, window_count=3, cap=10,
                                       extra={"screening_questions": ["(test) Why are you a fit?"]
                                              if name == "apply_failed" else []})
        sent = events.post(payload)
        print(f"{name:15} -> {'sent' if sent else 'FAILED'}")
        ok = ok and sent
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
