"""Restricted omp RPC execution and trusted host-tool transport."""

from .executable import OMP_VERSION, OmpExecutableError, omp_version, resolve_omp
from .rpc import RpcClient, RpcError

__all__ = [
    "OMP_VERSION", "OmpExecutableError", "RpcClient", "RpcError", "omp_version", "resolve_omp",
]
