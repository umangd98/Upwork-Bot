#!/usr/bin/env python3
"""
check_proposals_api.py — Can the bot list the proposals we already submitted?

`vendorProposals` requires three arguments (filter, sortAttribute, pagination). This
prints the full shape of each so the bot's query can be written exactly, then runs the
bot's current query and reports what it found. Read-only. Run on the deployed bot:

    python check_proposals_api.py
"""

import json
import sys

import applied_jobs
from check_apply_api import TYPE_REF, _input_fields, _print_type, _unwrap
from upwork_client import execute_graphql, get_client

ARGS_QUERY = '{ __type(name: "Query") { fields { name args { name type { %s } } } } }' % TYPE_REF


def main() -> int:
    client = get_client()
    res = execute_graphql(client, ARGS_QUERY, {})
    fields = (((res.get("data") or {}).get("__type") or {}).get("fields")) or []
    mine = [f for f in fields if f["name"] == "vendorProposals"]
    if not mine:
        print("No `vendorProposals` query visible. Other proposal queries:",
              ", ".join(f["name"] for f in fields if "proposal" in f["name"].lower()) or "(none)")
        return 1
    print("vendorProposals arguments:")
    for a in mine[0].get("args") or []:
        name, kind, _ = _unwrap(a["type"])
        print(f"  {a['name']}: {name}  [{kind}]")
        _print_type(client, name, 2, {name}, depth=2)
    out = execute_graphql(client, applied_jobs._QUERY, {"filter": None})
    if out.get("errors"):
        print("\nBot query REJECTED:", json.dumps(out["errors"])[:500])
        return 1
    ids = applied_jobs.parse(out)
    total = ((out.get("data") or {}).get("vendorProposals") or {}).get("totalCount")
    print(f"\nBot query OK: found {len(ids)} applied job ids on the first page (totalCount={total}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
