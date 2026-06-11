import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.migrations.rebuild_sqlite_search_index import main


if __name__ == "__main__":
    raise SystemExit(main())
