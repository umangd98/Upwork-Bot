#!/usr/bin/env python3
"""
check_proposals_api.py — Can the bot see the proposals we already submitted?

Read-only. Prints the shape of vendorProposals' arguments, the sort option the bot
picked, how many of the latest proposals it can read, and (optionally) whether a job
id you already applied to is recognised:

    python check_proposals_api.py
    python check_proposals_api.py 2113729766738199814     # a job you applied to
"""

import sys

import applied_jobs
import config
from check_apply_api import TYPE_REF, _print_type, _unwrap
from upwork_client import execute_graphql, get_client

ARGS_QUERY = '{ __type(name: "Query") { fields { name args { name type { %s } } } } }' % TYPE_REF


def main(job_ids: list[str] | None = None) -> int:
    print("Organization context:", config.UPWORK_AGENCY_ORG_ID or "(none: personal account)")
    client = get_client(config.UPWORK_AGENCY_ORG_ID or None)
    res = execute_graphql(client, ARGS_QUERY, {})
    fields = (((res.get("data") or {}).get("__type") or {}).get("fields")) or []
    mine = [f for f in fields if f["name"] == "vendorProposals"]
    if not mine:
        print("No `vendorProposals` query visible.")
        return 1
    print("vendorProposals arguments:")
    for a in mine[0].get("args") or []:
        name, kind, _ = _unwrap(a["type"])
        print(f"  {a['name']}: {name}  [{kind}]")
        _print_type(client, name, 2, {name}, depth=1)

    sort = applied_jobs._sort(client)
    print("\nSort the bot will use:", sort or "COULD NOT BE DETERMINED")
    if not sort:
        return 1
    if not applied_jobs.refresh_recent():
        print("Recent-proposals lookup FAILED (see the warning above).")
        return 1
    titles = applied_jobs._cache["titles"]
    print(f"Recent-proposals lookup OK: {len(titles)} distinct titles, e.g.:")
    for entry in list(titles.values())[:5]:
        print(f"  - {entry['title'][:70]}  (job {entry['job_id']})")
    for job_id in job_ids or []:
        ok = applied_jobs.check_jobs([job_id])
        print(f"check_jobs({job_id}) -> lookup {'OK' if ok else 'FAILED'}; "
              f"recognised as already applied: {applied_jobs.is_applied(job_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
