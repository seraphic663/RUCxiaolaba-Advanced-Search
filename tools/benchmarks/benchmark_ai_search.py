"""Run source-backed answer/abstention cases; raw local results stay Git-ignored."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.ai_search_benchmark_spec import evaluate, validate_manifest
from app.services.ai_search_config import AiSearchConfigStore
from app.services.ai_search_service import AiSearchService
from app.services.search_service import SearchService

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = Path(__file__).with_name("ai_search_cases.json")




def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured real AI API")
    parser.add_argument("--validate-only", action="store_true", help="Validate the frozen benchmark against local records; zero AI calls")
    parser.add_argument("--cases", default="", help="Comma-separated case IDs; blank means all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=18, help="Concurrent queries, 1-18")
    parser.add_argument("--label", default="run")
    parser.add_argument("--output-dir", default=str(ROOT / "reports/generated/ai-search"))
    args = parser.parse_args(argv)
    if not args.live and not args.validate_only:
        parser.error("--live is required; this command makes paid AI requests")
    if not 1 <= args.repeats <= 5:
        parser.error("repeats must be 1-5")
    if not 1 <= args.concurrency <= 18:
        parser.error("concurrency must be 1-18")
    manifest = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    manifest_hash = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
    audit_path = Path(args.output_dir) / "design-review.json"
    if args.validate_only:
        audit = validate_manifest(manifest, ROOT / "data/posts.db")
        audit["manifest_sha256"] = manifest_hash
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in audit.items() if key != "reference_evidence"}, ensure_ascii=False))
        return 0 if audit["passed"] else 1
    if not audit_path.exists():
        parser.error("Run --validate-only before spending API requests")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    db_stat = (ROOT / "data/posts.db").stat()
    if not audit.get("passed") or audit.get("manifest_sha256") != manifest_hash or audit["source"]["mtime_ns"] != db_stat.st_mtime_ns:
        parser.error("Benchmark or source changed since design review; run --validate-only first")
    selected = set(args.cases.split(",")) if args.cases else set()
    cases = [case for case in manifest["cases"] if not selected or case["id"] in selected]
    if not cases or selected - {case["id"] for case in cases}:
        parser.error("unknown case ID")
    config = AiSearchConfigStore(ROOT / "data/.admin_ai_search.json")
    settings = config.load()
    if not settings.api_key or settings.search_url:
        parser.error("benchmark requires an AI Key and the local retrieval source")
    search = SearchService(ROOT / "data/posts.db", ROOT / "data/bigram_index.db", ROOT / "data/symbol_index.db")
    class SnapshotConfig:
        def load(self):
            return settings

    engine = AiSearchService(search, SnapshotConfig())
    with sqlite3.connect((ROOT / "data/posts.db").as_uri() + "?mode=ro", uri=True) as conn:
        count, latest = conn.execute("select count(*),max(create_time) from posts").fetchone()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    out = Path(args.output_dir) / (stamp + "-" + re.sub(r"[^A-Za-z0-9_-]", "_", args.label))
    out.mkdir(parents=True, exist_ok=False)
    code_files = sorted((ROOT / "app").rglob("*ai*.py"))
    code_hash = hashlib.sha256(b"".join(path.read_bytes() for path in code_files)).hexdigest()
    report = {"label": args.label, "created_at_beijing": stamp, "model": settings.model,
              "source": {"posts": count, "latest_post_time": latest},
              "code_sha256": code_hash, "manifest_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
              "interpretation": "Automated regression checks plus manual source review; not human gold or a general accuracy estimate.",
              "concurrency": args.concurrency, "total": len(cases) * args.repeats, "status": "running", "runs": []}
    started = time.monotonic()

    def execute(case, repeat):
        payload = {"query": case["query"], "scope": "all", "mode": "answer", "top_k": 10}
        payload.update({key: case[key] for key in ("date_from", "date_to") if key in case})
        try:
            result = engine.run(payload)
            check = evaluate(case, result)
        except Exception as exc:
            result = {"answer_status": "error", "error_type": type(exc).__name__}
            check = {"passed": False, "failures": ["request_failed"], "review_required": True}
        return {"case": case, "repeat": repeat + 1, "result": result, "check": check}

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(execute, case, repeat) for repeat in range(args.repeats) for case in cases]
        for future in as_completed(futures):
            row = future.result()
            case, result, check = row["case"], row["result"], row["check"]
            report["runs"].append(row)
            report["runs"].sort(key=lambda row: (row["repeat"], row["case"]["id"]))
            report["wall_elapsed_ms"] = round((time.monotonic() - started) * 1000)
            (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case["id"], "repeat": row["repeat"], "passed": check["passed"],
                              "status": result.get("answer_status"), "failures": check["failures"],
                              "ai_calls": result.get("stats", {}).get("ai_calls")}, ensure_ascii=False), flush=True)
    report["status"] = "complete"
    report["wall_elapsed_ms"] = round((time.monotonic() - started) * 1000)
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# AI 检索 benchmark", "", f"模型：{settings.model}；本地库最新帖子：{latest}。", "",
             "这是小规模开发回归集，参考答案经源记录核对；自动门槛不等于人工评测或完整事实核验。", ""]
    for row in report["runs"]:
        case, result, check = row["case"], row["result"], row["check"]
        lines += [f"## {case['id']} · {case['query']} · 第 {row['repeat']} 次", "",
                  f"预期：{'有答案' if case['answerable'] else '无足够证据，须拒答'}。", "",
                  "参考答案：" + case["reference_answer"], "", "实际回答：", "",
                  result.get("answer") or f"（{result.get('answer_status')}：未给出事实答案）", "",
                  "自动检查：" + ("通过，仍需核对原文" if check["passed"] else "失败：" + ", ".join(check["failures"])), "",
                  "来源：" + ", ".join(s["post_id"] for s in result.get("sources", [])), ""]
    (out / "answers.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(out), "passed": sum(r["check"]["passed"] for r in report["runs"]), "runs": len(report["runs"])}, ensure_ascii=False))
    return 0 if all(r["check"]["passed"] for r in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
