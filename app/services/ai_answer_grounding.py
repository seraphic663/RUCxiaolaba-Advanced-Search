"""Validate exact evidence quotes and build citations on the server."""

from __future__ import annotations

import re

from app.services.ai_search_retrieval import normalized, passage_index


def grounded_claims(payload: dict, sources: list[dict]) -> tuple[list[dict], int]:
    index = passage_index(sources)
    claims, rejected = [], 0
    raw_claims = payload.get("claims", [])
    if not isinstance(raw_claims, list):
        return [], 1
    for raw in raw_claims[:10]:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not 1 <= len(raw["text"].strip()) <= 800:
            rejected += 1
            continue
        # The model cannot choose numeric source references or invent footnotes.
        text = raw["text"].strip()
        if "[" in text or "]" in text:
            rejected += 1
            continue
        citations = []
        evidence = raw.get("evidence", [])
        valid = isinstance(evidence, list) and 1 <= len(evidence) <= 5
        for citation in evidence if valid else []:
            if not isinstance(citation, dict):
                valid = False
                break
            passage = index.get(citation.get("id")) if isinstance(citation.get("id"), str) else None
            quote = citation.get("quote")
            if not passage or not isinstance(quote, str) or not quote.strip() or len(quote) > 600:
                valid = False
                break
            clean_quote = normalized(quote)
            if clean_quote not in normalized(passage["text"]) or (len(clean_quote) < 2 and clean_quote != normalized(passage["text"])):
                valid = False
                break
            citations.append({"evidence_id": citation["id"], "quote": quote.strip(),
                              "post_id": passage["post_id"], "source_ref": passage["source_ref"],
                              "kind": passage["kind"], "time": passage["time"],
                              "context": passage.get("parent_content", ""),
                              "uncertain": bool(re.search(r"[?？]|好像|可能|大概|应该|或许|似乎|理论上|吧[。！!\s]*$", quote))})
        if valid and citations:
            if any(citation["uncertain"] for citation in citations) and not re.search(r"不确定|好像|可能|大概|应该|或许|似乎|推测|猜测|建议|提及|线索|理论上|语气|记忆", text):
                text += "（原文含疑问或推测，仅作线索）"
            claims.append({"text": text, "citations": citations})
        else:
            rejected += 1
    return claims, rejected


def render_claims(claims: list[dict]) -> str:
    return "\n\n".join(
        claim["text"] + "".join(f"[{ref}]" for ref in dict.fromkeys(c["source_ref"] for c in claim["citations"]))
        for claim in claims
    )
