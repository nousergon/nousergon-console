"""`python -m console` — build the index from config and serve it.

A clean checkout runs the console with this and nothing else (no build step,
no database — docs/stack-decision.md). The index is built from the enabled
adapters at start and served read-only; a restart re-derives it, which is the
whole of §5.6's "renders, never owns" made operational.
"""
from __future__ import annotations

import argparse

from .config import build_index, load_config
from .server.app import serve


def main() -> int:
    ap = argparse.ArgumentParser(prog="console", description=__doc__)
    ap.add_argument("--config", default="config.yaml",
                    help="path to config.yaml (gitignored; see config.example.yaml)")
    ap.add_argument("--host", default=None, help="override bind host (default from config)")
    ap.add_argument("--port", type=int, default=None, help="override port (default from config)")
    args = ap.parse_args()

    config = load_config(args.config)
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


if __name__ == "__main__":
    raise SystemExit(main())
