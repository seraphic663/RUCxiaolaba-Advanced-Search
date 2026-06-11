"""Unified crawler command-line interface."""

from __future__ import annotations

import argparse
import sys
from crawler.client import MiniProgramClient, load_cookie
from crawler.config import (
    DEFAULT_CONFIG,
    DEFAULT_DB,
    DEFAULT_LOCK_TIMEOUT,
)
from crawler.normalizer import normalize_detail
from crawler.service import CrawlerService


def make_session(cookie: str):
    """Compatibility alias returning the new API client."""
    return MiniProgramClient(cookie)


def api_get(client: MiniProgramClient, path: str, params=None):
    """Compatibility alias for diagnostics that used the old helper."""
    return client.get(path, params)


def fetch_detail(client: MiniProgramClient, post_id: str):
    data, error = api_get(
        client,
        "/article/article/info",
        {"community_id": 4, "id": str(post_id)},
    )
    if error or not data:
        return None
    return normalize_detail(str(post_id), data)


def latest_list_id(client: MiniProgramClient) -> int:
    return client.latest_id()


def _service(args) -> CrawlerService:
    return CrawlerService(
        db_path=args.db_path,
        cookie=load_cookie(args.config),
        lock_timeout=args.lock_timeout,
        init_schema=args.init_schema,
        api_get_fn=api_get,
    )


def command_detail_fill(args) -> int:
    ids = [
        token.strip()
        for token in args.ids.replace(",", " ").split()
        if token.strip()
    ]
    _service(args).fill_details(
        ids,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
    )
    return 0


def command_incremental(args) -> int:
    _service(args).scan_pages(
        command=args.command,
        endpoint=args.endpoint,
        start_page=args.start_page,
        pages=args.pages,
        min_pages=args.min_pages,
        stop_unchanged=args.stop_unchanged,
        max_details=args.max_details,
        dry_run=args.dry_run,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
    )
    return 0


def command_new(args) -> int:
    args.endpoint = "lists"
    return command_incremental(args)


def command_refresh(args) -> int:
    args.endpoint = "lists2"
    return command_incremental(args)


def command_backfill(args) -> int:
    if args.start_page < 2 and not args.force_start_page:
        args.start_page = 2
    return command_incremental(args)


def command_phase1(args) -> int:
    _service(args).scan_id_range(
        from_date=args.from_date,
        to_date=args.to_date,
        start_id=args.start_id,
        end_id=args.end_id,
        workers=args.workers,
        chunk_size=args.chunk_size,
        restart=args.restart,
        dry_run=args.dry_run,
    )
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--lock-timeout", type=int, default=DEFAULT_LOCK_TIMEOUT)


def add_scan_options(
    parser: argparse.ArgumentParser,
    *,
    endpoint: str | None,
    pages: int = 500,
    min_pages: int = 20,
    stop_unchanged: int = 300,
) -> None:
    add_common(parser)
    if endpoint is None:
        parser.add_argument(
            "--endpoint", choices=("lists", "lists2"), default="lists2"
        )
    else:
        parser.set_defaults(endpoint=endpoint)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=pages)
    parser.add_argument("--min-pages", type=int, default=min_pages)
    parser.add_argument("--stop-unchanged", type=int, default=stop_unchanged)
    parser.add_argument(
        "--max-details",
        type=int,
        default=0,
        help="stop after this many detail records; 0 means unlimited",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-delay", type=float, default=0.3)
    parser.add_argument("--max-delay", type=float, default=0.8)


def _add_page_command(
    subparsers,
    name,
    *,
    aliases=(),
    help_text,
    endpoint,
    handler,
    pages=500,
    min_pages=20,
    stop_unchanged=300,
):
    parser = subparsers.add_parser(name, aliases=list(aliases), help=help_text)
    add_scan_options(
        parser,
        endpoint=endpoint,
        pages=pages,
        min_pages=min_pages,
        stop_unchanged=stop_unchanged,
    )
    parser.set_defaults(func=handler, canonical_command=name)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified DB-first crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_page_command(
        sub,
        "sync-latest",
        aliases=("new",),
        help_text="scan newest posts and upsert changed details",
        endpoint="lists",
        handler=command_new,
    )
    _add_page_command(
        sub,
        "sync-active",
        aliases=("refresh",),
        help_text="scan active/comment stream and refresh changed posts",
        endpoint="lists2",
        handler=command_refresh,
    )
    history = _add_page_command(
        sub,
        "scan-history",
        aliases=("backfill",),
        help_text="scan older list pages to fill historical gaps",
        endpoint=None,
        handler=command_backfill,
        stop_unchanged=600,
    )
    history.set_defaults(start_page=2)
    history.add_argument(
        "--force-start-page",
        action="store_true",
        help="allow page 1 for explicit rechecks",
    )

    detail = sub.add_parser(
        "fill-details",
        aliases=["detail-fill"],
        help="fetch explicit post IDs and upsert their full details",
    )
    add_common(detail)
    detail.add_argument("--batch-size", type=int, default=100)
    detail.add_argument("--ids", required=True)
    detail.add_argument("--dry-run", action="store_true")
    detail.add_argument("--min-delay", type=float, default=0.8)
    detail.add_argument("--max-delay", type=float, default=2.0)
    detail.set_defaults(func=command_detail_fill)

    scan_ids = sub.add_parser(
        "scan-id-range",
        aliases=["phase1"],
        help="scan every post ID in an explicit or date-derived range",
    )
    add_common(scan_ids)
    scan_ids.add_argument("--from-date", default="")
    scan_ids.add_argument("--to-date", default="")
    scan_ids.add_argument("--start-id", type=int, default=0)
    scan_ids.add_argument("--end-id", type=int, default=0)
    scan_ids.add_argument("--workers", type=int, default=4)
    scan_ids.add_argument("--chunk-size", type=int, default=500)
    scan_ids.add_argument("--restart", action="store_true")
    scan_ids.add_argument("--dry-run", action="store_true")
    scan_ids.set_defaults(func=command_phase1)

    incremental = sub.add_parser(
        "incremental",
        help="compatibility command for selecting lists/lists2 directly",
    )
    add_scan_options(incremental, endpoint=None)
    incremental.set_defaults(func=command_incremental)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[crawler] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
