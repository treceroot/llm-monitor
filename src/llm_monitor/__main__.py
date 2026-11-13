"""CLI entry point for llm-monitor."""
from __future__ import annotations

import argparse

from .app import LLMMonitorApp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="llm-monitor",
        description="Terminal monitor for llama.cpp + GPU/CPU/RAM usage",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8001",
        help="llama.cpp server base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="poll interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--theme",
        default=None,
        help="theme name, e.g. textual-dark, nord, catppuccin-mocha, dracula",
    )
    args = parser.parse_args()
    app = LLMMonitorApp(url=args.url, interval=args.interval, theme=args.theme)
    app.run()


if __name__ == "__main__":
    main()
