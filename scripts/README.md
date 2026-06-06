# Operations Scripts

## Railway scheduler

The Web service starts `railway_scheduler.py` when:

```text
CRAWLER_ENABLED=1
```

It updates `/app/data/posts.db` sequentially:

```text
new       every 30 minutes
refresh   every 60 minutes
backfill  every 24 hours
```

The scheduler requires `/app/data/config.txt`.

## Backup

```bash
python scripts/backup_runtime.py --data-dir /app/data
```

Use `--include-db` only when the Volume has enough free space.
