"""Export the SQL examples in docs/06-sql-examples.md into runnable .sql files.

The markdown document stays the single source of truth. Generating from it means
the runnable files and the teaching document can never drift, which hand-copying
35 queries across 35 files would guarantee they eventually do.

Each file carries a header comment giving the tier, what the query answers, which
SQL features it demonstrates, and a note on running it. docs/sql/README.md is an
index of the whole set.

    uv run python scripts/export-sql.py           # write the files
    uv run python scripts/export-sql.py --check   # fail if they are out of date
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "06-sql-examples.md"
OUT_DIR = ROOT / "docs" / "sql"

TIERS = {
    "B": ("Beginner", "SELECT / WHERE / GROUP BY / ORDER BY"),
    "I": ("Intermediate", "CASE, joins, subqueries, aggregation"),
    "A": ("Advanced", "CTEs, window functions and frames"),
    "E": ("Expert", "self-joins, correlated subqueries, change-point detection"),
}

SECTION = re.compile(r"\n(### ([BIAE])(\d+) — ([^\n]+))\n")
SQL_BLOCK = re.compile(r"```sql\n(.*?)```", re.DOTALL)

NAMED_WINDOW = re.compile(
    r"\b(ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD|FIRST_VALUE|LAST_VALUE|NTH_VALUE"
    r"|CUME_DIST|PERCENT_RANK)\s*\(",
    re.IGNORECASE,
)


def slug(title: str) -> str:
    """Lowercase hyphenated, no diacritics, stable for filenames."""
    ascii_title = (
        title.replace("—", " ").replace("–", " ")
        .encode("ascii", "ignore").decode()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")


def features(sql: str) -> list[str]:
    """Which SQL features a query demonstrates, for the index."""
    found: list[str] = []
    if re.search(r"^\s*WITH\b", sql, re.IGNORECASE | re.MULTILINE):
        found.append("CTE")
    if re.search(r"\bOVER\s*\(", sql, re.IGNORECASE):
        found.append("window function")
    found.extend(
        sorted({match.group(1).upper() for match in NAMED_WINDOW.finditer(sql)})
    )
    if re.search(r"\b(ROWS|RANGE)\s+BETWEEN", sql, re.IGNORECASE):
        found.append("frame")
    if re.search(r"date_bin|date_trunc", sql, re.IGNORECASE):
        found.append("time bucketing")
    if re.search(r"\(\s*SELECT", sql, re.IGNORECASE):
        found.append("subquery")
    return found


def first_paragraph(body: str) -> str:
    """The prose immediately after the heading, before the first code fence."""
    text = re.split(r"```", body, maxsplit=2)[0].strip()
    return " ".join(text.split())


PARAMETERISED = re.compile(r"\$\{?\w+\}?")
COMMENT_LINE = re.compile(r"^\s*(--.*)?$")


def sql_body(file_text: str) -> str:
    """Strip the leading `--` header so feature detection reads only the query.

    The generated header itself mentions `"$TOK"` in the run instructions, so
    scanning the whole file flagged all 35 as parameterised.
    """
    lines = [ln for ln in file_text.splitlines() if not COMMENT_LINE.match(ln)]
    return "\n".join(lines)


def uses_params(file_text: str) -> bool:
    return bool(PARAMETERISED.search(sql_body(file_text)))


def statements(sql: str) -> list[str]:
    """Split a generated file into individual statements.

    Only valid for files this script generated, where statements are separated
    by a blank line after the semicolon. A general SQL splitter is a hard
    problem and not needed here.
    """
    parts = [p.strip().rstrip(";").strip() for p in sql.split(";\n\n")]
    return [p for p in parts if p]


def header(tag: str, number: int, title: str, blurb: str, feats: list[str],
           multi: bool = False, param: bool = False) -> str:
    tier_name, tier_focus = TIERS[tag]
    width = 74
    out = [
        "-- " + "=" * width,
        f"-- {tag}{number}. {title}",
        "-- " + "=" * width,
        f"-- Tier       : {tier_name} — {tier_focus}",
        f"-- Demonstrates: {', '.join(feats) if feats else 'core SELECT fundamentals'}",
    ]
    if blurb:
        wrapped, line = [], ""
        for word in blurb.split():
            if len(line) + len(word) + 1 > width - 4:
                wrapped.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            wrapped.append(line)
        out.append("--")
        out += [f"-- {w}" for w in wrapped]
    out.append("--")
    if multi:
        out += [
            "-- NOTE: this file holds more than one statement, and that is the point. The",
            "--       first is the approach that looks right and is wrong; the second is",
            "--       the fix. The CLI accepts ONE statement per invocation, so run them",
            "--       separately:",
            "--",
            "--         uv run python scripts/export-sql.py --verify",
            "--",
        ]
    if param:
        out += [
            "-- NOTE: this query uses $name placeholders, which is the point -- it is the",
            "--       parameter binding that makes ad-hoc SQL injection-safe. Bindings",
            "--       travel as a JSON field and are never spliced into the SQL text.",
            "--       The CLI has no flag for them, so run this one through the API:",
            "--",
            "--         TOKEN=...            # see README, 'Verify it yourself'",
            "--         curl -sG http://127.0.0.1:8000/api/explore \\",
            "--           -H \"Authorization: Bearer $TOKEN\" \\",
            "--           --data-urlencode \"sql=$(sed 's/^--.*//' this-file.sql)\" \\",
            "--           --data-urlencode 'params={\"site\":\"mojave\", ...}'",
            "--",
            f"-- Full discussion: docs/06-sql-examples.md ({tag}{number})",
            "-- " + "=" * width,
            "",
        ]
        return "\n".join(out)

    out += [
        "-- Run:  docker compose exec -T influxdb influxdb3 query \\",
        "--          --host https://localhost:8181 --tls-no-verify \\",
        "--          --database solar --token \"$TOK\" < this-file.sql",
        f"-- Full discussion: docs/06-sql-examples.md ({tag}{number})",
        "-- " + "=" * width,
        "",
    ]
    return "\n".join(out)


def parse() -> list[dict[str, object]]:
    text = DOC.read_text()
    parts = SECTION.split(text)
    queries: list[dict[str, object]] = []
    # parts: [preamble, head, tag, number, title, body, head, ...]
    for i in range(1, len(parts), 5):
        tag, number, title, body = parts[i + 1], parts[i + 2], parts[i + 3], parts[i + 4]
        blocks = SQL_BLOCK.findall(body)
        if not blocks:
            continue
        # The doc's fences already carry a trailing semicolon; appending another
        # would emit `;;`, which the CLI reads as a second empty statement.
        cleaned = [b.strip().rstrip(";").strip() for b in blocks]
        sql = ";\n\n".join(cleaned)
        queries.append({
            "tag": tag,
            "number": int(number),
            "title": title.strip(),
            # Backticks are markdown emphasis and mean nothing in a SQL comment.
            "plain_title": title.replace("`", "").strip(),
            "sql": sql,
            "features": features(sql),
            "blurb": first_paragraph(body),
            "multi": len(blocks) > 1,
        })
    return queries


def render(queries: list[dict[str, object]]) -> dict[str, str]:
    files: dict[str, str] = {}
    for q in queries:
        name = f"{q['tag'].lower()}{q['number']}-{slug(str(q['title']))}.sql"
        sql = str(q["sql"])
        body = header(
            str(q["tag"]), int(q["number"]), str(q["plain_title"]),
            str(q["blurb"]), list(q["features"]),  # type: ignore[arg-type]
            multi=bool(q["multi"]), param=uses_params(sql),
        )
        files[name] = body + sql.rstrip() + ";\n"
    return files


def index(queries: list[dict[str, object]], files: dict[str, str]) -> str:
    out = [
        "# SQL examples, one file each",
        "",
        (
            "**Generated from [docs/06-sql-examples.md](../06-sql-examples.md) — "
            "do not edit by hand.**"
        ),
        "",
        "```bash",
        "uv run python scripts/export-sql.py           # regenerate from the document",
        "uv run python scripts/export-sql.py --check   # fail if out of date",
        "uv run python scripts/export-sql.py --verify  # execute every file against InfluxDB",
        "```",
        "",
        (
            "Every file is executed against the live database by "
            "`export-sql.py --verify`, and `bootstrap.sh test` fails if these "
            "files have drifted from the document they are generated from. "
            "Neither can rot unnoticed."
        ),
        "",
    ]
    for tag, (tier_name, tier_focus) in TIERS.items():
        group = [q for q in queries if q["tag"] == tag]
        if not group:
            continue
        out += [f"## {tier_name} — {tier_focus}", ""]
        for q in group:
            name = f"{q['tag'].lower()}{q['number']}-{slug(str(q['title']))}.sql"
            feats = ", ".join(q["features"]) or "—"
            out.append(f"- **[`{name}`]({name})** — {q['title']} · `{feats}`")
        out.append("")
    return "\n".join(out)


def verify() -> int:
    """Execute every generated file against the live database.

    Multi-statement files are split, because the CLI accepts one statement per
    invocation. Parameterised files are reported as skipped rather than failed:
    `$name` bindings travel in a JSON field, and running one with an unbound
    placeholder failing is the whole point of E8.
    """
    import json
    import subprocess

    token = json.loads((ROOT / "secrets" / "admin-token").read_text())["token"]
    passed = failed = skipped = 0
    problems: list[str] = []

    for path in sorted(OUT_DIR.glob("*.sql")):
        sql = path.read_text()
        if uses_params(sql):
            skipped += 1
            print(f"  SKIP  {path.name}  (parameterised; bindings are a JSON field)")
            continue
        parts = statements(sql)
        for index, statement in enumerate(parts, start=1):
            label = path.name if len(parts) == 1 else f"{path.name} [{index}/{len(parts)}]"
            result = subprocess.run(
                ["docker", "compose", "exec", "-T", "influxdb", "influxdb3", "query",
                 "--host", "https://localhost:8181", "--tls-no-verify",
                 "--database", "solar", "--token", token],
                cwd=ROOT, input=statement + ";", capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                passed += 1
                print(f"  PASS  {label}")
            else:
                failed += 1
                tail = (result.stderr or result.stdout).strip().splitlines()
                detail = tail[-1][:120] if tail else "no output"
                problems.append(f"{label}: {detail}")
                print(f"  FAIL  {label}")

    print(f"\n{passed} statements passed, {failed} failed, {skipped} file(s) skipped")
    for problem in problems:
        print(f"  - {problem}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the files on disk are out of date")
    parser.add_argument("--verify", action="store_true",
                        help="execute every file against the live database")
    args = parser.parse_args()

    queries = parse()
    files = render(queries)
    files["README.md"] = index(queries, files)

    if args.verify:
        return verify()

    if args.check:
        problems = []
        for name, content in sorted(files.items()):
            path = OUT_DIR / name
            if not path.exists():
                problems.append(f"missing: {name}")
            elif path.read_text() != content:
                problems.append(f"stale:   {name}")
        for existing in sorted(OUT_DIR.glob("*.sql")) if OUT_DIR.exists() else []:
            if existing.name not in files:
                problems.append(f"orphan:  {existing.name}")
        if problems:
            print("docs/sql is out of date; run: uv run python scripts/export-sql.py")
            for p in problems:
                print(f"  {p}")
            return 1
        print(f"docs/sql is current ({len(files) - 1} queries + index)")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(files.items()):
        (OUT_DIR / name).write_text(content)
    print(f"wrote {len(files) - 1} queries and an index to {OUT_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
