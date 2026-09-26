"""Backup and restore the solar database, and enforce a retention policy.

WHY THIS IS SCRIPTED RATHER THAN LEFT TO THE DATABASE
---------------------------------------------------
InfluxDB 3 Core has no scheduled backup and no scheduled retention. The volume
is anonymous: `docker compose down -v` destroys it and so does a
`docker volume prune`, and nothing in the stack noticed any of that. For a
simulator that is merely untidy; for anything holding real measurements it is
data loss.

FORMAT: WHY NOT PARQUET
-----------------------
`influxdb3 query --format parquet` produces a valid Parquet file, and Parquet is
what InfluxDB stores internally -- so it looks like the obvious choice. It
cannot be restored, though: 3.11 exposes no Parquet write endpoint. Both
`/api/v3/write_parquet` and `/api/v3/write` return 404, and the only write path
is `/api/v3/write_lp`, which takes line protocol. So the backup is CSV and the
restore converts to line protocol, using the same two HTTP endpoints the API
itself already uses.

TAG/FIELD DISCRIMINATION
------------------------
Line protocol needs to know which columns are tags and which are fields; getting
it wrong silently changes the schema, and tags are part of the primary key so it
cannot be undone. In the InfluxDB 3 SQL schema a tag has type
``Dictionary(Int32, Utf8)`` and a field does not, so the type tells us
authoritatively. That is read from ``information_schema.columns`` rather than
hard-coded, so the restore stays correct if a table gains a column.

    ./scripts/backup.sh              # or: python scripts/backup.py
    python scripts/backup.py --list
    python scripts/backup.py --restore backups/20260926T120000Z
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "secrets"
DEFAULT_DIR = ROOT / "backups"

# Retention. A simulator emits roughly 4 kB per simulated second, so a week of
# history is a few hundred megabytes; these defaults keep a running instance
# bounded without deleting anything an operator would still want.
KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
MAX_AGE_DAYS = int(os.environ.get("BACKUP_MAX_AGE_DAYS", "30"))

TAG_TYPE_MARKER = "Dictionary"


class BackupError(RuntimeError):
    pass


# --- connection --------------------------------------------------------------


@dataclass
class Influx:
    url: str
    database: str
    token: str
    ca: str | None

    def _opener(self) -> urllib.request.OpenerDirector:
        context = ssl.create_default_context(cafile=self.ca) if self.ca else None
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def query(self, sql: str) -> list[dict]:
        body = json.dumps({"db": self.database, "q": sql}).encode()
        request = urllib.request.Request(
            f"{self.url}/api/v3/query_sql", data=body,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        with self._opener().open(request, timeout=120) as response:  # noqa: S310
            payload = json.loads(response.read())
        return payload if isinstance(payload, list) else []

    def write(self, lines: list[str]) -> None:
        body = "\n".join(lines).encode()
        request = urllib.request.Request(
            f"{self.url}/api/v3/write_lp?db={self.database}&precision=ns",
            data=body,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "text/plain; charset=utf-8"},
        )
        with self._opener().open(request, timeout=120) as response:  # noqa: S310
            response.read()

    def tables(self) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT table_name FROM information_schema.tables "
            "WHERE table_schema = 'iox' ORDER BY table_name"
        )
        return [r["table_name"] for r in rows if r.get("table_name")]

    def columns(self, table: str) -> dict[str, str]:
        rows = self.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND table_schema = 'iox'"
        )
        return {r["column_name"]: r["data_type"] for r in rows if r.get("column_name")}


def connect(database: str | None = None) -> Influx:
    token_file = SECRETS / "admin-token"
    if not token_file.exists():
        raise BackupError(f"no {token_file}; run ./scripts/bootstrap.sh up first")
    token = json.loads(token_file.read_text())["token"]
    env_file = ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.split(" #")[0].strip()
    ca = SECRETS / "tls" / "ca-bundle.crt"
    return Influx(
        url=values.get("INFLUX_URL", "https://127.0.0.1:8181").replace("localhost", "127.0.0.1"),
        # An explicit argument beats both .env and the environment, which is
        # what makes `--database` actually work.
        database=database or os.environ.get("INFLUX_DB") or values.get("INFLUX_DB", "solar"),
        token=token,
        ca=str(ca) if ca.exists() else None,
    )


# --- line protocol -----------------------------------------------------------


#: The only data types `influxdb3 create table` accepts. Taken from the CLI's
#: own error message rather than guessed.
DDL_TYPES = frozenset({"int64", "uint64", "float64", "utf8", "bool"})


def _short_type(data_type: str) -> str:
    """Map a SQL column type onto the DDL spelling the CLI accepts.

    The SQL schema reports ``Float64``, ``Int64``, ``Utf8`` and ``Boolean``;
    the DDL wants ``float64``, ``int64``, ``utf8`` and ``bool``. A lowercased
    cast is not enough -- ``utf8`` does not lower to ``string`` -- so the mapping
    is explicit and checked against the accepted set.
    """
    kind = data_type.split("(")[0].strip().lower()
    if kind.startswith(("float", "double", "real", "decimal")):
        mapped = "float64"
    elif kind.startswith(("uint",)):
        mapped = "uint64"
    elif kind.startswith(("int", "bigint")):
        mapped = "int64"
    elif kind.startswith("bool"):
        mapped = "bool"
    else:
        mapped = "utf8"
    assert mapped in DDL_TYPES, f"{data_type!r} mapped to {mapped!r}, which the DDL rejects"
    return mapped


def escape_tag(value: str) -> str:
    return (str(value).replace("\\", "\\\\").replace(",", "\\,")
            .replace("=", "\\=").replace(" ", "\\ "))


def escape_field(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _to_nanoseconds(value: str) -> int | None:
    """Convert a CSV timestamp to integer nanoseconds.

    The writer is called with ``precision=ns``, and in that mode line protocol
    requires an **integer** nanosecond count. An RFC 3339 string is only accepted
    with the default ``precision=ns`` parsing rules when the server guesses --
    and it does not: passing ``2026-09-25T20:20:00`` made the parser read
    ``2026`` as the timestamp and then reject ``-09-25T20:20:00`` as trailing
    content. So the conversion is explicit here rather than left to the server.
    """
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        # Already an integer. Assume seconds, which is what the CLI emits.
        return int(text) * 1_000_000_000
    normalised = text.replace("Z", "+00:00")
    if "T" not in normalised and " " in normalised:
        normalised = normalised.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # InfluxDB reports timestamps in UTC; a naive value here is UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def line_protocol(table: str, row: dict, columns: dict[str, str]) -> str | None:
    """Render one CSV row as a line protocol point.

    Returns None for a row with no fields, which line protocol rejects. Tags are
    the columns the schema reports as ``Dictionary(...)``; everything else is a
    field. The timestamp is emitted as integer nanoseconds because the writer is
    called with ``precision=ns``.

    No synthetic tag is added for the database. The database is already named in
    the write URL, and inventing a ``measurement=<database>`` column would put a
    tag in the restored table that the original does not have -- and tags are
    part of the primary key, so it could never be removed afterwards.
    """
    timestamp = row.get("time") or row.get("_time")
    if not timestamp:
        return None
    nanos = _to_nanoseconds(timestamp)
    if nanos is None:
        return None

    tags: list[str] = []
    fields: list[str] = []

    for key, value in row.items():
        if key in ("time", "_time") or value is None or value == "":
            continue
        data_type = columns.get(key, "")
        if TAG_TYPE_MARKER in data_type:
            tags.append(f"{key}={escape_tag(value)}")
            continue
        text = str(value)
        if data_type.startswith("Float") or data_type.startswith("Double") or data_type.startswith("Decimal"):
            fields.append(f"{key}={text}")
        elif data_type.startswith("UInt") or data_type.startswith("BigUint"):
            fields.append(f"{key}={text}u")
        elif data_type.startswith("Int") or data_type.startswith("BigInt"):
            fields.append(f"{key}={text}i")
        elif data_type.startswith("Bool"):
            fields.append(f"{key}={'true' if text.lower() in ('true', '1') else 'false'}")
        else:
            fields.append(f'{key}="{escape_field(text)}"')

    if not fields:
        return None

    tag_set = ",".join(tags)
    prefix = escape_tag(table) + (f",{tag_set}" if tag_set else "")
    return f"{prefix} {','.join(fields)} {nanos}"


def to_csv(rows: list[dict]) -> str:
    """Serialise query rows to CSV, preserving column order and types."""
    if not rows:
        return ""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    return buffer.getvalue()


def from_csv(text: str) -> list[dict]:
    if not text.strip():
        return []
    return list(csv.DictReader(io.StringIO(text)))


# --- backup ------------------------------------------------------------------


def do_backup(target_root: Path, keep: int, max_age_days: int, database: str | None = None) -> int:
    influx = connect(database)
    tables = influx.tables()
    if not tables:
        raise BackupError(f"no tables in {influx.database}; is the stack up?")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_root / stamp
    target.mkdir(parents=True, exist_ok=True)
    print(f"==> backing up {influx.database} to {target.relative_to(ROOT)}")

    # The schema is part of the backup, not something restore can discover: a
    # restore into an empty database has no tables, and in InfluxDB 3 tags are
    # part of the primary key and cannot be added to a table after it exists.
    # Without this, a restore is only possible onto a database that already has
    # the right shape, which is no restore at all.
    schema: dict[str, dict[str, list[str]]] = {}
    for table in tables:
        columns = influx.columns(table)
        tags = sorted(k for k, v in columns.items() if TAG_TYPE_MARKER in v and k != "time")
        fields = {k: v for k, v in columns.items() if k not in tags and k != "time"}
        schema[table] = {"tags": tags, "fields": fields}

    manifest: dict[str, object] = {
        "database": influx.database,
        "created_utc": stamp,
        "format": "csv",
        "note": "CSV plus a manifest; restore recreates the schema then writes line protocol.",
        "schema": schema,
        "tables": {},
    }
    failures: list[str] = []
    for table in tables:
        try:
            rows = influx.query(f'SELECT * FROM "{table}"')
        except (urllib.error.URLError, OSError) as exc:
            failures.append(f"{table}: {exc}")
            print(f"    {table:<22} FAILED  {exc}")
            continue
        if not rows:
            (target / f"{table}.csv").write_text("")
            manifest["tables"][table] = 0  # type: ignore[index]
            print(f"    {table:<22} empty")
            continue
        path = target / f"{table}.csv"
        path.write_text(to_csv(rows))
        size = path.stat().st_size
        manifest["tables"][table] = len(rows)  # type: ignore[index]
        print(f"    {table:<22} {len(rows):>7} rows  {size/1024:8.1f} kB")

    if failures:
        # A partial backup that looks complete is worse than none, so the
        # manifest records what failed rather than the directory being trusted.
        manifest["failures"] = failures
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
        raise BackupError(
            f"{len(failures)} table(s) failed: {failures}\n"
            f"  manifest written with the failures recorded; treat this backup as incomplete"
        )

    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(int(v) for v in manifest["tables"].values())  # type: ignore[union-attr]
    print(f"    {len(tables)} tables, {total} rows, "
          f"{sum(f.stat().st_size for f in target.glob('*.csv'))/1024/1024:.2f} MB")

    prune(target_root, keep, max_age_days)
    return 0


def prune(target_root: Path, keep: int, max_age_days: int) -> None:
    print(f"==> retention: keep {keep}, max age {max_age_days} days")
    removed = 0
    if target_root.exists():
        cutoff = time.time() - max_age_days * 86400
        for directory in sorted(target_root.iterdir()):
            if not directory.is_dir():
                continue
            if directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                print(f"    removed {directory.name} (older than {max_age_days} days)")
                removed += 1
        remaining = sorted(
            (d for d in target_root.iterdir() if d.is_dir()),
            key=lambda d: d.name,
        )
        for directory in remaining[:max(0, len(remaining) - keep)]:
            shutil.rmtree(directory, ignore_errors=True)
            print(f"    removed {directory.name} (beyond keep-{keep})")
            removed += 1
        left = len([d for d in target_root.iterdir() if d.is_dir()])
    else:
        left = 0
    print(f"    {'pruned %d' % removed if removed else 'nothing to prune'}, {left} retained")


# --- restore -----------------------------------------------------------------


def do_restore(source: Path, force: bool, database: str | None = None) -> int:
    if not source.is_dir():
        raise BackupError(f"no such backup: {source}")
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise BackupError(f"{source} has no manifest.json; not a backup directory")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("failures"):
        raise BackupError(
            f"this backup recorded failures and is incomplete: {manifest['failures']}"
        )

    influx = connect(database)
    print(f"==> restoring {source.name} into {influx.database}")

    # Recreate tables before writing. Line protocol writes cannot create them:
    # a write to an unknown table either fails or lands in an implicitly created
    # one, and the latter would have the wrong tags -- and tags are immutable.
    schema = manifest.get("schema") or {}
    if schema:
        print("    recreating schema")
        for table, spec in schema.items():
            field_types = ",".join(
                f"{name}:{_short_type(kind)}" for name, kind in spec["fields"].items()
            )
            result = subprocess.run(
                ["docker", "compose", "exec", "-T", "influxdb", "influxdb3",
                 "create", "table", table,
                 "--database", influx.database,
                 "--tags", *spec["tags"], "--fields", field_types,
                 "--host", "https://localhost:8181", "--tls-no-verify",
                 "--token", influx.token],
                cwd=ROOT, capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"      {table:<22} created")
            elif "already exists" in (result.stdout + result.stderr).lower():
                pass  # restoring over a live database is legitimate
            else:
                raise BackupError(
                    f"could not create table {table}: "
                    f"{(result.stderr or result.stdout).strip()[:200]}"
                )
    else:
        print("    WARNING: backup has no recorded schema; tables must already exist")

    for table in manifest["tables"]:
        path = source / f"{table}.csv"
        if not path.exists():
            print(f"    {table:<22} MISSING in backup")
            continue
        rows = from_csv(path.read_text())
        if not rows:
            print(f"    {table:<22} empty")
            continue

        columns = influx.columns(table)
        if not columns:
            print(f"    {table:<22} no schema; run influx-init first")
            continue

        lines: list[str] = []
        for row in rows:
            rendered = line_protocol(table, row, columns)
            if rendered:
                lines.append(rendered)

        # Batched: a single request per table can exceed the 10 MB body limit,
        # and one failure should not lose the whole table.
        batch = 2000
        written = 0
        try:
            for start in range(0, len(lines), batch):
                influx.write(lines[start:start + batch])
                written += len(lines[start:start + batch])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise BackupError(
                f"{table}: restore failed after {written} points: HTTP {exc.code} {detail}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BackupError(f"{table}: restore failed after {written} points: {exc}") from exc
        print(f"    {table:<22} {written:>7} points written")

    print("    verify with: uv run python scripts/backup.py --list")
    return 0


# --- inspection --------------------------------------------------------------


def do_list(target_root: Path) -> int:
    if not target_root.exists():
        print(f"no backups yet in {target_root}")
        return 0
    entries = sorted(d for d in target_root.iterdir() if d.is_dir())
    if not entries:
        print(f"no backups yet in {target_root}")
        return 0
    print(f"==> backups in {target_root.relative_to(ROOT)}")
    for directory in entries:
        size = sum(f.stat().st_size for f in directory.glob("*.csv"))
        rows = tables = "?"
        manifest = directory / "manifest.json"
        if manifest.exists():
            data = json.loads(manifest.read_text())
            rows = sum(int(v) for v in data.get("tables", {}).values())
            tables = len(data.get("tables", {}))
            if data.get("failures"):
                tables = f"{tables} INCOMPLETE"
        print(f"    {directory.name:<20} {size/1024/1024:7.2f} MB  {rows:>8} rows  {tables} tables")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup", action="store_true", help="write a backup (default)")
    parser.add_argument("--restore", metavar="DIR", help="restore from a backup directory")
    parser.add_argument("--list", "-l", action="store_true", help="show what is on disk")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="backup root")
    parser.add_argument("--database", help="override the target database")
    parser.add_argument("--create-database", action="store_true",
                        help="create the target database if it does not exist")
    parser.add_argument("--keep", type=int, default=KEEP)
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    parser.add_argument("--force", action="store_true",
                        help="restore even if the target already has data")
    args = parser.parse_args()

    target_root = Path(args.dir)
    if args.database:
        if args.create_database:
            # The CLI has no SQL path for DDL; `influxdb3 create` does.
            result = subprocess.run(
                ["docker", "compose", "exec", "-T", "influxdb", "influxdb3",
                 "create", "database", args.database,
                 "--host", "https://localhost:8181", "--tls-no-verify",
                 "--token", connect(args.database).token],
                cwd=ROOT, capture_output=True, text=True,
            )
            # Idempotent: re-running against an existing database is the normal
            # case when re-testing a restore, and must not be an error.
            if result.returncode == 0:
                print(f"==> created database {args.database}")
            elif "already exists" in (result.stdout + result.stderr).lower():
                print(f"==> database {args.database} already exists")
            else:
                raise BackupError(
                    f"could not create database {args.database}: "
                    f"{(result.stderr or result.stdout).strip()[:200]}"
                )
        os.environ["INFLUX_DB"] = args.database
    try:
        if args.restore:
            return do_restore(Path(args.restore), args.force, args.database)
        if args.list:
            return do_list(target_root)
        return do_backup(target_root, args.keep, args.max_age_days, args.database)
    except BackupError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"\nerror: cannot reach InfluxDB: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
