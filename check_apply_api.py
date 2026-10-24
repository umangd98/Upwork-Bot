#!/usr/bin/env python3
"""
check_apply_api.py — Does Upwork's GraphQL API let this app submit proposals, and
what exactly does createJobProposal expect?

Read-only: runs introspection queries only and prints the proposal mutations with
their full input types, including nested ones (milestones, selectedContractor,
questions, sri). Run it on the deployed bot:

    python check_apply_api.py
"""

import json
import sys

from upwork_client import execute_graphql, get_client

TYPE_REF = "name kind ofType { name kind ofType { name kind ofType { name kind } } }"
MUTATIONS = '{ __type(name: "Mutation") { fields { name args { name type { %s } } } } }' % TYPE_REF


def _unwrap(t: dict | None) -> tuple[str | None, str | None, bool]:
    """Return (named type, its kind, is_list) through NON_NULL / LIST wrappers."""
    is_list = False
    while t:
        if t.get("kind") == "LIST":
            is_list = True
        if t.get("name"):
            return t["name"], t.get("kind"), is_list
        t = t.get("ofType")
    return None, None, is_list


def _input_fields(client, type_name: str) -> list[dict]:
    q = '{ __type(name: "%s") { kind inputFields { name type { %s } } enumValues { name } } }' % (type_name, TYPE_REF)
    res = execute_graphql(client, q, {})
    t = ((res or {}).get("data") or {}).get("__type") or {}
    if t.get("enumValues"):
        return [{"name": "(enum) " + ", ".join(v["name"] for v in t["enumValues"]), "type": None}]
    return t.get("inputFields") or []


def _print_type(client, type_name: str, indent: int, seen: set, depth: int) -> None:
    for f in _input_fields(client, type_name):
        name, kind, is_list = _unwrap(f.get("type"))
        label = f"{name}{'[]' if is_list else ''}" if name else ""
        print(f"{'  ' * indent}{f['name']}: {label}")
        if kind == "INPUT_OBJECT" and name not in seen and depth > 0:
            _print_type(client, name, indent + 1, seen | {name}, depth - 1)


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
        if f["name"] != "createJobProposal":
            print(f"MUTATION {f['name']}  (not needed)")
            continue
        print(f"MUTATION {f['name']}")
        for a in f.get("args") or []:
            name, _, _ = _unwrap(a["type"])
            print(f"  arg {a['name']}: {name}")
            _print_type(client, name, 2, {name}, depth=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
