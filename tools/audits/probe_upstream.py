"""Manually probe the upstream API endpoints used by the crawler.

This is an operator command, not a pytest test. By default it performs two
list requests. ``--with-detail`` adds one detail request for the first post
returned by ``lists``.

Usage:
    python -m tools.audits.probe_upstream
    python -m tools.audits.probe_upstream --with-detail
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from crawler.client import MiniProgramClient, load_cookie
from crawler.config import DEFAULT_CONFIG


def summarize_list(data: dict | None) -> dict:
    articles = list((data or {}).get("list", []))
    return {
        "items": len(articles),
        "first_ids": [str(item.get("id") or "") for item in articles[:5]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe the crawler's upstream lists/lists2 endpoints",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--with-detail",
        action="store_true",
        help="also make one article detail request",
    )
    args = parser.parse_args(argv)

    client = MiniProgramClient(load_cookie(args.config))
    results: dict[str, dict] = {}
    first_post_id = ""
    failed = False

    for endpoint in ("lists", "lists2"):
        data, error = client.list_page(endpoint, 1)
        if error:
            results[endpoint] = {"ok": False, "error": error}
            failed = True
            continue
        summary = summarize_list(data)
        results[endpoint] = {"ok": True, **summary}
        if endpoint == "lists" and summary["first_ids"]:
            first_post_id = summary["first_ids"][0]

    if args.with_detail:
        if not first_post_id:
            results["detail"] = {"ok": False, "error": "no post id from lists"}
            failed = True
        else:
            data, error = client.article(first_post_id)
            if error:
                results["detail"] = {
                    "ok": False,
                    "post_id": first_post_id,
                    "error": error,
                }
                failed = True
            else:
                results["detail"] = {
                    "ok": True,
                    "post_id": first_post_id,
                    "keys": sorted((data or {}).keys()),
                }

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
