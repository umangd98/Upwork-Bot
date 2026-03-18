#!/usr/bin/env python3
"""
lookup_skills.py — Helper to discover Upwork skill canonical slugs.

Usage
-----
Interactive mode (default):
    python lookup_skills.py

Batch mode:
    python lookup_skills.py --batch "Python, React, Web Scraping, Flask"

Requires valid OAuth2 tokens (run `python entrypoint.py --auth-only` first).
"""

import argparse
import json
import logging
import sys

from upwork_client import execute_graphql, get_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("lookup_skills")

# ---------------------------------------------------------------------------
# GraphQL query for skill search
# ---------------------------------------------------------------------------

SKILL_SEARCH_QUERY = """
query ontologyElementsSearchByPrefLabel(
  $filter: OntologyElementsSearchByPrefLabelFilter
) {
  ontologyElementsSearchByPrefLabel(filter: $filter) {
    id
    ontologyId
    preferredLabel
    type
    entityStatus
  }
}
"""

# Fallback: list skills via ontologySkills
SKILL_LIST_QUERY = """
query ontologySkills($limit: Int!, $offset: Int) {
  ontologySkills(limit: $limit, offset: $offset) {
    id
    ontologyId
    preferredLabel
    prettyName
    entityStatus
  }
}
"""


def search_skill(client, name: str, limit: int = 10) -> list[dict]:
    """Search for a skill by name, return a list of matches."""
    variables = {
        "filter": {
            "preferredLabel_any": name,
            "type": "SKILL",
            "entityStatus_eq": "ACTIVE",
            "sortOrder": "match-start",
            "limit": limit,
        }
    }
    try:
        result = execute_graphql(client, SKILL_SEARCH_QUERY, variables)
        data = (result or {}).get("data", {})
        return data.get("ontologyElementsSearchByPrefLabel") or []
    except Exception as exc:
        logger.error("Skill search failed: %s", exc)
        return []


def _print_results(name: str, results: list[dict]) -> None:
    """Pretty-print skill search results."""
    if not results:
        print(f'\n  ⚠  No results for "{name}"')
        return

    print(f'\n  Results for "{name}":')
    print(f"  {'#':<4} {'preferredLabel':<35} {'ontologyId':<35} {'id'}")
    print(f"  {'─'*4} {'─'*35} {'─'*35} {'─'*20}")
    for i, r in enumerate(results, 1):
        print(
            f"  {i:<4} {r.get('preferredLabel', ''):<35} "
            f"{r.get('ontologyId', ''):<35} {r.get('id', '')}"
        )


def _slug_from_label(label: str) -> str:
    """Derive the likely canonical slug from a preferredLabel."""
    return label.lower().replace(" ", "-").replace(".", "-")


def interactive_mode(client) -> None:
    """Interactive loop: user types a skill name, sees results."""
    print("\n🔍 Upwork Skill Lookup — Interactive Mode")
    print("   Type a skill name to search, or 'q' to quit.\n")

    while True:
        try:
            query = input("Skill name: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() in ("q", "quit", "exit"):
            break

        results = search_skill(client, query)
        _print_results(query, results)

        if results:
            top = results[0]
            slug = _slug_from_label(top["preferredLabel"])
            print(f'\n  → Suggested slug for filters.yaml: "{slug}"')
            print(f'  → Or use ontologyId: "{top.get("ontologyId", "")}"')
        print()


def batch_mode(client, skill_names: list[str]) -> None:
    """Look up multiple skills at once, output YAML-ready list."""
    print("\n🔍 Upwork Skill Lookup — Batch Mode\n")
    slugs: list[str] = []

    for name in skill_names:
        name = name.strip()
        if not name:
            continue
        results = search_skill(client, name, limit=3)
        _print_results(name, results)

        if results:
            top = results[0]
            slug = _slug_from_label(top["preferredLabel"])
            slugs.append(slug)
        else:
            print(f'  ⚠  Could not resolve "{name}" — skipping.')

    if slugs:
        print("\n" + "=" * 60)
        print("Paste this into your filters.yaml under `skills:`:\n")
        print("skills:")
        for s in slugs:
            print(f"  - {s}")
        print()

        # Also output as JSON for programmatic use
        print("As JSON list:")
        print(json.dumps(slugs, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Look up Upwork skill slugs for use in filters.yaml"
    )
    parser.add_argument(
        "--batch",
        type=str,
        default=None,
        help='Comma-separated skill names, e.g. "Python, React, Flask"',
    )
    args = parser.parse_args()

    try:
        client = get_client()
    except RuntimeError as exc:
        print(f"❌ {exc}")
        print("   Run `python entrypoint.py --auth-only` first to authenticate.")
        sys.exit(1)

    if args.batch:
        names = [n.strip() for n in args.batch.split(",") if n.strip()]
        if not names:
            print("❌ --batch requires at least one skill name.")
            sys.exit(1)
        batch_mode(client, names)
    else:
        interactive_mode(client)


if __name__ == "__main__":
    main()
