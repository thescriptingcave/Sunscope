#!/usr/bin/env python3
"""Generate a Postman collection from the API's own OpenAPI schema.

WHY A GENERATOR
---------------
The alternative is a hand-written ``postman_collection.json``. That file rots the moment an
endpoint is added, and it rots *silently*: a request for an endpoint that no longer exists
is still a syntactically valid collection, so nothing complains until someone runs it and
gets a 404 and assumes the API is broken.

So the collection is derived from ``/openapi.json`` -- the same document FastAPI serves to
Swagger UI. One source of truth, and ``--check`` fails the build when the committed
collection no longer matches it. This is the same arrangement as ``export-sql.py``: a
generated artefact with a gate that notices drift.

WHAT POSTMAN'S OWN OPENAPI IMPORT DOES NOT DO
---------------------------------------------
Postman will import ``openapi.json`` directly, and that gets you a request per endpoint.
It does not get you the three things that make a collection usable by a human learning the
API, which is the entire point of shipping one:

1. **Auth chaining.** Import produces 12 requests that all need a ``Bearer`` token and no
   way to get one. Here, the login request's test script captures the token into
   ``{{token}}`` and every other request picks it up, so the collection works from a cold
   clone with one click.
2. **A second role.** ``viewer`` exists specifically to prove the RBAC boundary, and that
   proof is two requests -- one that returns 200, one that returns 403 -- which is
   precisely the thing a generated import cannot know to include.
3. **Runnable values.** The imported requests have empty query parameters. These are filled
   with values that work against the shipped dataset, and a note on each says what a good
   answer looks like.

Usage
-----
    uv run --project api python scripts/gen-postman.py             # write the files
    uv run --project api python scripts/gen-postman.py --check     # fail if out of date
    uv run --project api python scripts/gen-postman.py --spec-only # refresh openapi.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "api" / "openapi.json"
COLLECTION_PATH = ROOT / "docs" / "postman" / "Sunscope.postman_collection.json"
ENV_PATH = ROOT / "docs" / "postman" / "Sunscope.postman_environment.json"

#: Where a running API publishes its schema. Loopback only, like everything else here.
DEFAULT_API = "http://127.0.0.1:8000/openapi.json"

#: Postman's schema URL. Only ever validated, never fetched.
COLLECTION_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

#: Map an OpenAPI tag to the folder name and blurb a reader sees in Postman.
#:
#: The ordering is deliberate: Auth first because nothing works without it, and Explore
#: last because it is the one folder a viewer cannot use, so putting it last means a
#: read-only user discovers the limit at the end of a successful run rather than the start.
FOLDERS: dict[str, tuple[str, str]] = {
    "auth": (
        "1. Auth",
        (
            "Log in, and see who you are. **Start here** — every other request needs "
            "the token this folder produces."
        ),
    ),
    "telemetry": (
        "2. Telemetry",
        (
            "What the plant is doing right now, and over a range. Read `/api/meta` "
            "first if you want to know which tables and metrics are legal."
        ),
    ),
    "alerting": (
        "3. Alerts",
        "The shipped rules, the live state of the engine, and what has fired recently.",
    ),
    "live": (
        "4. Live feed",
        "The WebSocket ticket path. This is where the PWA's live data comes from.",
    ),
    "explore": (
        "5. Explore (admin only)",
        (
            "Arbitrary read-only SQL against the database. A viewer gets 403 — that is "
            "the RBAC boundary working, not a fault."
        ),
    ),
}

#: Requests promoted out of their OpenAPI tag into their own folder, where the tag is too
#: coarse to be useful to a reader. Empty today, because the tags in ``main.py`` were fixed
#: to match what these endpoints actually are (``/api/explore`` was filed under
#: "telemetry", which is wrong in Swagger UI as well as here). Kept because the next
#: endpoint that needs promoting should not have to invent the mechanism.
FOLDER_OVERRIDES: dict[str, str] = {}


# --- spec --------------------------------------------------------------------


def fetch_spec(url: str) -> dict[str, Any]:
    """GET the OpenAPI document from a running API."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # fixed loopback URL, never user input
            return json.loads(response.read())
    except urllib.error.URLError as err:
        sys.exit(
            f"could not read {url}: {err}\n"
            "  the API has to be running. Start it with: ./scripts/bootstrap.sh up"
        )
    except json.JSONDecodeError as err:
        sys.exit(f"{url} did not return JSON: {err}")


def normalise(spec: dict[str, Any]) -> dict[str, Any]:
    """Pin the parts of the spec that are deployment details rather than API facts.

    Two of these would otherwise change the committed file on every run and produce a diff
    that means nothing:

    * ``servers`` is derived from the request Host header, so it is the loopback URL on one
      machine and a container name on another. Pinned to the published one.
    * ``info.version`` tracks the app version. Kept, but the version is stripped below so
      a release does not churn a file that describes the API shape.
    """
    spec = json.loads(json.dumps(spec))  # deep copy; the caller's dict is not ours to mutate
    spec["servers"] = [{"url": "http://127.0.0.1:8000", "description": "Local stack"}]
    spec["info"] = dict(spec.get("info", {}))
    spec["info"]["version"] = "1.0.0"
    spec["info"]["title"] = "Sunscope API"
    return spec


# --- helpers -----------------------------------------------------------------


def tag_of(op: dict[str, Any]) -> str:
    tags = op.get("tags") or ["other"]
    return str(tags[0])


def param_rows(op: dict[str, Any], method: str) -> list[dict[str, Any]]:
    return [p for p in op.get("parameters", []) if p.get("in") == "query"]


def header_rows(op: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in op.get("parameters", []) if p.get("in") == "header"]


def resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``$ref`` one level, returning *schema* unchanged if there is none.

    FastAPI emits ``{"$ref": "#/components/schemas/LoginRequest"}`` rather than inlining,
    so a body generator that reads ``properties`` straight off the request-body schema finds
    nothing and emits a request with no body at all. That is not hypothetical: it is
    exactly what this did to the login request, which is the one request in the collection
    that cannot work without a body.
    """
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        return schema
    return spec.get("components", {}).get("schemas", {}).get(ref.rsplit("/", 1)[-1], {})


#: Body fields that should reference an environment variable rather than carry a literal.
#:
#: The login body is the important one. If its fields are literals, then "switch the
#: username in the environment to try a viewer" is false -- the reader has to edit the
#: request body too, and the RBAC walkthrough silently does not work. Wiring the body to
#: the environment is what makes the collection's own instructions true.
BODY_VARS = {"username": "username", "password": "password"}


def example_body(op: dict[str, Any], spec: dict[str, Any]) -> str | None:
    """A request body that satisfies the schema, for the one endpoint that takes one."""
    content = (op.get("requestBody") or {}).get("content") or {}
    if "application/json" not in content:
        return None
    schema = resolve(content["application/json"].get("schema") or {}, spec)
    props = schema.get("properties") or {}
    if not props:
        return None
    body: dict[str, Any] = {}
    for name, field in props.items():
        # A field the environment already carries becomes a {{variable}} reference, so
        # Postman fills it in. Anything else becomes a visible placeholder: an empty string
        # reads as "meant to be blank" and gets pasted as-is.
        if name in BODY_VARS:
            # Doubled braces, because these are Postman variable references and not JSON.
            # A single-brace {{name}} is still valid JSON, still passes --check, and still
            # renders as a well-formed body -- it just sends the literal text "{name}" and
            # every login 401s. Replaying the collection against the live API is what caught
            # this; comparing the generator against itself never would have.
            body[name] = "{{" + BODY_VARS[name] + "}}"
            continue
        default = field.get("default")
        if default is not None:
            body[name] = default
        elif field.get("type") == "boolean":
            body[name] = False
        else:
            body[name] = f"<{name}>"
    return json.dumps(body, indent=2)


#: Query-parameter values that work against the shipped dataset. Keyed by parameter name.
#: A generated import leaves these blank, which is the difference between a collection that
#: teaches the API and one that 422s on first use.
QUERY_VALUES: dict[str, str] = {
    "table": "inverter_telemetry",
    "metric": "ac_power_w",
    "interval": "5m",
    "group_by": "inverter_id",
    "severity": "critical",
    "sql": "SELECT time, total_ac_power_w, pr_ratio FROM site_rollup ORDER BY time DESC LIMIT 10",
    "params": "",
    "start": "",
    "end": "",
}

#: Per-endpoint notes, appended to the generated description. Written by a person, which is
#: the entire reason this file is not purely generated.
NOTES: dict[str, str] = {
    "POST /api/auth/login": (
        "**Run this first.** Its test script saves the token to `{{token}}`, which every "
        "other request in this collection uses. Without it the rest return 401.\n\n"
        "The collection environment holds two accounts: `admin` (sees everything) and "
        "`viewer` (sees everything except Explore). Switch `username`/`password` in the "
        "environment, re-run this request, and watch `/api/explore` start returning 403."
    ),
    "GET /api/auth/me": (
        "Reports the caller's role. A viewer is a fully supported account, not a "
        "degraded one — see `/api/explore` for the one thing it cannot do."
    ),
    "GET /api/now": (
        "Served from the Last Value Cache, so it answers even when InfluxDB is unhappy. "
        "That is why the PWA still shows numbers during a database restart. Expect "
        "`stale: true` and an old timestamp if the simulator has stopped."
    ),
    "GET /api/summary": (
        "Site rollup plus a per-device summary over a range. This is the 'last 24 hours' "
        "number, not the instantaneous one — compare with `/api/now` and they should agree "
        "close to the present."
    ),
    "GET /api/series": (
        "The workhorse. `table` and `metric` are checked against an allowlist before any "
        "SQL is built; an unknown value is a 400 listing what is legal, not a 500.\n\n"
        "`GET /api/meta` returns that allowlist, so read it first if you want to explore."
    ),
    "GET /api/explore": (
        "**Admin only.** A viewer gets `403 {\"detail\": \"role 'viewer' may not "
        "sql:raw\"}` — that is the RBAC boundary working, not a bug.\n\n"
        "Two more limits, both deliberate: read-only SQL only, and loopback callers only. "
        "Bindings go in the `params` field as a JSON object and travel separately from the "
        "SQL text, so you can parameterise without concatenating."
    ),
    "POST /api/live-ticket": (
        "Hands back a **single-use, 30-second** ticket for the WebSocket. The long-lived "
        "JWT cannot go in a URL: a handshake carries no Authorization header, and a token "
        "in a query string ends up in access logs, proxy logs and browser history.\n\n"
        "Postman cannot drive a WebSocket from a test script, so use the ticket at "
        "`ws://127.0.0.1:8000{{path}}?ticket=...` in a WebSocket client."
    ),
    "GET /api/meta": "The allowlist: every legal `table`, `metric` and `dimension`. Start here.",
}


def describe(method: str, path: str, op: dict[str, Any]) -> str:
    parts = [op.get("description") or op.get("summary") or path]
    if op.get("summary") and op["summary"] not in parts[0]:
        parts.insert(0, f"**{op['summary']}**")
    note = NOTES.get(f"{method.upper()} {path}")
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def build_request(
    method: str, path: str, op: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    """One Postman request object."""
    query = [
        {
            "key": p["name"],
            "value": QUERY_VALUES.get(p["name"], ""),
            "description": (p.get("description") or "")[:400],
            "disabled": not QUERY_VALUES.get(p["name"], ""),
        }
        for p in param_rows(op, method)
    ]
    # Leave optional filters off by default: a request with every optional parameter
    # filled in is noisier to read than one that asks a single question.
    for row in query:
        if row["key"] in {"severity", "start", "end", "params", "group_by"}:
            row["disabled"] = True

    body = example_body(op, spec)
    request: dict[str, Any] = {
        "method": method.upper(),
        "header": [
            {"key": "Accept", "value": "application/json"},
            *[
                {"key": h["name"], "value": "", "description": (h.get("description") or "")[:200]}
                for h in header_rows(op)
            ],
        ],
        "url": {
            "raw": "{{baseUrl}}" + path + ("?" + "&".join(f"{q['key']}=" for q in query) if query else ""),
            "host": ["{{baseUrl}}"],
            "path": [seg for seg in path.split("/") if seg],
            "query": query,
        },
        "description": describe(method, path, op),
    }
    if body is not None:
        request["header"].append(
            {"key": "Content-Type", "value": "application/json", "type": "text"}
        )
        request["body"] = {"mode": "raw", "raw": body, "options": {"raw": {"language": "json"}}}

    if path == "/api/auth/login":
        # The one piece a generated import cannot produce, and the reason this collection
        # is worth having: log in once, and every later request is authenticated.
        request["event"] = [
            {
                "listen": "test",
                "script": {
                    "type": "text/javascript",
                    "exec": [
                        "// Capture the token so the rest of the collection just works.",
                        "const body = pm.response.json();",
                        "if (!body.token) {",
                        "  console.error('no token in response:', pm.response.text());",
                        "} else {",
                        "  pm.collectionVariables.set('token', body.token);",
                        "  console.log('signed in as', body.role, '- token saved');",
                        "}",
                    ],
                },
            }
        ]
    return request


def build_collection(spec: dict[str, Any]) -> dict[str, Any]:
    """The whole collection, grouped into folders by OpenAPI tag."""
    folders: dict[str, list[dict[str, Any]]] = {}
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            folders.setdefault(tag_of(op), []).append(build_request(method, path, op, spec))

    items = []
    # Iterate FOLDERS, not a hardcoded tuple. A literal list here is how "alerting" ended
    # up filed under a bare "alerting" folder while everything else got a number and a
    # blurb: the tag was renamed in main.py, the tuple was not, and the mismatch is silent
    # because an unknown tag still produces a valid folder.
    for tag, (name, blurb) in FOLDERS.items():
        if tag not in folders:
            continue
        # POST first within a folder: the login request is the one a reader runs first, and
        # sorting by "is it a POST" is a cheaper proxy for that than threading an order
        # through the schema, which does not record it.
        requests = sorted(
            folders.pop(tag), key=lambda r: (r["method"] != "POST", r["url"]["path"][-1])
        )
        items.append(
            {
                "name": name,
                "description": blurb,
                "item": requests,
                # Bearer at folder level, so a new request added here is authenticated by
                # default rather than being the one that forgets.
                "auth": {
                    "type": "bearer",
                    "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
                },
            }
        )
    # A tag with no FOLDERS entry is a mistake, not a fallback. It happened: "alerting"
    # was renamed in main.py, the folder list was not, and the result was a valid
    # collection with a bare "alerting" folder and no blurb -- which Postman renders
    # perfectly happily and nobody notices until a reader gives up.
    unfiled = sorted(set(folders) - set(FOLDERS) - set(FOLDER_OVERRIDES))
    if unfiled:
        listed = ", ".join(unfiled)
        raise SystemExit(
            f"these OpenAPI tags have no entry in FOLDERS, so their requests would land "
            f"in a bare folder: {listed}\n"
            f"  add each to FOLDERS (name + blurb) or to FOLDER_OVERRIDES."
        )

    # Postman renders this as the collection's own description, so it is the first thing a
    # reader sees. Written here rather than in a committed file so that it cannot drift out
    # of sync with the folders above.
    readme = """# Sunscope API

Generated from `api/openapi.json` by `scripts/gen-postman.py`. Do not edit by hand — the
next run overwrites it, and `--check` fails the build if it drifts.

## Using it

1. Import `Sunscope.postman_collection.json`, and `Sunscope.postman_environment.json`
   alongside it.
2. Fill in `password` in the environment. `username` defaults to `admin`.
3. Run **1. Auth → Login** once. Its test script saves the token.
4. Everything else works.

## Seeing the role boundary

Switch the environment's `username` to `viewer`, re-run Login, then open **5. Explore**.
You get 403. Switch back to `admin` and it returns 200. That one difference is the whole
role system — see docs/13-postman.md.

## If a request 401s

The token expired (8 hours) or Login has not been run in this session. Tokens live in a
collection variable, which is memory-only, so re-running Login is the fix. It is always
safe to do.
"""

    return {
        "info": {
            "name": "Sunscope API",
            "description": readme,
            "schema": COLLECTION_SCHEMA,
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
        },
        "variable": [{"key": "token", "value": "", "type": "string"}],
        "item": items,
    }


def build_environment() -> dict[str, Any]:
    return {
        "id": "sunscope-local",
        "name": "Sunscope (local)",
        "values": [
            {"key": "baseUrl", "value": "http://127.0.0.1:8000", "enabled": True, "type": "default"},
            {"key": "username", "value": "admin", "enabled": True, "type": "default"},
            # Deliberately blank. A collection that ships a working password is a published
            # credential, which is the same mistake as committing a password digest.
            {
                "key": "password",
                "value": "",
                "enabled": True,
                "type": "secret",
            },
        ],
        "_postman_variable_scope": "environment",
        "_postman_exported_at": "generated by scripts/gen-postman.py",
        "_postman_exported_using": "gen-postman.py",
    }


# --- entry point -------------------------------------------------------------


def render(spec: dict[str, Any]) -> dict[Path, str]:
    collection = build_collection(spec)
    pretty = json.dumps(collection, indent=2, ensure_ascii=False) + "\n"
    env = json.dumps(build_environment(), indent=2, ensure_ascii=False) + "\n"
    return {
        SPEC_PATH: json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
        COLLECTION_PATH: pretty,
        ENV_PATH: env,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the files are stale")
    parser.add_argument("--spec-only", action="store_true", help="refresh only openapi.json")
    parser.add_argument("--api", default=os.environ.get("SUNSCOPE_OPENAPI", DEFAULT_API))
    args = parser.parse_args()

    spec = normalise(fetch_spec(args.api))
    files = render(spec)
    if args.spec_only:
        files = {SPEC_PATH: files[SPEC_PATH]}

    if args.check:
        problems = []
        for path, content in sorted(files.items()):
            if not path.exists():
                problems.append(f"missing: {path.relative_to(ROOT)}")
            elif path.read_text() != content:
                problems.append(f"stale:   {path.relative_to(ROOT)}")
        for path in (SPEC_PATH, COLLECTION_PATH, ENV_PATH):
            if path not in files and path.exists() and not args.spec_only:
                problems.append(f"orphan:  {path.relative_to(ROOT)}")
        if problems:
            print("the Postman collection is out of date; run:")
            print("  uv run --project api python scripts/gen-postman.py")
            for problem in problems:
                print(f"  {problem}")
            return 1
        count = len(build_collection(spec)["item"])
        print(f"the Postman collection is current ({count} folders, spec + collection + env)")
        return 0

    for path, content in sorted(files.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"  wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
