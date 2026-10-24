#!/usr/bin/env python3
"""
check_apply_api.py — Does Upwork's GraphQL API let this app submit proposals?

Read-only: runs two introspection queries and prints what it finds. Run it once on
the deployed bot (it needs the saved OAuth token):

    python check_apply_api.py

If createJobProposal is listed, copy its input type's fields into applier.py's
build_input(). If it is missing or the schema is hidden, auto-apply cannot work
through the API and the bot falls back to showing the cover letter + job link in
Slack for you to submit manually.
"""

import json
import sys

from upwork_client import execute_graphql, get_client

MUTATIONS = '{ __type(name: "Mutation") { fields { name args { name type { name kind ofType { name } } } } } }'


def _input_fields(client, type_name: str) -> list[dict]:
    q = ('{ __type(name: "%s") { inputFields { name type { name kind '
         'ofType { name kind ofType { name } } } } } }' % type_name)
    res = execute_graphql(client, q, {})
    return (((res or {}).get("data") or {}).get("__type") or {}).get("inputFields") or []


def main() -> int:
    client = get_client()
    res = execute_graphql(client, MUTATIONS, {})
    if res.get("errors"):
        print("Introspection blocked:", json.dumps(res["errors"])[:300])
        return 2
    fields = (((res.get("data") or {}).get("__type") or {}).get("fields")) or []
    hits = [f for f in fields if "proposal" in f["name"].lower()]
    if not hits:
        print("No proposal mutations visible to this app. Auto-apply via API is NOT possible.")
        print("All mutations:", ", ".join(f["name"] for f in fields) or "(none)")
        return 1
    for f in hits:
        print(f"MUTATION {f['name']}")
        for a in f.get("args") or []:
            t = a["type"]
            tname = t.get("name") or (t.get("ofType") or {}).get("name")
            print(f"  arg {a['name']}: {tname}")
            for inp in _input_fields(client, tname):
                it = inp["type"]
                nm = it.get("name") or (it.get("ofType") or {}).get("name") or (
                    ((it.get("ofType") or {}).get("ofType") or {}).get("name"))
                print(f"    {inp['name']}: {nm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
