"""Build the optional symbol search sidecar.

Example:
    python -m tools.build_symbol_index --posts-db data/posts.db --output data/symbol_index.db
"""

from __future__ import annotations

import argparse
from pathlib import Path

from storage.symbol_index import build_symbol_index


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build symbol search sidecar")
    parser.add_argument("--posts-db", default="data/posts.db", help="source posts.db")
    parser.add_argument(
        "--output",
        default="data/symbol_index.db",
        help="output symbol_index.db",
    )
    parser.add_argument("--sample-mod", type=int, default=1)
    args = parser.parse_args(argv)

    stats = build_symbol_index(
        Path(args.posts_db),
        Path(args.output),
        sample_mod=args.sample_mod,
    )
    print(
        "built symbol index: "
        f"source_rows={stats.source_rows:,} "
        f"symbol_rows={stats.rows:,} "
        f"elapsed={stats.elapsed_seconds:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
