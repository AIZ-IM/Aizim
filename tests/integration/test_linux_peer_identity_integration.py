from __future__ import annotations

import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from aizim.domain import AgentRole
from aizim.gateway import (
    BrokerDependencies,
    BrokerRegistration,
    GatewaySessionBroker,
    current_process_image_sha256,
    redeem_session,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux peer-credential identity is Linux-only",
)


@pytest.mark.asyncio
async def test_default_linux_identity_redeems_over_private_socket() -> None:
    now = datetime(2026, 7, 23, 10, tzinfo=UTC)
    image_hash = current_process_image_sha256()
    with TemporaryDirectory(prefix="aizim-", dir="/tmp") as directory:
        broker = GatewaySessionBroker(
            Path(directory) / "gateway.sock",
            dependencies=BrokerDependencies(
                clock=lambda: now,
                session_ids=lambda: "linux-session",
            ),
        )
        session_id = broker.register(
            BrokerRegistration(
                run_id="run-linux",
                worker_id="worker-linux",
                role=AgentRole.FORMALIZER,
                sidecar_executable_sha256=image_hash,
                expires_at=now + timedelta(minutes=5),
                raw_token="linux-secret-token",
            )
        )

        await broker.start()
        try:
            redeemed = await redeem_session(broker.socket_path, session_id)
            mode = stat.S_IMODE(broker.socket_path.stat().st_mode)
        finally:
            await broker.aclose()

    assert mode == 0o600
    assert redeemed.raw_token == "linux-secret-token"
    assert redeemed.run_id == "run-linux"
    assert redeemed.worker_id == "worker-linux"
    assert redeemed.role is AgentRole.FORMALIZER
    assert "linux-secret-token" not in repr(redeemed)
    assert not broker.socket_path.exists()
