"""Inspectable CNY estimates, never an account bill or a price for a proxy."""

from __future__ import annotations

import heapq
from urllib.parse import urlsplit

PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICE_CHECKED = "2026-11-05"
# CNY / million tokens: off-peak cache hit, cache miss, output. Peak is 2x.
PRICES = {"deepseek-flash": (0.02, 1.0, 4.0), "deepseek-v4-pro": (0.15, 4.5, 13.5)}


def count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def usage_cost(settings, usage: dict, *, calls=0, reported=0) -> dict:
    rates = PRICES.get(settings.model) if urlsplit(settings.base_url).hostname == "api.deepseek.com" else None
    prompt, output = count(usage.get("prompt_tokens")), count(usage.get("completion_tokens"))
    hit = min(prompt, count(usage.get("prompt_cache_hit_tokens")))
    result = {"available": bool(rates), "currency": "CNY", "source_url": PRICE_SOURCE,
              "checked_on": PRICE_CHECKED, "model": settings.model,
              "usage_complete": reported == calls, "reported_calls": reported, "calls": calls,
              "excludes_retrieval_api": True, "prompt_tokens": prompt, "completion_tokens": output}
    if rates:
        low = (hit * rates[0] + (prompt - hit) * rates[1] + output * rates[2]) / 1_000_000
        result.update(low_cny=round(low, 6), high_cny=round(low * 2, 6))
    return result


def estimate(settings, samples: list[dict], *, concurrency=1, basis="fallback", baseline_concurrency=1) -> dict:
    """Use a comparable historical query per sample; do not assume a cache hit."""
    durations, prompt, output = [], 0, 0
    for sample in samples:
        stats = sample.get("stats", {})
        usage = stats.get("usage", {})
        if stats.get("ai_calls") == 0 and "ai_calls" in stats:
            durations.append(max(0.2, stats.get("elapsed_ms", 1000) / 1000))
            continue
        prompt += count(usage.get("prompt_tokens")) or 12000
        output += count(usage.get("completion_tokens")) or 1000
        durations.append(max(1, stats.get("elapsed_ms", 20000) / 1000))
    slots = [0.0] * min(max(1, concurrency), max(1, len(durations)))
    for duration in sorted(durations, reverse=True):
        heapq.heapreplace(slots, slots[0] + duration)
    cost = usage_cost(settings, {"prompt_tokens": prompt, "completion_tokens": output})
    # Serial timings do not measure contention. Widen the upper end when
    # projecting to greater concurrency; a comparable live run supersedes it.
    contention = min(4, max(1, 1 + 0.15 * (concurrency / max(1, baseline_concurrency) - 1)))
    result = {"query_count": len(samples), "concurrency": concurrency, "basis": basis, "cost": cost,
              "seconds_low": round(max(slots, default=0) * 0.7, 1),
              "seconds_high": round(max(slots, default=0) * 2 * contention + 10, 1),
              "baseline_concurrency": baseline_concurrency}
    if cost["available"]:
        cost["low_cny"] = round(cost["low_cny"] * 0.7, 6)
        cost["high_cny"] = round(cost["high_cny"] * 1.5, 6)
    return result
