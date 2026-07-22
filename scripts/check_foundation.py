from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Final

from aizim.foundation_checks import evaluate
from aizim.foundation_evidence import fail, load_evidence

_SUCCESS: Final = "FOUNDATION ACCEPTANCE PASS 16/16"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        evidence = asyncio.run(load_evidence(arguments.project, arguments.run_id))
        checks = evaluate(evidence)
        if len(checks) != 16 or len({name for name, _passed in checks}) != 16:
            fail("ACCEPTANCE_RECORDS_INVALID")
        if not all(passed for _name, passed in checks):
            fail("EVIDENCE_FAILED")
    except Exception:
        print("FOUNDATION ACCEPTANCE FAIL", file=sys.stderr)
        return 1
    print(_SUCCESS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
