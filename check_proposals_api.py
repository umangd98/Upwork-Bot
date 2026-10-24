#!/usr/bin/env python3
"""
check_proposals_api.py — Can the bot list the proposals we already submitted?

Prints the arguments Upwork's `vendorProposals` query accepts, then runs the query the
bot uses and shows how many job ids it found. Read-only. Run on the deployed bot:

    python check_proposals_api.py
"""

import json
import sys

import applied_jobs
from upwork_client import execute_graphql, get_client

ARGS_QUERY = ('{ __type(name: "Query") { fields { name args { name type { name kind '
              'ofType { name kind } } } } } }')


def main() -> int:
    client = get_client()
    res = execute_graphql(client, ARGS_QUERY, {})
    fields = (((res.get("data") or {}).get("__type") or {}).get("fields")) or []
    mine = [f for f in fields if f["name"] == "vendorProposals"]
    if mine:
        print("vendorProposals args:")
        for a in mine[0].get("args") or []:
            t = a["type"]
            print(f"  {a['name']}: {t.get('name') or (t.get('ofType') or {}).get('name')}")
    else:
        print("No `vendorProposals` query visible. Other proposal queries:",
              ", ".join(f["name"] for f in fields if "proposal" in f["name"].lower()) or "(none)")
    out = execute_graphql(client, applied_jobs._QUERY, {})
    if out.get("errors"):
        print("Bot query REJECTED:", json.dumps(out["errors"])[:400])
        return 1
    ids = applied_jobs.parse(out)
    print(f"Bot query OK: found {len(ids)} applied job ids on the first page "
          f"(totalCount={((out.get('data') or {}).get('vendorProposals') or {}).get('totalCount')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
