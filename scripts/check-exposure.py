#!/usr/bin/env python3
"""Verify the stack's exposure posture, and test it from a real non-loopback address.

WHY THIS EXISTS
---------------
Everything in this project had only ever been exercised on ``127.0.0.1``. That hides a
specific class of bug: code that infers "is this request local?" from the source address.
``TestClient`` pins the peer to ``127.0.0.1`` and Docker's own traffic arrives as the bridge
gateway, so neither reproduces what a genuinely remote client looks like.

That is not hypothetical. ``_is_local_address`` in ``main.py`` treats **all of RFC 1918 as
local**, because the Docker bridge rewrites the source address to ``172.22.0.1`` and a naive
loopback check rejects every real request. The consequence is that any client on the LAN is
treated as local and is granted ``/api/explore`` -- an endpoint that runs arbitrary SQL with an
**admin-scoped** InfluxDB token, because InfluxDB 3 Core has no permission-scoped tokens
(see docs/04-security.md 4.4).

So this script does two jobs:

1. **Audit** the current bindings and report anything reachable off-loopback.
2. **Test** the security model from a real non-loopback address, by starting a *second* API
   instance on a spare port bound to ``0.0.0.0``. Nothing about the running stack is
   modified, and the instance is killed on the way out even on failure.

Usage:
    uv run --project api python scripts/check-exposure.py            # audit only
    uv run --project api python scripts/check-exposure.py --test     # audit + live test

Exits non-zero if anything is exposed that should not be, or if a security control that is
supposed to hold off-loopback does not.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Ports that must never be reachable off-loopback, and why. The broker is the dangerous
#: one: it is unauthenticated and accepts both publish and subscribe.
MUST_BE_LOOPBACK = {
    1883: "EMQX MQTT -- unauthenticated, accepts publish AND subscribe",
    8083: "EMQX WebSocket -- same broker, HTTP-reachable",
    8181: "InfluxDB 3 -- holds the admin token, no read-only tokens exist",
}

#: Ports intended to be published if the stack is ever put behind a proxy.
MAY_BE_PUBLISHED = {
    8000: "FastAPI -- the PWA and the REST API, JWT-protected",
    3000: "Grafana",
}

#: The port the throwaway test instance listens on. Deliberately not 8000, so this can run
#: against a live stack without disturbing it.
TEST_PORT = 8011


# --- helpers -----------------------------------------------------------------


def host_addresses() -> list[str]:
    """Non-loopback IPv4 addresses of this machine."""
    found: set[str] = set()
    # `ipconfig getifaddr` is the reliable route on macOS; ifconfig parsing is the
    # portable fallback and also works in a container.
    for iface in ("en0", "en1", "bridge100"):
        try:
            # returncode is inspected below: a host without en1 is normal.
            out = subprocess.run(  # noqa: PLW1510
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                found.add(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    try:

        out = subprocess.run(  # noqa: PLW1510
            ["ifconfig"], capture_output=True, text=True, timeout=10
        )
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "inet" and not parts[1].startswith("127."):
                found.add(parts[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(found)


def listening_on(port: int) -> list[str]:
    """Addresses a port is bound to, from lsof."""
    try:
        # A port with nothing listening makes lsof exit non-zero; that is a result,
        # not a failure.
        out = subprocess.run(  # noqa: PLW1510
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    addrs = set()
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 8 and ":" in parts[8]:
            addrs.add(parts[8].rsplit(":", 1)[0])
    return sorted(addrs)


def http(url: str, token: str | None = None, timeout: float = 10.0):
    """GET and return (status, body). Never raises for an HTTP error status."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def login(base: str, timeout: float = 20.0) -> tuple[int, str]:
    """POST real credentials and return (status, token)."""
    payload = json.dumps({"username": "admin", "password": admin_password()}).encode()
    request = urllib.request.Request(
        f"{base}/api/auth/login", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode()).get("token", "")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def admin_password() -> str:
    env = ROOT / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("API_ADMIN_PASSWORD="):
            return line.split("=", 1)[1].split(" #")[0].strip()
    raise SystemExit("API_ADMIN_PASSWORD not found in .env")


# --- 1. audit ----------------------------------------------------------------


def audit() -> list[str]:
    print("==> Binding audit")
    problems: list[str] = []
    for port, why in MUST_BE_LOOPBACK.items():
        addrs = listening_on(port)
        if not addrs:
            print(f"    {port:<5} not listening")
            continue
        off = [a for a in addrs if a not in ("127.0.0.1", "::1")]
        if off:
            print(f"    {port:<5} EXPOSED on {', '.join(off)}  <- {why}")
            problems.append(f"port {port} is bound to {', '.join(off)}")
        else:
            print(f"    {port:<5} loopback only  (ok)")
    for port, what in MAY_BE_PUBLISHED.items():
        addrs = listening_on(port)
        off = [a for a in addrs if a not in ("127.0.0.1", "::1")]
        state = f"EXPOSED on {', '.join(off)}" if off else "loopback only"
        print(f"    {port:<5} {state}  ({what})")
        if off:
            # Not a failure on its own -- publishing these is the supported way to put the
            # PWA behind a tunnel -- but it is reported, and the warning below applies.
            print("          if this is intentional, the checklist in docs/04-security.md applies")
    return problems


# --- 2. live test from a non-loopback address -------------------------------


def test_off_loopback(address: str) -> list[str]:
    """Start a second API instance on 0.0.0.0 and probe it from the LAN address."""
    print(f"==> Off-loopback security test via {address}")
    failures: list[str] = []

    env = dict(os.environ)
    env.update({
        "API_HOST": "0.0.0.0",
        "API_PORT": str(TEST_PORT),
        "ALERTS_ENABLED": "false",   # no broker subscription needed for this probe
    })
    proc = subprocess.Popen(
        [
            "uv", "run", "--project", "api", "python", "-c",
            (
                "import uvicorn; uvicorn.run('solar_api.main:app', "
                f"host='0.0.0.0', port={TEST_PORT}, log_level='warning')"
            ),
        ],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def cleanup() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    base = f"http://{address}:{TEST_PORT}"
    try:
        # Wait for the instance to answer.
        for _ in range(40):
            if proc.poll() is not None:
                print(f"    instance exited early (rc={proc.returncode})")
                return ["the throwaway API instance did not start"]
            status, _ = http(f"{base}/healthz", timeout=2)
            if status == 200:
                break
            time.sleep(0.5)
        else:
            return ["the throwaway API instance never became ready"]

        if listening_on(TEST_PORT) and not any(
            a not in ("127.0.0.1", "::1") for a in listening_on(TEST_PORT)
        ):
            failures.append("the test instance did not actually bind off-loopback, so the "
                            "test proved nothing")

        # 1. Unauthenticated read must be refused, off-loopback as much as on.
        status, _ = http(f"{base}/api/now")
        print(f"    GET /api/now            unauthenticated -> {status}")
        if status != 401:
            failures.append(f"/api/now returned {status} without a token; expected 401")

        # 2. The PWA's own login must work off-loopback, so the dashboard is usable if
        #    deliberately published.
        status, token = login(base)
        print(f"    POST /api/auth/login    -> {status}, token {'issued' if token else 'MISSING'}")
        if status != 200 or not token:
            failures.append(
                f"login off-loopback returned {status}; the remainder of the probe could "
                "not run"
            )
        else:
            # 3. An authenticated read works.
            status, _ = http(f"{base}/api/now", token=token)
            print(f"    GET /api/now            authenticated   -> {status}")
            if status != 200:
                failures.append(f"authenticated /api/now returned {status} off-loopback")

            # 4. THE ONE THAT MATTERS. Raw SQL with an admin token must be refused from a
            #    non-loopback address. If this returns 200, every host on the LAN can run
            #    arbitrary SQL against the database.
            probe = f"{base}/api/explore?sql=SELECT%20COUNT(*)%20AS%20n%20FROM%20site_rollup"
            status, _ = http(probe, token=token)
            print(f"    GET /api/explore        raw SQL         -> {status}")
            if status == 200:
                failures.append(
                    "/api/explore returned 200 to a non-loopback client. _is_local_address "
                    "treats all of RFC 1918 as local, so any host on the LAN can run "
                    "arbitrary SQL with an admin-scoped token."
                )
            elif status != 403:
                failures.append(f"/api/explore returned {status} off-loopback; expected 403")
    finally:
        cleanup()
    return failures


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true",
                        help="also start a throwaway API instance and probe it off-loopback")
    args = parser.parse_args()

    print("Sunscope exposure check")
    print("=" * 60)

    problems = audit()

    addresses = host_addresses()
    if args.test:
        if not addresses:
            print("\n==> Off-loopback test")
            print("    no non-loopback address on this host; cannot test the remote path")
            problems.append("no non-loopback address available for the off-loopback test")
        else:
            problems.extend(test_off_loopback(addresses[0]))
    else:
        print(f"\n  non-loopback addresses: {', '.join(addresses) or 'none'}")
        print("  pass --test to start a throwaway instance and probe it from one of those")

    print("\n" + "=" * 60)
    if problems:
        print(f"FAIL: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("PASS: nothing is exposed off-loopback, and the security model holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
