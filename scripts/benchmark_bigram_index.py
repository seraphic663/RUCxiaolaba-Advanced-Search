import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.benchmarks.benchmark_bigram_index import *  # noqa: F401,F403,E402


if __name__ == "__main__":
    main()
