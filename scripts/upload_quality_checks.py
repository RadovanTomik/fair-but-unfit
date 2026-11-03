#!/usr/bin/env python3
"""Upload data quality checks from a JSON file to a Data Quality Framework agent.

The JSON file must be an array of Agent `QualityCheckCreateDTO` payloads, e.g. the
bundled quality-checks/qc_synthea.json. Each entry is matched against the agent's
existing checks by exact name: matching entries are updated (PUT), the rest are
created (POST), so re-running is safe.

Authentication: the agent's admin user. The password is taken from --password,
then $DQF_AGENT_PASSWORD, then an interactive prompt.

Usage:
  python3 scripts/upload_quality_checks.py --agent-url https://dqf-synthea-agent.dyn.cloud.e-infra.cz
  python3 scripts/upload_quality_checks.py --agent-url http://127.0.0.1:8081 --dry-run
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CHECKS = Path(__file__).resolve().parent.parent / "quality-checks" / "qc_synthea.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent-url", required=True, help="Agent base URL, e.g. https://dqf-synthea-agent.dyn.cloud.e-infra.cz")
    p.add_argument("--user", default="admin", help="Agent user (default: admin)")
    p.add_argument("--password", help="Agent password (or $DQF_AGENT_PASSWORD, or prompt)")
    p.add_argument("--checks", type=Path, default=DEFAULT_CHECKS,
                   help=f"JSON file with quality check payloads (default: {DEFAULT_CHECKS})")
    p.add_argument("--dry-run", action="store_true", help="Print what would change without touching the agent")
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


def list_checks(base: str, token: str) -> list[dict]:
    checks, page = [], 0
    while True:
        _, body = request(base, "GET", f"/api/quality-checks?page={page}&size=100", token)
        body = body or {}
        embedded = body.get("_embedded", {})
        checks.extend(embedded.get("quality-checks") or [])
        info = body.get("page", {})
        if page + 1 >= info.get("totalPages", 1):
            return checks
        page += 1


def main() -> int:
    args = parse_args()
    base = args.agent_url.rstrip("/")
    checks = json.loads(args.checks.read_text())
    password = args.password or __import__("os").environ.get("DQF_AGENT_PASSWORD") \
        or getpass.getpass(f"Agent password for {args.user}@{base}: ")

    print(f"Authenticating at {base} as {args.user} ...")
    token = login(base, args.user, password)

    existing = {c["name"]: c for c in list_checks(base, token)}
    created = updated = skipped = 0
    for check in checks:
        name = check["name"]
        if name in existing:
            current = existing[name]
            fields = {k: current.get(k) for k in ("description", "query", "type", "warningThreshold", "errorThreshold")}
            changed = any(fields[k] != check.get(k) for k in fields)
            if not changed and current.get("epsilonBudget") == check.get("epsilonBudget"):
                print(f"  = {name} (unchanged)")
                skipped += 1
                continue
            if not args.dry_run:
                request(base, "PUT", f"/api/quality-checks/{current['id']}", token, check)
            print(f"  ~ {name} ({'would update' if args.dry_run else 'updated'})")
            updated += 1
        else:
            if not args.dry_run:
                request(base, "POST", "/api/quality-checks", token, check)
            print(f"  + {name} ({'would create' if args.dry_run else 'created'})")
            created += 1

    print(f"Done: {created} created, {updated} updated, {skipped} unchanged"
          + (" [dry-run]" if args.dry_run else ""))
    if not args.dry_run:
        final = list_checks(base, token)
        print(f"Agent now has {len(final)} quality checks.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
