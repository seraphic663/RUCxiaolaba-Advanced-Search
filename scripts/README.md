# Operations Scripts

## Railway scheduler

The Web service starts `railway_scheduler.py` when:

```text
CRAWLER_ENABLED=1
```

It updates `/app/data/posts.db` sequentially:

```text
new       every 4 hours
refresh   every 4 hours, offset by 2 hours
backfill  every 24 hours
phase1    every 7 days, rescanning the latest 8 calendar days
```

The scheduler requires `/app/data/config.txt`.

## Backup

```bash
python scripts/backup_runtime.py --data-dir /app/data
```

Use `--include-db` only when the Volume has enough free space.
