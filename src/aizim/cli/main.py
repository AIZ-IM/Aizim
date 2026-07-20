from __future__ import annotations

import argparse

from .. import __version__


def main() -> int:
    parser = argparse.ArgumentParser(prog="aizim")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()
    return 0
