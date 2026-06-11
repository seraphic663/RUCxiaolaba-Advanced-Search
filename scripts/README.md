# Operations Scripts

## Railway scheduler

The Web service starts `jobs.scheduler` when:

```text
CRAWLER_ENABLED=1
```

It updates `/app/data/posts.db` sequentially:

```text
sync-latest  every 8 hours
sync-active  every 8 hours, staggered
backfill  every 24 hours
scan-id-range every 7 days, rescanning the latest 7 calendar days
```

The scheduler requires `/app/data/config.txt`.

## Backup

```bash
python -m jobs.backup --data-dir /app/data
```

Use `--include-db` only when the Volume has enough free space.

Files under `scripts/` are compatibility wrappers. Canonical implementations
live under `jobs/` and `tools/`.
