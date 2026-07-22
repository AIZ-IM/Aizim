from __future__ import annotations

import os
from pathlib import Path
from tempfile import mkdtemp

_DARWIN_SOCKET_PATH_MAX = 103


class SocketAliasError(RuntimeError):
    pass


class ProjectSocketAlias:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path):
            raise SocketAliasError("INVALID_PROJECT_ROOT")
        try:
            canonical = project_root.resolve(strict=True)
        except OSError as error:
            raise SocketAliasError("PROJECT_ROOT_UNAVAILABLE") from error
        if not canonical.is_dir():
            raise SocketAliasError("PROJECT_ROOT_UNAVAILABLE")
        root = Path(mkdtemp(prefix="aizim-gw-", dir="/private/tmp"))
        link = root / "project"
        try:
            link.symlink_to(canonical, target_is_directory=True)
            socket_path = link / ".aizim" / "run" / "gateway.sock"
            if len(os.fsencode(socket_path)) > _DARWIN_SOCKET_PATH_MAX:
                raise SocketAliasError("SOCKET_ALIAS_TOO_LONG")
        except BaseException:
            link.unlink(missing_ok=True)
            root.rmdir()
            raise
        self._root, self._link, self.socket_path = root, link, socket_path

    def close(self) -> None:
        self._link.unlink(missing_ok=True)
        self._root.rmdir()
