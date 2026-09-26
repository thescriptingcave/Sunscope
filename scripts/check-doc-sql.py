"""Execute every fenced ```sql block in the docs against the live database.

Docs that drift from reality are worse than no docs, so this runs them for real.
Blocks that intentionally demonstrate a *failure* (the WRONG query in I5) are
skipped by tagging them with a `-- EXPECT-FAILURE: reason` comment.

Run: uv run --project api python scripts/check-doc-sql.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
TOKEN = json.loads((Path(__file__).resolve().parents[1] / "secrets/admin-token").read_text())["token"]

# ```sql fences that are placeholders, not runnable statements.
SKIP_MARKERS = (
    "EXPECT-FAILURE",
    "EXPECT_FAILURE",
    "not runnable",
    "<table>",
    "$start_time",
    "{table}",
)


def sql_blocks() -> list[tuple[Path, int, str]]:
    blocks: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"^```sql\n(.*?)^```", re.MULTILINE | re.DOTALL)
    for path in sorted(DOCS.glob("*.md")):
        for match in pattern.finditer(path.read_text()):
            line = path.read_text()[: match.start()].count("\n") + 1
            blocks.append((path, line, match.group(1)))
    return blocks


def run(sql: str) -> tuple[bool, str]:
    result = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "influxdb",
            "influxdb3", "query", sql.strip().rstrip(";"),
            "--host", "https://localhost:8181", "--tls-no-verify",
            "--database", "solar", "--token", TOKEN,
        ],
        cwd=DOCS.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def main() -> int:
    passed = failed = skipped = 0
    failures: list[str] = []

    for path, line, block in sql_blocks():
        # Take the first statement only; a fence may hold a setup + a query.
        statements = [s.strip() for s in block.split(";") if s.strip()]
        runnable = [
            s for s in statements
            if s.lower().startswith(("select", "with", "show", "explain"))
            and not any(marker in block for marker in SKIP_MARKERS)
            and not re.search(r"\$\{|\{[a-z_]+\}|<[a-z_]+>", s)
        ]
        if not runnable:
            skipped += 1
            continue

        ok, output = run(runnable[-1])
        label = f"{path.name}:{line}"
        if ok:
            passed += 1
            print(f"  PASS  {label}")
        else:
            failed += 1
            first = output.splitlines()[-1][:150] if output else "no output"
            failures.append(f"{label}: {first}")
            print(f"  FAIL  {label}")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped (placeholders/intentional)")
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
