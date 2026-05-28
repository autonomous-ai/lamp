"""Backward-compatible entry point. Use `python -m dlserver` instead."""

from dlserver.app import main

if __name__ == "__main__":
    main()
