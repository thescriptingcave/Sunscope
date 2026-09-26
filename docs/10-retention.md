# 10. Data retention: what InfluxDB 3 Core can and cannot do

Every claim here was verified against the running stack (`influxdb:3.11-core`,
file object store), not inferred from documentation. Several of the obvious
answers are wrong in ways that are actively dangerous, so this document exists
mostly to record the wrong answers.

## Summary

| Question | Answer |
|---|---|
| Can I `DELETE` old rows? | **No.** `DML not supported: Delete` |
| Is there a retention policy or TTL? | **No.** No such setting, env var, or system view exists |
| Can I drop old tables? | **Technically yes, and it will silently corrupt your data** — see §2 |
| Does dropping a database free disk? | **No.** Verified over 3.5 minutes and repeated cycles |
| How fast does it actually grow? | **1.29 GB/year** |
| How do I reclaim space? | **Destroy and rebuild the volume** — see §4 |

The short version: **retention is not needed at this scale and cannot be
meaningfully implemented on this engine.** The only mechanism that reclaims disk
is recreating the volume, and `scripts/backup.py` already provides everything
that requires.

## 1. There is no row-level delete

```sql
DELETE FROM inverter_telemetry WHERE time < '2026-09-24'
-- Error during planning: DML not supported: Delete
```

The `influxdb3 delete` CLI subcommand only removes *resources* — databases,
tables, caches, tokens. There is no row-level path at all, and no retention
policy, TTL, or `system.retention_policies` view to configure.

## 2. Dropping a table silently orphans the data — do not do this

This is the trap. A table that is dropped is **tombstoned, not removed**, and the
next write to that measurement name creates a *different* table:

```
$ influxdb3 create table t1 --database probe --tags site --fields v:float64
$ # ... write rows, drop the table, write again ...
$ influxdb3 query 'SELECT table_name FROM information_schema.tables ...'
t1                      <- the original name, still listed
t1-20260926T181521      <- where the data actually went
$ influxdb3 query 'SELECT COUNT(*) FROM t1'
(no rows)               <- the original name now resolves to nothing
```

Three consequences, all bad:

- **Queries by the original name return nothing** while the data still occupies
  disk. A retention job that dropped "expired" tables would make the database
  look pruned while using the same space and hiding the data.
- **The drop cannot be undone.** Deleting the replacement produces yet another
  suffixed table, and the previous generation becomes a permanent tombstone:

  ```
  Delete command failed: 409 Conflict:
  attempted to modify resource that was already deleted
  ```

- **There is no clean-up path.** A table created by mistake stays in
  `information_schema.tables` forever. The only way to remove it is to destroy
  the volume.

This is also why `scripts/backup.py` grew a `--only` flag: a restore cannot be
tidied up afterwards, so anything unwanted must be excluded *before* it is
written in.

## 3. Dropping a database does not free disk

Wrote ~40,000 rows (~4 MB), dropped the database, and watched:

```
  written:        disk=29 MB
  after 45s:      disk=29 MB   (waiting for compaction/GC)
  dropped:        disk=29 MB
  +30s:           disk=29 MB
  +60s:           disk=29 MB
  +120s:          disk=29 MB
```

Three separate write-then-drop cycles grew the volume 16 → 20 → 25 → 29 MB and it
never came back down. There is no GC configuration in `influxdb3 serve`, and no
delayed reclamation.

**Disk usage is monotonic for the life of the volume.** The only thing that
frees space is deleting the volume.

## 4. The reclaim procedure (verified)

This is the only mechanism that works. It is a maintenance operation, not
something to run on a schedule.

```bash
# 1. Back up only what you want to keep. --since is the retention primitive:
#    back up the window worth keeping, then rebuild around it.
uv run --project api python scripts/backup.py --backup \
  --since 2026-09-25T00:00:00Z \
  --only events,inverter_telemetry,site_rollup,string_telemetry,weather_station

# 2. Stop the simulator and the stack.
./scripts/bootstrap.sh sim:stop
./scripts/bootstrap.sh down

# 3. Destroy the volume — the only step that reclaims anything.
docker volume rm sunscope_influx-data

# 4. Bring it back; influx-init recreates the schema.
./scripts/bootstrap.sh up

# 5. Restore the window.
uv run --project api python scripts/backup.py --restore backups/<stamp>
```

Verified end to end: **29 MB → 2 MB**, the four tombstoned probe tables were
gone, and the restored data was **bit-exact** — over a window the simulator's
24-hour backfill does not reach, 1080 rows with `SUM(ac_power_w)` matching the
source CSV to the last decimal place.

Two things to know:

- **Stop the simulator first.** It publishes every ~30 s, and a write landing
  between your backup and the volume swap is lost. `./scripts/bootstrap.sh`
  owns process lifecycle, which is why the procedure goes through it.
- **A restore does not repopulate the Last Value Cache.** `/api/now` reads the
  LVC, not history, so it stays empty until the simulator publishes again. It
  self-heals within one publish interval, but a dashboard opened immediately
  after a restore will briefly show nothing.

## 5. How much space this actually needs

Measured over 27.4 h of simulated run, 11,545 rows, 4.13 MB of Parquet
(358 bytes/row):

| Table | Rows/year | MB/year |
|---|---:|---:|
| `inverter_telemetry` | 773,827 | 462.2 |
| `string_telemetry` | 2,321,480 | 278.9 |
| `weather_station` | 193,457 | 263.2 |
| `site_rollup` | 193,457 | 219.1 |
| `events` | 215,557 | 98.9 |
| **Total** | **3,697,778** | **1,322 MB (1.29 GB)** |

At this rate a 500 GB disk lasts roughly **387 years**. Retention is a
theoretical concern for this workload, and `./scripts/bootstrap.sh disk` reports
the live figure plus the projection so the assumption stays honest rather than
becoming folklore.

`string_telemetry` dominates row count (12 strings × 4 inverters per tick) and is
the first thing to reconsider if that ever changes — 30-second resolution on
string current is far more detail than the dashboards use.

## 6. If retention ever becomes real

The correct fix is a different engine, not a cleverer script:

- **InfluxDB 3 Enterprise / Cloud** has native retention policies and partition
  expiry, which is the feature being approximated here.
- Failing that, keep the simulator short and rebuild the volume periodically
  using §4.

Do not write a drop-based pruning script against InfluxDB 3 Core. §2 explains
what it does to your data, and it does it quietly.
