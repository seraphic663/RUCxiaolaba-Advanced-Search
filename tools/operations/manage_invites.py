#!/usr/bin/env python3
"""Manage AI-search invite codes.

Usage:
  python scripts/manage_invites.py generate --count 50 --daily 30
  python scripts/manage_invites.py list
  python scripts/manage_invites.py disable <hash-prefix>
  python scripts/manage_invites.py enable  <hash-prefix>
  python scripts/manage_invites.py set-quota <hash-prefix> --daily 50
  python scripts/manage_invites.py stats
  python scripts/manage_invites.py cleanup-sessions
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.repositories.ai_access_repository import AIStore, get_store


def cmd_generate(store: AIStore, args: argparse.Namespace) -> None:
    codes = store.generate_codes(
        count=args.count,
        daily_quota=args.daily,
        max_quota=args.max_total,
        note=args.note or "",
        prefix=args.prefix,
    )
    total_text = str(args.max_total) if args.max_total > 0 else "unlimited"
    print(
        f"Generated {len(codes)} invite code(s) "
        f"(daily quota: {args.daily}, total quota: {total_text})\n"
    )
    for c in codes:
        print(f"  {c}")
    print()
    print("⚠️  PLAINTEXT CODES SHOWN ONLY ONCE — copy them now.")
    print("   Only SHA-256 hashes are stored in the database.\n")


def cmd_list(store: AIStore, args: argparse.Namespace) -> None:
    rows = store.list_codes()
    if not rows:
        print("(no invite codes in database)")
        return

    header = f"{'Hash prefix':<18} {'Daily':>6} {'Today':>6} {'Total':>7} {'Active':>7}  Note"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['hash_prefix']:<18} "
            f"{r['daily_quota']:>6} "
            f"{r['used_today']:>6} "
            f"{r['used_total']:>7} "
            f"{'Yes' if r['is_active'] else 'NO':>7}  "
            f"{r['note']}"
        )


def cmd_disable(store: AIStore, args: argparse.Namespace) -> None:
    ok = store.set_active(args.prefix, False)
    print("✅ Code disabled." if ok else "❌ No matching code found.")


def cmd_enable(store: AIStore, args: argparse.Namespace) -> None:
    ok = store.set_active(args.prefix, True)
    print("✅ Code enabled." if ok else "❌ No matching code found.")


def cmd_set_quota(store: AIStore, args: argparse.Namespace) -> None:
    ok = store.set_quota(args.prefix, args.daily, getattr(args, "max", None))
    print("✅ Quota updated." if ok else "❌ No matching code found.")


def cmd_stats(store: AIStore, args: argparse.Namespace) -> None:
    s = store.get_stats()
    print(f"Total codes:    {s['total_codes']}")
    print(f"Active codes:   {s['active_codes']}")
    print(f"Active sessions:{s['active_sessions']}")
    print(f"Today queries:  {s['today_queries']}")


def cmd_cleanup(store: AIStore, args: argparse.Namespace) -> None:
    n = store.cleanup_expired_sessions()
    print(f"🧹 Removed {n} expired session(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage AI invite codes")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="Generate new invite codes")
    p_gen.add_argument("--count", type=int, default=50, help="Number of codes")
    p_gen.add_argument("--daily", type=int, default=30, help="Daily query quota per code")
    p_gen.add_argument(
        "--max-total",
        type=int,
        default=0,
        help="Lifetime query quota per code; 0 means unlimited",
    )
    p_gen.add_argument("--note", default="", help="Admin note")
    p_gen.add_argument("--prefix", default="XLB", help="Code prefix")
    p_gen.add_argument("--db", default=None, help="Path to ai.db")

    p_list = sub.add_parser("list", help="List all invite codes")
    p_list.add_argument("--db", default=None, help="Path to ai.db")

    p_disable = sub.add_parser("disable", help="Disable a code by hash prefix")
    p_disable.add_argument("prefix", help="First N chars of the SHA-256 hash")
    p_disable.add_argument("--db", default=None, help="Path to ai.db")

    p_enable = sub.add_parser("enable", help="Enable a code by hash prefix")
    p_enable.add_argument("prefix", help="First N chars of the SHA-256 hash")
    p_enable.add_argument("--db", default=None, help="Path to ai.db")

    p_quota = sub.add_parser("set-quota", help="Change daily quota for a code")
    p_quota.add_argument("prefix", help="First N chars of the SHA-256 hash")
    p_quota.add_argument("--daily", type=int, required=True, help="New daily quota")
    p_quota.add_argument("--max", type=int, default=None, help="New max total quota")
    p_quota.add_argument("--db", default=None, help="Path to ai.db")

    p_stats = sub.add_parser("stats", help="Show aggregate stats")
    p_stats.add_argument("--db", default=None, help="Path to ai.db")

    p_clean = sub.add_parser("cleanup-sessions", help="Remove expired sessions")
    p_clean.add_argument("--db", default=None, help="Path to ai.db")

    args = parser.parse_args()
    store = get_store(args.db if hasattr(args, "db") and args.db else None)

    handlers = {
        "generate": cmd_generate,
        "list": cmd_list,
        "disable": cmd_disable,
        "enable": cmd_enable,
        "set-quota": cmd_set_quota,
        "stats": cmd_stats,
        "cleanup-sessions": cmd_cleanup,
    }
    handlers[args.command](store, args)


if __name__ == "__main__":
    main()
