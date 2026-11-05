"""Manual end-to-end check: does the API show each user ONLY their own scopes?

WHAT IT DOES
------------
1. Reads the published suggestions file and picks two household scopes in it.
2. Registers three new users THROUGH THE API (so passwords are hashed by the
   real code): A, B and C.
3. Puts A in the first household and B in the second (a direct insert into the
   SYNTHETIC database: the API has no endpoint for this yet). C stays alone.
4. Logs each one in and calls GET /api/v1/suggestions, then checks:
     A sees only household A,  B sees only household B,  C sees nothing,
     no token -> 401, and the per-request fields are right.
5. Deletes the three users and their memberships again (also on failure).

SAFETY
------
It only touches the database in SYNTHETIC_DATABASE_URL, and only when both
households contain nothing but synthetic users (@synthetic.invalid). The API
server MUST be using that same database, so start it like this (PowerShell,
one terminal):

    $env:DATABASE_URL = $env:SYNTHETIC_DATABASE_URL     # only for this terminal
    uv run uvicorn backend.app.main:app

then run this script from another terminal:

    uv run python scripts/check_api_scopes.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API = os.getenv("API_URL", "http://127.0.0.1:8000")
SERVING = PROJECT_ROOT / "data" / "serving" / "suggestions.json"
failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("PASS  " if ok else "FAIL  ") + message)
    if not ok:
        failures.append(message)


def call(method: str, path: str, body: dict | None = None, token: str | None = None):
    request = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SYNTHETIC_DATABASE_URL")
    if not url:
        print("SYNTHETIC_DATABASE_URL is not set.")
        return 1
    if not SERVING.exists():
        print(f"No published file at {SERVING}. Run ml.suggest --publish first.")
        return 1

    published = json.loads(SERVING.read_text(encoding="utf-8"))
    households = sorted({s["scope_id"] for s in published["suggestions"]
                         if s["scope_type"] == "household"})
    if len(households) < 2:
        print("The published file needs at least two household scopes.")
        return 1
    house_a, house_b = households[:2]

    engine = create_engine(url)
    with engine.connect() as connection:
        outsiders = connection.execute(text(
            "SELECT count(*) FROM household_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.household_id IN (:a, :b) AND u.email NOT LIKE '%@synthetic.invalid'"),
            {"a": house_a, "b": house_b}).scalar_one()
    if outsiders:
        print("Refusing: those households contain non-synthetic users.")
        return 1

    stamp = f"{int(time.time())}"
    password = secrets.token_urlsafe(18)
    users = {name: f"apicheck-{stamp}-{name}@apicheck.example.com" for name in "abc"}
    created: list[int] = []
    try:
        for name, email in users.items():
            status, _ = call("POST", "/api/v1/auth/register", {"email": email, "password": password})
            check(status == 201, f"register user {name.upper()} -> {status}")
            if status != 201:
                print("Registration failed: is the server running at " + API + "?")
                return 1
        with engine.begin() as connection:
            ids = {row.email: row.id for row in connection.execute(
                text("SELECT id, email FROM users WHERE email = ANY(:e)"), {"e": list(users.values())})}
            created = list(ids.values())
            if len(ids) != 3:
                print("\nThe server is NOT using the synthetic database. Restart it with "
                      "DATABASE_URL set to SYNTHETIC_DATABASE_URL (see the top of this file).")
                return 1
            for name, house in (("a", house_a), ("b", house_b)):
                connection.execute(text(
                    "INSERT INTO household_members (household_id, user_id, role) "
                    "VALUES (:h, :u, 'member')"), {"h": house, "u": ids[users[name]]})

        seen: dict[str, list[dict]] = {}
        for name in "abc":
            status, login = call("POST", "/api/v1/auth/login", {"email": users[name], "password": password})
            check(status == 200, f"login user {name.upper()} -> {status}")
            status, body = call("GET", "/api/v1/suggestions", token=login["access_token"])
            check(status == 200, f"suggestions for {name.upper()} -> {status}")
            seen[name] = body["suggestions"]

        scopes = lambda rows: {(s["scope_type"], s["scope_id"]) for s in rows}  # noqa: E731
        check(scopes(seen["a"]) == {("household", house_a)}, f"A sees only household:{house_a}")
        check(scopes(seen["b"]) == {("household", house_b)}, f"B sees only household:{house_b}")
        check(seen["c"] == [], "C (no household) sees nothing")
        expected_a = [s for s in published["suggestions"]
                      if (s["scope_type"], s["scope_id"]) == ("household", house_a)]
        check(len(seen["a"]) == len(expected_a), "A gets every suggestion of their household")
        check(call("GET", "/api/v1/suggestions")[0] == 401, "no token -> 401")

        today = datetime.now(timezone.utc).date()
        rows = seen["a"] + seen["b"]
        check(all(s["days_until"] == (date.fromisoformat(s["expected_date"]) - today).days
                  for s in rows if s["expected_date"]), "days_until is worked out from today's date")
        check(all("history_strength" in s and "confidence" not in s for s in rows),
              "history_strength present, old `confidence` gone")

        print(f"\n{'user':5} {'scope':14} {'product':10} {'status':9} {'days':>5} {'history':9} on list")
        for name in "ab":
            for s in seen[name]:
                print(f"{name.upper():5} {s['scope_type'] + ':' + str(s['scope_id']):14} "
                      f"{s['product_name']:10} {s['status']:9} {str(s['days_until']):>5} "
                      f"{s['history_strength']:9} {s['on_list_id'] or '-'}")
    finally:
        if created:
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM household_members WHERE user_id = ANY(:u)"), {"u": created})
                connection.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": created})
            print("\n(test users removed)")
    print("\nALL CHECKS PASSED" if not failures else f"\n{len(failures)} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())