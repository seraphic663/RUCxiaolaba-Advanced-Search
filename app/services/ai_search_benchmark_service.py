"""Persistent admin-only benchmark jobs sharing the production query pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.ai_search import AiSearchError, AiSearchSettings
from app.services.ai_search_benchmark_spec import evaluate, validate_manifest
from app.services.ai_search_costs import estimate, usage_cost
from app.services.ai_search_service import MAX_CONCURRENT_QUERIES

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "tools/benchmarks/ai_search_cases.json"
ARCHIVE_PATH = ROOT / "reports/generated/ai-search"


def _write_json(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AiSearchBenchmarkService:
    def __init__(self, engine, runs_dir: Path, *, manifest_path=CASES_PATH, archive_dir=ARCHIVE_PATH):
        self.engine = engine
        self.runs_dir, self.archive_dir = Path(runs_dir), Path(archive_dir)
        self.manifest_path = Path(manifest_path)
        self._lock = threading.RLock()
        self._active = None
        self._thread = None

    def _manifest(self):
        try:
            raw = self.manifest_path.read_bytes()
            manifest = json.loads(raw)
            return manifest, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError) as exc:
            raise AiSearchError("benchmark_unavailable", "Bench 清单无法读取", 503) from exc

    def _paths(self):
        paths = {}
        for prefix, root in (("run", self.runs_dir), ("archive", self.archive_dir)):
            if root.exists():
                for path in root.glob("*/results.json"):
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", path.parent.name) and not path.is_symlink() and not path.parent.is_symlink():
                        paths[prefix + ":" + path.parent.name] = path
        return paths

    def _read(self, run_id, paths=None):
        path = (self._paths() if paths is None else paths).get(run_id)
        if path is None:
            raise AiSearchError("benchmark_not_found", "找不到该轮 Bench", 404)
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("report too large")
            report = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or not isinstance(report.get("runs"), list):
                raise ValueError("invalid report")
            if report.get("status") in {"running", "queued"} and run_id != self._active:
                report["status"] = "interrupted"
                report["running_case_ids"] = []
            return report
        except (OSError, ValueError) as exc:
            raise AiSearchError("benchmark_unreadable", "该轮 Bench 记录无法读取", 503) from exc

    @staticmethod
    def _summary(run_id, report, manifest_hash):
        rows = report["runs"]
        usage, calls, reported, unmetered = {}, 0, 0, 0
        for row in rows:
            stats = row.get("result", {}).get("stats", {})
            unmetered += bool(stats.get("usage_unknown") or (row.get("result", {}).get("answer_status") == "error" and not stats))
            calls += stats.get("ai_calls", 0)
            reported += stats.get("usage_reported_calls", stats.get("ai_calls", 0) if stats.get("usage") else 0)
            for key, value in stats.get("usage", {}).items():
                if type(value) is int and value >= 0:
                    usage[key] = usage.get(key, 0) + value
        pricing_settings = AiSearchSettings(model=report.get("model", ""), base_url=report.get("base_url", "https://api.deepseek.com"))
        total = report.get("total", len(rows))
        cost = usage_cost(pricing_settings, usage, calls=calls, reported=reported)
        if unmetered:
            cost.update(usage_complete=False, unmetered_queries=unmetered)
        return {"id": run_id, "label": report.get("label", run_id),
                "created_at_beijing": report.get("created_at_beijing", ""), "model": report.get("model", ""),
                "status": report.get("status", "complete"), "total": total, "completed": len(rows),
                "passed": sum(row.get("check", {}).get("passed") is True for row in rows),
                "failed": sum(row.get("check", {}).get("passed") is not True for row in rows),
                "concurrency": report.get("concurrency", 1), "source": report.get("source", {}),
                "manifest_matches": report.get("manifest_sha256") == manifest_hash,
                "code_sha256": report.get("code_sha256", ""),
                "wall_elapsed_ms": report.get("wall_elapsed_ms"),
                "query_elapsed_ms": sum(row.get("result", {}).get("stats", {}).get("elapsed_ms", 0) for row in rows),
                "usage": usage, "ai_calls": calls,
                "cost": cost,
                "estimate": report.get("estimate"), "running_case_ids": report.get("running_case_ids", []),
                "error_code": report.get("error_code", "")}

    def _reports(self, manifest_hash):
        reports = []
        paths = self._paths()
        for run_id in sorted(paths, key=lambda key: paths[key].parent.name, reverse=True)[:100]:
            try:
                report = self._read(run_id, paths)
                reports.append((self._summary(run_id, report, manifest_hash), report))
            except AiSearchError:
                continue
        return reports

    def _estimate(self, cases, concurrency, settings, manifest_hash):
        baseline = next((report for summary, report in self._reports(manifest_hash)
                         if summary["status"] == "complete" and summary["manifest_matches"] and summary["model"] == settings.model
                         and len({row["case"]["id"] for row in report["runs"]}) >= len(cases)), None)
        by_id = {row["case"]["id"]: row["result"] for row in baseline["runs"]} if baseline else {}
        return estimate(settings, [by_id.get(case["id"], {}) for case in cases], concurrency=concurrency,
                        basis="previous_run" if baseline else "fallback",
                        baseline_concurrency=baseline.get("concurrency", 1) if baseline else 1)

    def catalog(self):
        with self._lock:
            manifest, manifest_hash = self._manifest()
            settings = self.engine.config.load()
            reports = self._reports(manifest_hash)
            return {"manifest": manifest, "manifest_sha256": manifest_hash,
                    "history": [summary for summary, _ in reports], "active_id": self._active,
                    "estimate": self._estimate(manifest["cases"], 18, settings, manifest_hash),
                    "configured": bool(settings.api_key), "local_retrieval": not settings.search_url,
                    "max_concurrency": MAX_CONCURRENT_QUERIES, "model": settings.model}

    def detail(self, run_id):
        with self._lock:
            _, manifest_hash = self._manifest()
            report = self._read(run_id)
            settings = AiSearchSettings(model=report.get("model", ""), base_url=report.get("base_url", "https://api.deepseek.com"))
            for row in report["runs"]:
                result = row.get("result", {})
                stats = result.setdefault("stats", {})
                if result.get("answer_status") == "error" and "ai_calls" not in stats:
                    stats["usage_unknown"] = True
                if stats and "cost" not in stats:
                    calls = stats.get("ai_calls", 0)
                    stats["cost"] = usage_cost(settings, stats.get("usage", {}), calls=calls,
                                               reported=stats.get("usage_reported_calls", calls if stats.get("usage") else 0))
                    if stats.get("usage_unknown"):
                        stats["cost"]["usage_complete"] = False
            # Only the stored, sanitized query outputs are exposed; no config or filesystem paths.
            return {"summary": self._summary(run_id, report, manifest_hash), "runs": report["runs"],
                    "case_ids": report.get("case_ids", [row["case"]["id"] for row in report["runs"]])}

    @staticmethod
    def _options(payload, cases):
        selected = payload.get("case_ids", [])
        if not isinstance(selected, list) or not all(isinstance(value, str) for value in selected):
            raise AiSearchError("invalid_cases", "case_ids 须为用例编号数组")
        known = {case["id"] for case in cases}
        if set(selected) - known:
            raise AiSearchError("invalid_cases", "用例编号不在当前 Bench 中")
        cases = [case for case in cases if not selected or case["id"] in selected]
        concurrency = payload.get("concurrency", 18)
        if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENT_QUERIES:
            raise AiSearchError("invalid_concurrency", "并发数须为 1–18")
        return cases, concurrency

    def estimate(self, payload):
        with self._lock:
            manifest, manifest_hash = self._manifest()
            cases, concurrency = self._options(payload, manifest["cases"])
            return self._estimate(cases, concurrency, self.engine.config.load(), manifest_hash)

    def start(self, payload):
        with self._lock:
            request_id = payload.get("request_id", "")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
                raise AiSearchError("invalid_request_id", "请提供本次运行的唯一请求编号")
            manifest, manifest_hash = self._manifest()
            for summary, report in self._reports(manifest_hash):
                if report.get("request_id") == request_id:
                    return summary
            if self._active:
                raise AiSearchError("benchmark_busy", "已有 Bench 正在运行，请查看进度", 409)
            cases, concurrency = self._options(payload, manifest["cases"])
            settings = self.engine.config.load()
            if not settings.api_key or settings.search_url:
                raise AiSearchError("benchmark_config", "Bench 需要已保存的 AI Key，并使用本站本地检索", 409)
            # Always finish a fresh offline design/source audit before any paid call.
            audit = validate_manifest(manifest, Path(self.engine.search_service.repository.posts_db).resolve())
            if not audit["passed"]:
                raise AiSearchError("benchmark_design_failed", "Bench 离线源记录核查失败，未调用 AI，请检查清单与数据库", 409)
            audit["manifest_sha256"] = manifest_hash
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
            name = stamp + "-admin-" + uuid.uuid4().hex[:8]
            out = self.runs_dir / name
            report = {"label": "Admin 重测", "created_at_beijing": stamp, "model": settings.model,
                      "base_url": settings.base_url, "source": audit["source"],
                      "manifest_sha256": manifest_hash, "request_id": request_id,
                      "code_sha256": hashlib.sha256(b"".join(path.read_bytes() for path in sorted((ROOT / "app").rglob("*ai*.py")))).hexdigest(),
                      "total": len(cases), "concurrency": concurrency, "case_ids": [case["id"] for case in cases],
                      "status": "running", "running_case_ids": [], "runs": [],
                      "estimate": self._estimate(cases, concurrency, settings, manifest_hash),
                      "interpretation": "固定开发回归门槛，不是人工 gold 或总体准确率。"}
            try:
                out.mkdir(parents=True, exist_ok=False)
                _write_json(out / "design-review.json", audit)
                _write_json(out / "results.json", report)
            except OSError as exc:
                raise AiSearchError("benchmark_storage", "Bench 结果目录无法写入，未调用 AI", 503) from exc
            run_id = "run:" + name
            self._active = run_id
            self._thread = threading.Thread(target=self._run, args=(run_id, out, report, cases, settings), daemon=True)
            self._thread.start()
            return self._summary(run_id, report, manifest_hash)

    def _run(self, run_id, out, report, cases, settings):
        started = time.monotonic()

        def execute(case):
            with self._lock:
                report["running_case_ids"].append(case["id"])
            payload = {"query": case["query"], "scope": "all", "mode": "answer", "top_k": 10}
            payload.update({key: case[key] for key in ("date_from", "date_to") if key in case})
            try:
                result = self.engine.run(payload, settings=settings, wait_for_slot=True)
                check = evaluate(case, result)
            except Exception as exc:
                # Exception text may contain credentials from a third-party provider.
                result = {"answer_status": "error", "error_type": type(exc).__name__, "stats": {"usage_unknown": True}}
                check = {"passed": False, "failures": ["request_failed"], "review_required": True}
            return {"case": case, "repeat": 1, "result": result, "check": check}

        try:
            with ThreadPoolExecutor(max_workers=report["concurrency"], thread_name_prefix="ai-bench") as pool:
                futures = [pool.submit(execute, case) for case in cases]
                for future in as_completed(futures):
                    row = future.result()
                    with self._lock:
                        report["runs"].append(row)
                        report["runs"].sort(key=lambda row: row["case"]["id"])
                        report["running_case_ids"].remove(row["case"]["id"])
                        report["wall_elapsed_ms"] = round((time.monotonic() - started) * 1000)
                        _write_json(out / "results.json", report)
            report["status"] = "complete"
        except Exception:
            report["status"], report["error_code"] = "failed", "benchmark_runner_failed"
        finally:
            with self._lock:
                report["wall_elapsed_ms"] = round((time.monotonic() - started) * 1000)
                report["running_case_ids"] = []
                try:
                    _write_json(out / "results.json", report)
                finally:
                    if self._active == run_id:
                        self._active = None
