"""`python -m console` — build the index from config and serve it.

A clean checkout runs the console with this and nothing else (no build step,
no database — docs/stack-decision.md). The index is built from the enabled
adapters at start and served read-only; a restart re-derives it, which is the
whole of §5.6's "renders, never owns" made operational.

Commands:

- ``console`` / ``console serve`` — build the index and serve it.
- ``console index`` — build the index and print a machine-readable JSON dump
  of its entities and edges to stdout (the versioned §5.3 projection).
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from .config import build_index, load_config
from .index.graph import Index
from .render.json import index_dump
from .server.app import serve


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="console", description=__doc__)
    ap.add_argument("--config", default="config.yaml",
                    help="path to config.yaml (gitignored; see config.example.yaml)")
    ap.add_argument("--host", default=None, help="override bind host (default from config)")
    ap.add_argument("--port", type=int, default=None, help="override port (default from config)")
    sub = ap.add_subparsers(dest="command", metavar="{serve,index}")
    # Sub-parser flags use SUPPRESS so a flag given before the subcommand
    # survives (the sub-parser default would otherwise overwrite it).
    serve_p = sub.add_parser("serve", help="build the index and serve it (default)")
    serve_p.add_argument("--config", default=argparse.SUPPRESS,
                         help="path to config.yaml (gitignored; see config.example.yaml)")
    serve_p.add_argument("--host", default=argparse.SUPPRESS,
                         help="override bind host (default from config)")
    serve_p.add_argument("--port", type=int, default=argparse.SUPPRESS,
                         help="override port (default from config)")
    index_p = sub.add_parser(
        "index",
        help="build the index and print a machine-readable JSON dump to stdout",
    )
    index_p.add_argument("--config", default=argparse.SUPPRESS,
                         help="path to config.yaml (gitignored; see config.example.yaml)")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    if args.command == "index":
        print(_dump(build_index(config)))
        return 0

    console_cfg = config.get("console", {})
    host = args.host or console_cfg.get("bind", "127.0.0.1")
    port = args.port or int(console_cfg.get("port", 5180))

    index = build_index(config)
    server = serve(index, host=host, port=port)
    reach = index.reachability()
    print(f"nousergon-console serving on http://{host}:{port} — "
          f"{reach['total']} entities indexed, "
          f"reachability {reach['reachable_all_three']}/{reach['total']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _dump(index: Index) -> str:
    """The machine-readable projection — the SAME serializer the HTTP JSON
    representation uses (`render/json.py`).

    Deliberately shared: a CLI dump with its own entity shape is a second wire
    format, and the two would answer the same question differently the first
    time either changed.
    """
    return json.dumps(index_dump(index), indent=2, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
