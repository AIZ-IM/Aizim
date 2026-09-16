"""Compare deterministic synthetic retrieval with a repository revision.

Run with the project's Python environment, e.g.:
  python scripts/benchmark_research_search.py --baseline-ref 62b52bc --size 5000
This measures retrieval only, not model inference or mathematical proving.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

from aizim.research.search import SearchCorpus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="62b52bc")
    parser.add_argument("--size", type=int, default=5000)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if not 10 <= args.size <= 100_000 or not 1 <= args.repeat <= 10:
        parser.error("Use size 10-100000 and repeat 1-10")
    root = Path(__file__).resolve().parents[1]
    source = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:src/aizim/research/search.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    baseline = {"__package__": "aizim.research", "__name__": "aizim.research.benchmark_baseline"}
    exec(compile(source, "<repository-baseline>", "exec"), baseline)
    documents = [
        {
            "name": f"lemma_{i}",
            "signature": "(n : Nat) : n + 0 = n",
            "docstring": f"Addition identity for natural numbers, example {i}.",
        }
        for i in range(args.size)
    ]
    query = "addition identity natural"
    start = time.perf_counter()
    corpus = SearchCorpus(documents)
    construction = time.perf_counter() - start
    old_times, new_times = [], []
    for _ in range(args.repeat):
        start = time.perf_counter()
        before = baseline["ranked"](query, documents, 10)
        old_times.append(time.perf_counter() - start)
        start = time.perf_counter()
        after = corpus.search(query, 10)
        new_times.append(time.perf_counter() - start)
        assert [item["name"] for item in before] == [item["name"] for item in after]
    old, new = statistics.median(old_times), statistics.median(new_times)
    print(
        json.dumps(
            {
                "scope": "synthetic ranking, not theorem proving",
                "documents": args.size,
                "repeats": args.repeat,
                "baseline_ref": args.baseline_ref,
                "baseline_median_seconds": round(old, 6),
                "index_build_seconds": round(construction, 6),
                "cached_query_median_seconds": round(new, 6),
                "first_query_seconds": round(construction + new, 6),
                "cached_query_speedup": round(old / new, 2),
                "top_10_unchanged": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
