"""Shared, source-backed benchmark gates for the CLI and admin runner."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.services.ai_search_retrieval import normalized


def evaluate(case: dict, result: dict) -> dict:
    generated = result.get("answer_status") == "generated" and bool(result.get("answer"))
    ids = {s["post_id"] for s in result.get("sources", [])}
    citations = result.get("claims", [])
    cited_ids = {c["post_id"] for claim in citations for c in claim.get("citations", [])}
    answer = re.sub(r"\[\d+\]", "", result.get("answer", ""))
    failures = []
    status = result.get("answer_status")
    if status in {"error", "failed", "unverified", "not_configured"} or result.get("stats", {}).get("pipeline_errors"):
        failures.append("pipeline_failure_or_unsupported_generation_blocked")
    if case["answerable"]:
        if not generated:
            failures.append("answerable_query_abstained")
        if case.get("reference_policy", "required") == "required" and not any(p in ids for p in case.get("evidence_post_ids", [])):
            failures.append("reference_evidence_not_retrieved")
        if case.get("reference_policy", "required") == "required" and citations and not any(p in cited_ids for p in case.get("evidence_post_ids", [])):
            failures.append("reference_evidence_not_cited")
        if not all(re.search(pattern, answer) for pattern in case.get("required_patterns", [])):
            failures.append("expected_answer_points_missing")
        if generated and "citation_check" not in result:
            failures.append("citation_verification_unavailable")
        if generated and "citation_check" in result:
            sources = {source["post_id"]: source for source in result.get("sources", [])}
            scope_texts = []
            quote_error = any(not claim.get("citations") for claim in citations)
            for claim in citations:
                for citation in claim.get("citations", []):
                    source = sources.get(citation.get("post_id"), {})
                    passage = next((p for p in source.get("passages", []) if p["id"] == citation.get("evidence_id")), None)
                    if not passage or normalized(citation.get("quote", "")) not in normalized(passage["text"]):
                        quote_error = True
                    scope_texts.append(source.get("content", "") + "\n" + citation.get("quote", ""))
            if not citations or quote_error:
                failures.append("missing_or_wrongly_owned_citation")
            if not any(all(re.search(pattern, text) for pattern in case.get("source_scope_patterns", [])) for text in scope_texts):
                failures.append("cited_evidence_outside_expected_question_scope")
    elif generated or answer.strip():
        failures.append("unanswerable_query_received_factual_answer")
    elif status not in {"no_answer", "no_results"}:
        failures.append("not_a_valid_abstention")
    if ids & set(case.get("exclude_post_ids", [])):
        failures.append("known_off_topic_source_shown")
    return {"passed": not failures, "failures": failures,
            "expected_answerable": case["answerable"], "generated": generated,
            "known_reference_retrieved": bool(ids & set(case.get("evidence_post_ids", []))),
            "known_reference_cited": bool(cited_ids & set(case.get("evidence_post_ids", []))),
            "review_required": True}


def validate_manifest(manifest: dict, db_path: Path) -> dict:
    """Check the reference set without making any model or upstream requests."""
    cases = manifest["cases"]
    failures, references, seen = [], [], set()
    if len({c.get("domain") for c in cases}) < 6 or len({c.get("intent") for c in cases}) < 6:
        failures.append("insufficient_domain_or_intent_diversity")
    if sum(c.get("answerable") is True for c in cases) < 4 or sum(c.get("answerable") is False for c in cases) < 4:
        failures.append("insufficient_positive_or_negative_cases")
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        source_count, latest = conn.execute("select count(*),max(create_time) from posts").fetchone()
        for case in cases:
            if not isinstance(case.get("id"), str) or case["id"] in seen:
                failures.append("duplicate_or_invalid_id")
            seen.add(case["id"])
            if type(case.get("answerable")) is not bool or not case.get("reference_answer") or not case.get("query"):
                failures.append(case["id"] + ":missing_expectation")
            ids = case.get("evidence_post_ids", []) + case.get("related_post_ids", [])
            snapshots = []
            for post_id in ids:
                post = conn.execute("select id,content,create_time from posts where id=?", (post_id,)).fetchone()
                if post is None:
                    failures.append(case["id"] + ":reference_post_missing:" + post_id)
                    continue
                comments = [dict(r) for r in conn.execute("select comment_id,parent_comment_id,detail,create_time from comments where post_id=? order by create_time", (post_id,))]
                snapshots.append({"post": dict(post), "comments": comments})
            if case["answerable"]:
                if not snapshots or not case.get("required_patterns"):
                    failures.append(case["id"] + ":answerable_without_reference")
                reference_text = "\n".join(s["post"]["content"] + "\n" + "\n".join(c["detail"] for c in s["comments"]) for s in snapshots)
                for pattern in case.get("required_patterns", []):
                    if not re.search(pattern, reference_text):
                        failures.append(case["id"] + ":answer_point_absent_from_reference:" + pattern)
            elif not case.get("negative_kind"):
                failures.append(case["id"] + ":negative_reason_missing")
            if case["id"] == "006" and any(snapshot["comments"] for snapshot in snapshots):
                failures.append("006:unanswered_reference_changed_review_required")
            if case.get("date_from") and case["date_from"] <= latest[:10] and case.get("negative_kind") == "empty_time_window":
                failures.append(case["id"] + ":time_window_is_not_empty_anymore")
            references.append({"case_id": case["id"], "snapshots": snapshots})
    return {"passed": not failures, "failures": failures,
            "source": {"posts": source_count, "latest_post_time": latest,
                       "bytes": db_path.stat().st_size, "mtime_ns": db_path.stat().st_mtime_ns},
            "cases": len(cases), "answerable": sum(c["answerable"] for c in cases),
            "unanswerable": sum(not c["answerable"] for c in cases),
            "domains": sorted({c["domain"] for c in cases}), "intents": sorted({c["intent"] for c in cases}),
            "reference_evidence": references}
