#!/usr/bin/env python3
"""Compatibility entrypoint for the web application."""

if __name__ == "__main__":
    from app.http.server import main

    raise SystemExit(main())
else:
    # Preserve ``import server`` compatibility, including monkey-patching module
    # globals in existing tests and local tools.
    import sys

    from app.http import server as _implementation

    sys.modules[__name__] = _implementation
