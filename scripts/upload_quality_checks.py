#!/usr/bin/env python3
"""Upload data quality checks from a JSON file to a Data Quality Framework agent or server.

The JSON file is an array of check payloads (see quality-checks/qc_synthea.json). Each
entry's "category" names a data-quality dimension category, defined in
quality-checks/categories.json; the script creates missing categories and links the
checks. Matching is by exact check name: existing checks are updated, the rest created,
so re-running is safe.

Targets (--target):
  agent  — /api endpoints; checks carry their SQL in the create/update payload.
  server — /api/v1 endpoints; check metadata holds no query, so the script creates a
           new immutable query version (POST /api/v1/quality-checks/{id}/versions)
           whenever the query differs from the latest stored version.

Authentication: admin bearer login at /api/auth/login (same on agent and server).
Password from --password, then $DQF_AGENT_PASSWORD, then an interactive prompt.

Usage:
  python3 scripts/upload_quality_checks.py --agent-url https://dqf-synthea-agent.dyn.cloud.e-infra.cz
  python3 scripts/upload_quality_checks.py --agent-url https://<server-host> --target server
  python3 scripts/upload_quality_checks.py --agent-url http://127.0.0.1:8081 --dry-run
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "quality-checks"
DEFAULT_CHECKS = ROOT / "qc_synthea.json"
DEFAULT_CATEGORIES = ROOT / "categories.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent-url", required=True,
                   help="DQF agent or server base URL, e.g. https://dqf-synthea-agent.dyn.cloud.e-infra.cz")
    p.add_argument("--target", choices=["agent", "server"], default="agent",
                   help="Upload target: agent (default) or Data Quality Server")
    p.add_argument("--user", default="admin", help="Admin user (default: admin)")
    p.add_argument("--password", help="Password (or $DQF_AGENT_PASSWORD, or prompt)")
    p.add_argument("--checks", type=Path, default=DEFAULT_CHECKS,
                   help=f"JSON file with quality check payloads (default: {DEFAULT_CHECKS})")
    p.add_argument("--categories-file", type=Path, default=DEFAULT_CATEGORIES,
                   help="JSON array of dimension categories (default: quality-checks/categories.json)")
    p.add_argument("--category-file", type=Path, default=None,
                   help="Single category object {name, colorHex}; forces ALL checks into it, "
                        "overriding per-check 'category' fields")
    p.add_argument("--no-category", action="store_true", help="Upload checks without any category")
    p.add_argument("--dry-run", action="store_true", help="Print what would change without changing anything")
    return p.parse_args()


class ApiError(RuntimeError):
    pass


def request(base: str, method: str, path: str, token: str | None = None,
            payload: dict | None = None, timeout: int = 30):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise ApiError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def login(base: str, user: str, password: str) -> str:
    status, body = request(base, "POST", "/api/auth/login", payload={"username": user, "password": password})
    if status != 200 or not body or "token" not in body:
        raise ApiError("login failed: unexpected response")
    return body["token"]


class Client:
    """Thin difference layer between the agent and server APIs."""

    def __init__(self, base: str, token: str, target: str):
        self.base, self.token, self.target = base, token, target
        self.api = "/api" if target == "agent" else "/api/v1"

    def list_checks(self) -> list[dict]:
        key = "quality-checks" if self.target == "agent" else "qualityChecks"
        checks, page = [], 0
        while True:
            _, body = request(self.base, "GET",
                              f"{self.api}/quality-checks?page={page}&size=100", self.token)
            body = body or {}
            checks.extend(body.get("_embedded", {}).get(key) or [])
            info = body.get("page", {})
            if page + 1 >= info.get("totalPages", 1):
                return checks
            page += 1

    def list_categories(self) -> list[dict]:
        _, body = request(self.base, "GET", f"{self.api}/categories", self.token)
        return (body or {}).get("_embedded", {}).get("categories", [])

    def create_category(self, name: str, color: str) -> dict:
        _, body = request(self.base, "POST", f"{self.api}/categories", self.token,
                          {"name": name, "colorHex": color})
        return body or {}

    def create_check(self, payload: dict) -> dict:
        _, body = request(self.base, "POST", f"{self.api}/quality-checks", self.token, payload)
        return body or {}

    def update_check(self, check_id: int, payload: dict) -> dict:
        _, body = request(self.base, "PUT", f"{self.api}/quality-checks/{check_id}", self.token, payload)
        return body or {}

    # server only
    def list_versions(self, check_id: int) -> list[dict]:
        _, body = request(self.base, "GET", f"{self.api}/quality-checks/{check_id}/versions", self.token)
        embedded = (body or {}).get("_embedded", {})
        for v in embedded.values():
            if isinstance(v, list):
                return v
        return []

    def create_version(self, check_id: int, query: str, qtype: str, version: int) -> None:
        # The deployed server resolves a null version field to 1 (409 Conflict on
        # checks that already have versions), so always pass the next number.
        request(self.base, "POST", f"{self.api}/quality-checks/{check_id}/versions", self.token,
                {"query": query, "type": qtype, "version": version})


def ensure_categories(client: Client, definitions: list[dict], needed: set[str],
                      dry_run: bool) -> dict[str, int]:
    """Return name -> id for every name in `needed`, creating missing categories."""
    existing = {c["name"]: c["id"] for c in client.list_categories()}
    result = {}
    for name in sorted(needed):
        if name in existing:
            print(f"  = category {name!r} exists (id {existing[name]})")
            result[name] = existing[name]
            continue
        match = next((d for d in definitions if d.get("name") == name), None)
        if match is None:
            raise ApiError(f"category {name!r} is used by a check but missing from {DEFAULT_CATEGORIES}")
        if dry_run:
            print(f"  + category {name!r} (would create)")
            result[name] = -1
        else:
            created = client.create_category(name, match["colorHex"])
            print(f"  + category {name!r} created (id {created['id']})")
            result[name] = created["id"]
    return result


def desired_categories(args: argparse.Namespace, checks: list[dict],
                       definitions: list[dict]) -> dict[str, str]:
    """check name -> category name, or {} when categorization is disabled."""
    if args.no_category:
        return {}
    if args.category_file:
        single = json.loads(args.category_file.read_text())["name"]
        definitions.append({"name": single,
                            "colorHex": json.loads(args.category_file.read_text()).get("colorHex", "#2563EB")})
        return {c["name"]: single for c in checks}
    return {c["name"]: c["category"] for c in checks if c.get("category")}


def main() -> int:
    args = parse_args()
    base = args.agent_url.rstrip("/")
    checks = json.loads(args.checks.read_text())
    definitions = json.loads(args.categories_file.read_text())
    password = args.password or os.environ.get("DQF_AGENT_PASSWORD") \
        or getpass.getpass(f"Password for {args.user}@{base}: ")

    print(f"Authenticating at {base} as {args.user} (target: {args.target}) ...")
    token = login(base, args.user, password)
    client = Client(base, token, args.target)

    wanted = desired_categories(args, checks, definitions)
    cat_ids = ensure_categories(client, definitions, set(wanted.values()), args.dry_run) if wanted else {}

    existing = {c["name"]: c for c in client.list_checks()}
    created = updated = skipped = 0
    versioned = 0
    for check in checks:
        name = check["name"]
        cat_name = wanted.get(name)
        cat_id = cat_ids.get(cat_name) if cat_name else None

        if client.target == "agent":
            payload = {k: check[k] for k in
                       ("name", "description", "query", "type", "warningThreshold", "errorThreshold")
                       if k in check}
            if "epsilonBudget" in check:
                payload["epsilonBudget"] = check["epsilonBudget"]
            payload["categoryId"] = cat_id
        else:  # server: metadata only, no query/type
            payload = {k: check[k] for k in
                       ("name", "description", "warningThreshold", "errorThreshold") if k in check}
            payload["categoryId"] = cat_id

        current = existing.get(name)
        check_id = None
        if current is None:
            if not args.dry_run:
                check_id = client.create_check(payload)["id"]
            print(f"  + {name} ({'would create' if args.dry_run else 'created'})"
                  + (f" [{cat_name}]" if cat_name else ""))
            created += 1
        else:
            check_id = current["id"]
            fields = ("description", "warningThreshold", "errorThreshold") if client.target == "server" \
                else ("description", "query", "type", "warningThreshold", "errorThreshold")
            same = all(current.get(k) == check.get(k) for k in fields) \
                and current.get("epsilonBudget", None) == check.get("epsilonBudget", None) \
                and (current.get("category") or {}).get("id") == cat_id
            if not same:
                if not args.dry_run:
                    client.update_check(check_id, payload)
                print(f"  ~ {name} ({'would update' if args.dry_run else 'updated'})"
                      + (f" [{cat_name}]" if cat_name else ""))
                updated += 1
            else:
                print(f"  = {name} (unchanged)")
                skipped += 1

        # server: the query lives in immutable versions, uniquely keyed by its
        # sha256 hash. The agent's reported versions may store an empty query text
        # with the real hash (backfill), so compare hashes, never the query text.
        if client.target == "server" and not args.dry_run and check_id is not None:
            versions = client.list_versions(check_id)
            query_hash = hashlib.sha256(check["query"].encode()).hexdigest()
            if not any(v.get("hash") == query_hash for v in versions):
                latest_v = max((v.get("version", 0) for v in versions), default=0)
                client.create_version(check_id, check["query"], check.get("type", "SQL"), latest_v + 1)
                print(f"    ^ new query version v{latest_v + 1} published (was v{latest_v or '-'})")
                versioned += 1

    print(f"Done: {created} created, {updated} updated, {skipped} unchanged"
          + (f", {versioned} new query versions" if client.target == "server" and not args.dry_run else "")
          + (" [dry-run]" if args.dry_run else ""))
    if not args.dry_run:
        print(f"Target now has {len(client.list_checks())} quality checks.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
