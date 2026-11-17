#!/usr/bin/env python3
"""BaoTa launcher for the application account/queue server."""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("/opt/sorvflow/app"))
    parser.add_argument("--port-start", type=int, default=8765)
    parser.add_argument("--port-end", type=int, default=8815)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/run/sorvflow"))
    args = parser.parse_args()
    os.chdir(args.app)
    sys.path.insert(0, str(args.app.resolve()))
    from core.dotenv import load_dotenv

    load_dotenv()
    from web.runtime import run

    run(
        host="127.0.0.1", port=args.port_start, port_end=args.port_end, runtime_dir=args.runtime_dir
    )


if __name__ == "__main__":
    main()
