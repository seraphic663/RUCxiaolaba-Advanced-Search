#!/usr/bin/env python3
"""Maintain the standalone crawler metrics ledger and rebuild reports.

The ledger is a CSV file, not a runtime SQLite table. ``import-legacy`` keeps
all machine-readable old report payloads in the ledger and records every old
artifact in the source manifest. PNG-only artifacts are retained as
``visual_only`` until their values are digitized.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from crawler.lock import database_write_lock
from storage.crawler_metrics import (
    HISTORY_COLUMNS,
    METRICS_SCHEMA_VERSION,
    SOURCE_COLUMNS,
    append_rows,
    current_crawler_pool_metrics,
    read_rows,
    record_crawler_pool_snapshot,
    sources_path_for_metrics,
)


CHINA_TZ = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_METRICS_PATH = REPO_ROOT / "metrics" / "crawler_history.csv"
DEFAULT_SOURCES_PATH = REPO_ROOT / "metrics" / "crawler_sources.csv"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"


def _now_iso() -> str:
    return datetime.now(CHINA_TZ).isoformat()


@contextmanager
def _read_only_connection(path: Path):
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma busy_timeout=60000")
    try:
        yield connection
    finally:
        connection.close()


def capture(
    db_path: Path,
    metrics_path: Path,
    *,
    sample_kind: str = "manual",
) -> dict[str, Any]:
    """Read one exact SQLite state and append it to the standalone ledger."""

    with database_write_lock(db_path, 60):
        with _read_only_connection(db_path) as conn:
            return record_crawler_pool_snapshot(
                conn,
                metrics_path=metrics_path,
                source_command="metrics-capture",
                sample_kind=sample_kind,
                method="exact",
                stats={"capture_tool": "tools/operations/crawler_metrics.py"},
            )


def _as_number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _json_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _report_meta(payload: dict[str, Any], filename: str) -> dict[str, str]:
    report = payload.get("report") if isinstance(payload.get("report"), dict) else payload
    return {
        "title": _first(report, "title", "name") or filename,
        "window_start": _first(report, "window_start", "asof"),
        "window_end": _first(report, "window_end", "window_end_exclusive"),
        "queried_at": _first(
            report,
            "generated_at",
            "captured_at",
            "asof",
        ),
    }


def _quality_for_item(item: dict[str, Any], filename: str) -> tuple[str, str]:
    source = str(item.get("source") or item.get("source_id") or "")
    if source in {"current", "live"} or item.get("distance") == 0:
        return "exact", "legacy_exact_point"
    if item.get("anchor_minutes") == 0:
        return "exact", "legacy_exact_anchor"
    if any(key in item for key in ("nearest_anchor", "distance", "anchor_minutes")):
        return "estimated", "backcast"
    if "source" in filename and filename.endswith(".json"):
        return "provenance", "source_receipt"
    return "structured_legacy", "legacy_import"


def _observed_at(item: dict[str, Any], fallback: str) -> str:
    return _first(item, "target_at", "sampled_at", "time", "iso", "at") or fallback


def _metric_group(filename: str, path: str = "") -> str:
    text = f"{filename} {path}".lower()
    if "quota" in text or "allocation" in text:
        return "quota"
    if "efficiency" in text or "bench" in text:
        return "efficiency"
    if "dedup" in text or "duplicate" in text:
        return "dedup"
    if "pool" in text or "pending" in text:
        return "pool"
    return "legacy"


def _task_type(metric_name: str) -> str:
    name = metric_name.lower()
    if "history" in name or "old_detail" in name:
        return "history_detail"
    if "id_pool" in name or "id_pending" in name or "new_detail" in name:
        return "id_followup"
    return ""


def _flatten_numeric(value: Any, prefix: str = "") -> Iterable[tuple[str, int | float]]:
    scalar = _as_number(value)
    if scalar is not None:
        yield prefix, scalar
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_numeric(child, child_prefix)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from _flatten_numeric(child, child_prefix)


def _point_rows(
    payload: dict[str, Any],
    *,
    filename: str,
    source_id: str,
    meta: dict[str, str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    arrays = ("points", "rows", "runs", "daily", "segments", "scenarios")
    for array_name in arrays:
        values = payload.get(array_name)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                continue
            observed_at = _observed_at(item, meta["queried_at"])
            quality, method = _quality_for_item(item, filename)
            for metric_name, number in _flatten_numeric(item):
                if not metric_name:
                    continue
                rows.append(
                    {
                        "record_id": f"legacy:{source_id}:{array_name}:{index}:{metric_name}",
                        "observed_at": observed_at,
                        "window_start": meta["window_start"],
                        "window_end": meta["window_end"],
                        "metric_group": _metric_group(filename, array_name),
                        "metric_name": f"{array_name}.{metric_name}",
                        "task_type": _task_type(metric_name),
                        "status": "pending" if "pending" in metric_name else "",
                        "value": str(number),
                        "unit": "count",
                        "method": method,
                        "quality": quality,
                        "source_id": source_id,
                        "source_run_id": str(item.get("run_id") or ""),
                        "notes": f"legacy array={array_name};index={index}",
                        "extra_json": json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
                    }
                )
    return rows


def _generic_numeric_rows(
    payload: dict[str, Any],
    *,
    filename: str,
    source_id: str,
    meta: dict[str, str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    excluded = {"points", "rows", "runs", "daily", "segments", "scenarios"}
    for key, value in payload.items():
        if key in excluded:
            continue
        for metric_name, number in _flatten_numeric(value, key):
            quality, method = _quality_for_item(payload, filename)
            rows.append(
                {
                    "record_id": f"legacy:{source_id}:{metric_name}",
                    "observed_at": meta["queried_at"],
                    "window_start": meta["window_start"],
                    "window_end": meta["window_end"],
                    "metric_group": _metric_group(filename, metric_name),
                    "metric_name": metric_name,
                    "task_type": _task_type(metric_name),
                    "status": "pending" if "pending" in metric_name else "",
                    "value": str(number),
                    "unit": "count",
                    "method": method,
                    "quality": quality,
                    "source_id": source_id,
                    "source_run_id": "",
                    "notes": "legacy numeric field",
                    "extra_json": "",
                }
            )
    return rows


def import_legacy(
    reports_dir: Path,
    metrics_path: Path,
    sources_path: Path,
) -> dict[str, int]:
    """Import old report payloads without treating rendered files as facts."""

    existing_sources = {row.get("source_id", "") for row in read_rows(sources_path)}
    history_rows: list[dict[str, str]] = []
    source_rows: list[dict[str, str]] = []
    imported_sources = 0
    skipped_sources = 0

    for artifact in sorted(reports_dir.rglob("*")):
        if not artifact.is_file():
            continue
        relative = artifact.relative_to(reports_dir).as_posix()
        if relative == "README.md" or relative.startswith("generated/"):
            continue
        source_id = f"legacy:{relative}"
        if source_id in existing_sources:
            skipped_sources += 1
            continue
        imported_sources += 1
        suffix = artifact.suffix.lower().lstrip(".") or "file"
        report_title = artifact.name
        window_start = ""
        window_end = ""
        queried_at = ""
        method = "legacy_artifact"
        quality = "visual_only" if suffix in {"png", "svg", "html"} else "unknown"
        imported_count = 0
        notes = "Rendered artifact; no numeric payload imported."
        raw_content = ""
        if suffix in {"html", "svg"}:
            try:
                raw_content = artifact.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                notes += f" Raw text read failed: {exc}."

        paired_json = artifact.with_suffix(".json")
        if artifact.stem.endswith("_sources"):
            paired_json = artifact.with_name(f"{artifact.stem[:-8]}.json")
        if suffix in {"png", "svg", "html"} and paired_json.exists():
            quality = "rendered_duplicate"
            method = "rendered_duplicate"
            notes = f"Rendered artifact; numeric payload is imported from {paired_json.name}."

        if suffix == "json":
            try:
                payload = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                payload = {"parse_error": str(exc)}
            if isinstance(payload, dict):
                meta = _report_meta(payload, artifact.name)
                report_title = meta["title"]
                window_start = meta["window_start"]
                window_end = meta["window_end"]
                queried_at = meta["queried_at"]
                numeric_rows = _point_rows(
                    payload,
                    filename=artifact.name,
                    source_id=source_id,
                    meta=meta,
                )
                numeric_rows.extend(
                    _generic_numeric_rows(
                        payload,
                        filename=artifact.name,
                        source_id=source_id,
                        meta=meta,
                    )
                )
                raw_row = {
                    "record_id": f"legacy:{source_id}:raw_payload",
                    "observed_at": queried_at,
                    "window_start": window_start,
                    "window_end": window_end,
                    "metric_group": "legacy_payload",
                    "metric_name": "raw_json",
                    "task_type": "",
                    "status": "",
                    "value": "",
                    "unit": "json",
                    "method": "legacy_payload",
                    "quality": "raw",
                    "source_id": source_id,
                    "source_run_id": "",
                    "notes": "Full machine-readable legacy payload retained in extra_json.",
                    "extra_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                }
                history_rows.extend(numeric_rows)
                history_rows.append(raw_row)
                imported_count = len(numeric_rows) + 1
                quality = "structured_json"
                method = "legacy_import"
                notes = "Structured JSON imported; duplicate rendered files are not re-counted."
            else:
                notes = "JSON root was not an object; retained only in source manifest."

        source_rows.append(
            {
                "source_id": source_id,
                "source_file": relative,
                "source_kind": suffix,
                "report_title": report_title,
                "window_start": window_start,
                "window_end": window_end,
                "queried_at": queried_at,
                "method": method,
                "quality": quality,
                "imported_records": str(imported_count),
                "notes": notes,
                "raw_content": raw_content,
            }
        )

    append_rows(metrics_path, history_rows, columns=HISTORY_COLUMNS)
    append_rows(sources_path, source_rows, columns=SOURCE_COLUMNS)
    return {
        "imported_sources": imported_sources,
        "skipped_sources": skipped_sources,
        "history_rows": len(history_rows),
    }


def _history_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    exact = [row for row in rows if row.get("quality") == "exact"]
    groups: dict[str, int] = {}
    for row in rows:
        group = row.get("metric_group", "")
        groups[group] = groups.get(group, 0) + 1
    return {
        "schemaVersion": METRICS_SCHEMA_VERSION,
        "generatedAt": _now_iso(),
        "rowCount": len(rows),
        "exactRowCount": len(exact),
        "groups": groups,
    }


def render(metrics_path: Path, output_dir: Path, *, limit: int = 0) -> dict[str, Any]:
    rows = read_rows(metrics_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    if limit > 0:
        rows_for_view = rows[-limit:]
    else:
        rows_for_view = rows
    document = {
        **_history_summary(rows),
        "source": str(metrics_path),
        "rows": rows_for_view,
    }
    (output_dir / "latest.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "history.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_for_view)

    visible = [row for row in reversed(rows_for_view) if row.get("metric_name") not in {"raw_json", "raw_stats"}]
    visible = visible[:300]
    table = []
    for row in visible:
        table.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(row.get(field, '')))}</td>"
                for field in (
                    "observed_at",
                    "metric_group",
                    "metric_name",
                    "task_type",
                    "status",
                    "value",
                    "method",
                    "quality",
                    "source_id",
                )
            )
            + "</tr>"
        )
    summary = _history_summary(rows)
    (output_dir / "latest.html").write_text(
        "<!doctype html>\n<meta charset='utf-8'>\n"
        "<title>Crawler metrics ledger</title>\n"
        "<style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:2rem;color:#24313d}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd5dd;padding:.35rem}"
        "th{background:#eef3f6;position:sticky;top:0}</style>\n"
        "<h1>Crawler metrics ledger</h1>\n"
        f"<p>Rows {summary['rowCount']} · exact rows {summary['exactRowCount']} · generated {html.escape(summary['generatedAt'])}</p>\n"
        "<table><thead><tr>"
        "<th>observed_at</th><th>metric_group</th><th>metric_name</th>"
        "<th>task_type</th><th>status</th><th>value</th><th>method</th>"
        "<th>quality</th><th>source_id</th>"
        "</tr></thead><tbody>"
        + "".join(table)
        + "</tbody></table>\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(
        "# Crawler metrics\n\n"
        f"- Source: `{metrics_path}`\n- Rows: `{summary['rowCount']}`\n"
        f"- Exact rows: `{summary['exactRowCount']}`\n- Generated: `{summary['generatedAt']}`\n",
        encoding="utf-8",
    )
    return {"output_dir": str(output_dir), **summary}


def validate(db_path: Path, metrics_path: Path) -> dict[str, Any]:
    rows = read_rows(metrics_path)
    exact_rows = [row for row in rows if row.get("method") == "exact"]
    if not exact_rows:
        return {"ok": False, "reason": "no exact ledger rows"}
    latest_at = max(row.get("observed_at", "") for row in exact_rows)
    latest = [row for row in exact_rows if row.get("observed_at") == latest_at]
    values = {
        row.get("metric_name", ""): int(float(row.get("value") or 0))
        for row in latest
        if row.get("metric_name") in {
            "id_pending",
            "history_pending",
            "queue_pending_total",
            "id_ledger_total",
            "list2_observation_total",
        }
    }
    with database_write_lock(db_path, 60):
        with _read_only_connection(db_path) as conn:
            current = current_crawler_pool_metrics(conn)
    expected = {
        "id_pending": int(current["id_pending"]),
        "history_pending": int(current["history_pending"]),
        "queue_pending_total": int(current["queue_pending_total"]),
        "id_ledger_total": int(current["id_ledger_total"]),
        "list2_observation_total": int(current["list2_observation_total"]),
    }
    return {
        "ok": all(values.get(key) == value for key, value in expected.items()),
        "sampled_at": latest_at,
        "ledger": values,
        "current": expected,
        "missing": [key for key in expected if key not in values],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone crawler metrics ledger")
    parser.add_argument("--db-path", type=Path, default=REPO_ROOT / "data" / "posts.db")
    parser.add_argument("--metrics-path", type=Path, default=DEFAULT_METRICS_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_runtime_options(child: argparse.ArgumentParser) -> None:
        child.add_argument("--db-path", type=Path, default=argparse.SUPPRESS)
        child.add_argument("--metrics-path", type=Path, default=argparse.SUPPRESS)

    capture_parser = sub.add_parser("capture", help="append one exact SQLite snapshot")
    add_runtime_options(capture_parser)
    capture_parser.add_argument("--sample-kind", choices=("manual", "heartbeat"), default="manual")

    render_parser = sub.add_parser("render", help="rebuild disposable report files")
    add_runtime_options(render_parser)
    render_parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORTS_DIR / "generated")
    render_parser.add_argument("--limit", type=int, default=0)

    import_parser = sub.add_parser("import-legacy", help="import old report artifacts")
    add_runtime_options(import_parser)
    import_parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    import_parser.add_argument("--sources-path", type=Path, default=DEFAULT_SOURCES_PATH)

    validate_parser = sub.add_parser("validate", help="compare latest exact ledger point with SQLite")
    add_runtime_options(validate_parser)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture":
        print(
            json.dumps(
                capture(args.db_path, args.metrics_path, sample_kind=args.sample_kind),
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "render":
        print(
            json.dumps(
                render(args.metrics_path, args.output_dir, limit=args.limit),
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "import-legacy":
        print(
            json.dumps(
                import_legacy(args.reports_dir, args.metrics_path, args.sources_path),
                ensure_ascii=False,
            )
        )
        return 0
    result = validate(args.db_path, args.metrics_path)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
