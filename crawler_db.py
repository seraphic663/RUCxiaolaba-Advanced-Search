#!/usr/bin/env python3
"""Compatibility entrypoint for the unified crawler CLI."""

if __name__ == "__main__":
    from crawler.cli import main

    raise SystemExit(main())
else:
    import sys
    from crawler import cli as _implementation

    sys.modules[__name__] = _implementation
