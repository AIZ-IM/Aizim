from __future__ import annotations

import json

from aizim.orchestration.runner import _fixture_root


def test_deterministic_worker_fixtures_are_package_owned() -> None:
    root = _fixture_root()

    assert root.parent.parent.name == "aizim"
    for name in ("prover_a.json", "prover_b.json"):
        document = json.loads((root / name).read_text())
        assert document["rounds"]
        assert all(
            action["operation"]
            for round_actions in document["rounds"]
            for action in round_actions
        )
