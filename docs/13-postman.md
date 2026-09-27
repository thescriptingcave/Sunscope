# 13 — The Postman collection

A ready-made [Postman](https://www.postman.com/) collection for the Sunscope API, so you
can drive the whole system without writing a request by hand.

```
docs/postman/Sunscope.postman_collection.json    the collection
docs/postman/Sunscope.postman_environment.json   baseUrl, username, password
api/openapi.json                                 the schema both are generated from
```

## Importing it

1. In Postman: **Import** → drag in `Sunscope.postman_collection.json`. Tick
   `Sunscope.postman_environment.json` at the same time if you want the environment
   pre-wired; otherwise set `baseUrl` yourself.
2. Select the **Sunscope (local)** environment in the environment picker, top right.
3. Open the environment and fill in **`password`**. Get it from your `.env`:
   ```bash
   grep API_ADMIN_PASSWORD .env
   ```
   `username` defaults to `admin`. `baseUrl` is `http://127.0.0.1:8000`.
4. Open **1. Auth → Login** and hit **Send**.

That is the whole setup. The Login request has a test script that copies the returned token
into a collection variable, and every other request sends it as a `Bearer` header, so
everything works from here on.

**The token is the one thing to re-run.** It lives in a Postman *collection variable*,
which lives in the running app's memory — it is not written to disk, and it expires after
8 hours. If requests start returning 401, re-run Login. Doing that is always safe.

## What is in it

| Folder | What it covers |
|---|---|
| **1. Auth** | `POST /api/auth/login`, `GET /api/auth/me`. Start here. |
| **2. Telemetry** | `/api/now`, `/api/summary`, `/api/series`, `/api/strings`, `/api/events`, `/api/meta` |
| **3. Alerts** | `/api/alerts`, `/api/alert-rules`, `/api/alert-stats` |
| **4. Live feed** | `POST /api/live-ticket` — the single-use ticket for the live WebSocket |
| **5. Explore** | `GET /api/explore` — arbitrary read-only SQL. **Admin only.** |

Optional query parameters are present but switched off, so each request asks one question.
Turn one on by ticking the `?` next to it — that is also how you see the allowlist
rejection for an unknown `table` or `metric`, which is a `400` listing what is legal.

## Seeing the role boundary for yourself

This is the one thing worth doing, and it takes about a minute.

1. Run **1. Auth → Login** as `admin`. Open **5. Explore → Explore** → **200**, with rows.
2. In the environment, change **`username`** to `viewer`.
3. Re-run **Login**. (The password is the same only if you gave both accounts the same one
   — see [04-security §4.7a](04-security.md) for how to add a viewer account.)
4. Open **5. Explore** again → **`403 {"detail":"role 'viewer' may not sql:raw"}`**.

Everything else keeps returning `200`. That difference — one endpoint, one role — is the
entire RBAC system. See [04-security](04-security.md) for why that endpoint in particular
is where the line is drawn.

## Two limits on `/api/explore` that are not bugs

- **Read-only SQL only.** `DROP`, `DELETE`, `UPDATE` and `INSERT` are rejected before they
  reach the database.
- **Loopback callers only.** Requests from another machine on your network get `403`, even
  with an admin token. A viewer token gets `403` as well, but with a different message.

Bindings: put them in the `params` field as a JSON object (`{"site":"mojave"}`). They travel
to InfluxDB separately from the SQL text, which is what makes ad-hoc SQL safe to write —
never concatenate a value into the `sql` string.

## The live WebSocket

`POST /api/live-ticket` returns a single-use ticket that is valid for 30 seconds, and a
`path` to use it:

```json
{ "ticket": "...", "expires_in": 30, "path": "/api/live", "stream": true }
```

Connect to `ws://127.0.0.1:8000/api/live?ticket=...`. The long-lived JWT cannot go in the
URL: a WebSocket handshake carries no `Authorization` header, and a token in a query string
ends up in access logs, proxy logs and browser history. Postman cannot drive a WebSocket
from a test script, so use any WebSocket client for this one.

## The collection is generated — do not hand-edit it

Both the collection and `api/openapi.json` are derived from the schema the running API
publishes at `/openapi.json`, by `scripts/gen-postman.py`. Editing either by hand works
until the next regeneration silently discards it.

To change what is in the collection, change the generator:

```bash
uv run --project api python scripts/gen-postman.py            # rewrite the files
uv run --project api python scripts/gen-postman.py --check    # fail if stale
```

`--check` runs in `./scripts/bootstrap.sh test` and in CI, so an endpoint added without
regenerating fails the build. That check exists because a request for an endpoint that no
longer exists is still a *valid* Postman request — nothing complains until you run it and
get a `404` and assume the API is broken.

Three things in the collection are written by a person rather than derived, and are the
reason it is worth more than Postman's own OpenAPI import:

- the **Login test script** that captures the token, so the collection works from a cold
  start with one click;
- the **`{{username}}` / `{{password}}` references** in the login body, so switching roles
  is an environment edit rather than a body edit — which is what makes the walkthrough
  above actually work as written;
- the **per-request notes**, including what a good answer looks like.

If you add an endpoint whose OpenAPI tag has no folder, the generator refuses to write
rather than dropping the requests into an unnamed folder.
