"""Bounded recall, passage scoring and source diversity for AI search."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from urllib.parse import quote

from app.domain.ai_search import excerpt

MAX_QUERIES = 8
MAX_PRIMARY_QUERIES = 6
MAX_CANDIDATES = 240
RERANK_CANDIDATES = 20


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).casefold())


def grams(text: str) -> list[str]:
    runs = re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", normalized(text))
    return [token for run in runs for token in ([run] if run.isascii() or len(run) < 2 else [run[i:i + 2] for i in range(len(run) - 1)])]


def queries_from_plan(query: str, plan: dict) -> list[str]:
    raw = plan.get("queries", []) if isinstance(plan.get("queries"), list) else []
    # A planner sometimes puts alternative facets in one AND expression. Keep
    # the original question and add bounded relaxed routes; final relevance and
    # answerability always use the full original question and all its limits.
    relaxed = []
    for value in raw[:5]:
        if isinstance(value, str) and len(value) <= 80:
            atoms = value.split()
            if len(atoms) > 2:
                relaxed.extend(atoms[0] + " " + atom for atom in atoms[1:4])
            else:
                relaxed.append(value)
    values = [query, *relaxed, *raw[:5]]
    queries = []
    for value in values:
        if isinstance(value, str) and value.strip() and len(value.strip()) <= (500 if not queries else 80):
            value = " ".join(value.split())
            if value not in queries:
                queries.append(value)
        if len(queries) >= MAX_PRIMARY_QUERIES:
            break
    return queries


def _body_signature(text: str) -> set[str]:
    text = re.sub(r"[\W_]", "", normalized(text))
    return {text[i:i + 3] for i in range(max(0, len(text) - 2))}


def diverse_documents(documents: list[dict]) -> tuple[list[dict], int]:
    def replies(document):
        boilerplate = {"谢谢", "谢谢你", "谢谢啦", "谢", "感谢", "蹲", "同问", "dd", "顶"}
        values = [normalized(comment["content"]) for comment in document["comments"]]
        return {value for value in values if value and re.sub(r"[\W_]", "", value) not in boilerplate}

    kept = []
    removed = 0
    for document in documents:
        body = document["content"]
        signature = _body_signature(body) if len(body) >= 80 else set()
        duplicate = None
        for other in kept:
            other_signature = other.get("_signature", set())
            if signature and other_signature and len(signature & other_signature) / len(signature | other_signature) >= .82:
                # Different substantive replies are different evidence. Do not
                # erase a conflicting answer just because the question repeats.
                if replies(document) == replies(other):
                    duplicate = other
                    break
        if duplicate:
            duplicate["duplicate_post_ids"].append(document["post_id"])
            removed += 1
        else:
            document["_signature"] = signature
            kept.append(document)
    for document in kept:
        document.pop("_signature", None)
    return kept, removed


def passage_index(sources: list[dict]) -> dict[str, dict]:
    return {
        passage["id"]: {**passage, "post_id": source["post_id"], "source_ref": source["ref"]}
        for source in sources for passage in source["passages"]
    }


class AiSearchRetrieval:
    def __init__(self, search, evidence, client):
        self.search, self.evidence, self.client = search, evidence, client

    def retrieve(self, request: dict, settings, queries: list[str]) -> tuple[list[dict], dict]:
        candidates, families, backends = {}, {}, set()
        fallback_attempted = False
        fields = {"body", "cmt"} if request["scope"] == "all" else {"body"}
        route_index = 0
        while route_index < len(queries):
            query = queries[route_index]
            route_index += 1
            if settings.search_url:
                result = self.client.retrieve(settings, query, limit=60, scope=request["scope"], **{
                    "from": request["date_from"].strftime("%Y-%m-%d") if request["date_from"] else "",
                    "to": request["date_to"].strftime("%Y-%m-%d") if request["date_to"] else "",
                })
            else:
                result = self.search.search_cursor(
                    query, "time", 1, 60, scope=request["scope"], admin=True,
                    admin_fields=fields, source_state="available",
                    date_from=request["date_from"], date_to=request["date_to"],
                )
            backends.add(str(result.get("search_backend", "external")))
            for rank, item in enumerate(result["results"][:60], 1):
                if not isinstance(item, dict) or not isinstance(item.get("id"), (str, int)):
                    continue
                post_id = str(item["id"])
                if not post_id or len(post_id) > 128 or item.get("source_state", "available") != "available":
                    continue
                if post_id not in candidates and len(candidates) >= MAX_CANDIDATES:
                    continue
                candidates.setdefault(post_id, {**item, "matched_queries": []})["matched_queries"].append(query)
                family = normalized(query)
                votes = families.setdefault(post_id, {})
                votes[family] = max(votes.get(family, 0), 1 / (60 + rank))
            if route_index == len(queries) and len(candidates) < 3 and not settings.search_url and not fallback_attempted:
                fallback_attempted = True
                for seed in self.evidence.anchor_candidates(request["query"], self.search.repository.bigram_db):
                    if seed not in queries and len(queries) < MAX_QUERIES:
                        queries.append(seed)
        terms = list(dict.fromkeys(term for query in queries for term in query.split()))[:32]
        documents = []
        for post_id, item in candidates.items():
            body = excerpt(item.get("content", ""), terms, 1200)
            comments = self.evidence.comments(post_id, terms) if request["scope"] == "all" and not settings.search_url else []
            passages = [{"id": "p:" + post_id, "kind": "body", "text": body, "time": str(item.get("time", ""))}] if body else []
            for comment in comments:
                passages.append({"id": "c:" + post_id + ":" + comment["id"], "kind": "comment",
                                 "text": comment["content"], "time": comment["time"],
                                 "parent_id": comment["parent_id"], "parent_content": comment.get("parent_content", "")})
            documents.append({
                "post_id": post_id, "content": body, "comments": comments, "passages": passages,
                "time": str(item.get("time", ""))[:32], "category": str(item.get("category", ""))[:100],
                "crawl_status": str(item.get("crawl_status", "unknown")),
                "url": "/admin?post=" + quote(post_id, safe="") if not settings.search_url else "",
                "matched_queries": list(dict.fromkeys(item["matched_queries"])),
                "duplicate_post_ids": [], "_rrf": sum(families[post_id].values()),
            })
        self._score(documents, request["query"], queries)
        documents.sort(key=lambda document: (-document["retrieval_score"], document["post_id"]))
        diverse, removed = diverse_documents(documents)
        return diverse[:RERANK_CANDIDATES], {
            "candidate_count": len(candidates), "retrieval_calls": len(queries), "backends": sorted(backends),
            "duplicates_removed": removed, "ranked_candidate_count": min(len(diverse), RERANK_CANDIDATES),
        }

    @staticmethod
    def _score(documents: list[dict], query: str, queries: list[str]):
        features = set(grams(query)) | {gram for text in queries for gram in grams(text)}
        frequencies = [Counter(grams(document["content"] + " " + " ".join(c["content"] for c in document["comments"]))) for document in documents]
        sizes = [sum(values.values()) for values in frequencies]
        average = sum(sizes) / max(1, len(sizes))
        df = Counter(term for values in frequencies for term in values if term in features)
        for document, values, size in zip(documents, frequencies, sizes):
            score = 0.0
            for term in features:
                tf = values.get(term, 0)
                if tf:
                    idf = math.log(1 + (len(documents) - df[term] + .5) / (df[term] + .5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * size / max(1, average)))
            document["retrieval_score"] = round(score + document.pop("_rrf") * 2, 6)

    @staticmethod
    def for_model(documents: list[dict]) -> list[dict]:
        return [{"post_id": d["post_id"], "post_time": d["time"], "body": d["content"],
                 "passages": d["passages"]} for d in documents]

    @staticmethod
    def sources(documents: list[dict], limit: int) -> list[dict]:
        return [{**document, "ref": index + 1} for index, document in enumerate(documents[:limit])]
