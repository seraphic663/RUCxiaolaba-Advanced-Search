# Legacy crawler archive: 2026-06-02 pre DB-first

This snapshot keeps the old CSV-first crawler scripts and local logs available
while the project moves toward a DB-first crawler pipeline.

Original files are intentionally left in place. This archive is a reference copy,
not a deletion/migration marker.

## Archived scripts

- spider.py
- spider_danger.py
- crawl_detail.py
- scan_full.py
- update_full.py
- mitm_filter.py
- analyze_ids.py

## Archived logs / captures

- crawl_log.txt / crawl_err.txt
- detail_log.txt / detail_err.txt
- scan_log.txt / scan_err.txt
- analyze_log.txt / analyze_err.txt
- flask.log
- captured_requests.jsonl

## Large data not copied

Large runtime data is documented but not duplicated here:

- data/posts_final.csv
- data/posts_final.csv.gz
- data/posts.db
- data/posts.slim.db
- data/railway_sync/*

Reason: duplicating multi-GB files would make local and deployment cleanup harder.
Keep them under data/ until the DB-first route is proven.

## DB-first progress after archive

After this archive was created, `crawler_db.py` was added as the new migration-safe DB-first crawler entrypoint. The archived scripts remain reference copies; the live root files also remain in place.

Verified short route:

```text
live detail API -> crawler_db.py detail-fill -> temp SQLite DB -> server.py SQLite backend
```

No legacy crawler file has been deleted.

## 2026-06-02 incremental verification

`crawler_db.py incremental` was tested against `data/posts.slim.db` with `--max-details 2`.

Observed result:

```text
posts: 543,601 -> 543,603
latest: #5018419 / 2026-06-02 11:30:00
```

The legacy CSV crawlers remain unmodified in the project root and copied in this archive.
