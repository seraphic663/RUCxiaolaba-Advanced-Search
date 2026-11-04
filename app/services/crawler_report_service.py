"""Read-only crawler reports from persisted observations, runs and quota days."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHINA_TZ = timezone(timedelta(hours=8))
LEGACY_HISTORY = Path(__file__).resolve().parents[1] / "static" / "crawler_legacy.json"


def parse_time(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (at.replace(tzinfo=CHINA_TZ) if at.tzinfo is None else at).astimezone(CHINA_TZ)


class CrawlerReportService:
    def __init__(self, posts_db: str | Path):
        self.posts_db = Path(posts_db)
        self.metrics_path = Path(os.environ.get("CRAWLER_METRICS_PATH") or self.posts_db.parent / "crawler_metrics" / "crawler_history.csv")
        self._lock = threading.Lock()
        self._stamp = None
        self._points: list[dict] = []

    def _observations(self) -> list[dict]:
        # Cache only the small ID time series, not the CSV's other metrics/raw payloads.
        stamp = (self.metrics_path.stat().st_mtime_ns, self.metrics_path.stat().st_size) if self.metrics_path.exists() else None
        with self._lock:
            if stamp == self._stamp and self._points:
                return self._points
            points = []
            if LEGACY_HISTORY.exists():
                points.extend(json.loads(LEGACY_HISTORY.read_text(encoding="utf-8")))
            if stamp is not None:
                with self.metrics_path.open(encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        if row.get("metric_name") != "id_pending" or None in row.values():
                            continue
                        try:
                            at, value = parse_time(row["observed_at"]), int(row["value"])
                        except (ValueError, TypeError):
                            continue
                        points.append({"record_id": row["record_id"], "at": at.isoformat(),
                                       "value": value, "method": row["method"],
                                       "quality": row["quality"], "source": row["source_id"],
                                       "run_id": row["source_run_id"]})
            unique = {p["record_id"]: p for p in points}
            self._points = sorted(unique.values(), key=lambda p: p["at"])
            self._stamp = stamp
            return self._points

    def _quota_days(self, start: datetime, end: datetime) -> list[dict]:
        days = {}
        path = self.posts_db.with_name(".crawler_quota_history.jsonl")
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("reason") == "day_rollover":
                    days[row["date"]] = row
        current = self.posts_db.with_name(".crawler_quota.json")
        if current.exists():
            row = json.loads(current.read_text(encoding="utf-8"))
            days[row["date"]] = row
        result = []
        today = datetime.now(CHINA_TZ).date().isoformat()
        for day, row in sorted(days.items()):
            if not start.date().isoformat() <= day <= end.date().isoformat():
                continue
            lanes = row.get("cookie_lanes") or {"shared": row}
            for lane, counters in lanes.items():
                calls = {key: int(counters.get(key, 0) or 0) for key in ("detail_calls", "new_list_calls", "active_list_calls", "rate_limited")}
                result.append({"date": day, "lane": lane, **calls,
                               "total": sum(calls[k] for k in ("detail_calls", "new_list_calls", "active_list_calls")),
                               "partial": day == today})
        return result

    def _daily_runs(self, start: datetime, end: datetime) -> list[dict]:
        if not self.posts_db.exists():
            return []
        with closing(sqlite3.connect(f"file:{self.posts_db.resolve().as_posix()}?mode=ro", uri=True, timeout=3)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='crawler_run_history'").fetchone():
                return []
            rows = conn.execute("SELECT command,finished_at,source_calls,stats_json FROM crawler_run_history WHERE finished_at>=? AND finished_at<=? ORDER BY finished_at", (start.isoformat(), end.isoformat())).fetchall()
        groups = defaultdict(lambda: defaultdict(int))
        for command, finished, calls, raw in rows:
            stats = json.loads(raw)
            if command not in {"discover-latest", "discover-active", "trickle-fill"}:
                continue
            if command == "trickle-fill" and (stats.get("task_type") == "history_detail" or stats.get("scheduler_job") == "trickle_fill_history"):
                continue
            day = parse_time(finished).date().isoformat()
            g = groups[day]
            g["runs"] += 1
            if command == "trickle-fill" and not stats.get("task_type") and stats.get("scheduler_job") != "trickle_fill":
                g["unrouted_detail_calls"] += calls
                g["incomplete_runs"] += 1
                continue
            g["incomplete_runs"] += int(bool(stats.get("scheduler_failed")) or "queue_delta_total" not in stats)
            if command in {"discover-latest", "discover-active"}:
                prefix = "list1" if command == "discover-latest" else "list2"
                g[prefix + "_runs"] += 1
                g[prefix + "_calls"] += calls
                for field in ("queue_inserted", "queue_reopened", "queue_updated", "ledger_new_ids"):
                    g[prefix + "_" + field] += int(stats.get(field, 0) or 0)
                g["inflow"] += int(stats.get("queue_delta_total", 0) or 0)
            elif command == "trickle-fill":
                g["detail_calls"] += calls
                g["removed_pending"] -= int(stats.get("queue_delta_total", 0) or 0)
                for field in ("completed_details", "refreshed_details", "misses", "partial_saved", "partial_completed", "retry_scheduled", "deferred_observations", "new_comment_rows", "partial_comment_rows_added", "claim_conflicts"):
                    g[field] += int(stats.get(field, 0) or 0)
                # Partial merges can also increment completed_details: keep outcomes exclusive.
                g["normal_success"] += int(stats.get("completed_details", 0)) + int(stats.get("refreshed_details", 0)) - int(stats.get("partial_completed", 0))
        return [{"date": day, **dict(g), "net": g["inflow"] - g["removed_pending"]} for day, g in sorted(groups.items())]

    def report(self, date_from: str = "", date_to: str = "") -> dict:
        now = datetime.now(CHINA_TZ)
        start = parse_time(date_from) if date_from else now - timedelta(hours=72)
        end = parse_time(date_to) if date_to else now
        # Date/minute controls include the whole last day/minute selected.
        if len(date_to) == 10:
            end += timedelta(days=1, microseconds=-1)
        elif len(date_to) == 16:
            end += timedelta(minutes=1, microseconds=-1)
        if start > end:
            raise ValueError("开始时间不能晚于结束时间")
        all_points = self._observations()
        points = [p for p in all_points if start.isoformat() <= p["at"] <= end.isoformat()]
        return {"ok": True, "generated_at": now.isoformat(), "from": start.isoformat(), "to": end.isoformat(),
                "points": points, "total_observations": len(all_points),
                "earliest": all_points[0]["at"] if all_points else None,
                "latest": all_points[-1]["at"] if all_points else None,
                "daily": self._daily_runs(start, end), "quota_days": self._quota_days(start, end),
                "definition": "crawler_queue.task_type=id_followup AND status=pending"}
