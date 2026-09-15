from __future__ import annotations

from aizim.research.records import ResearchError, operator_change

from .operations import RpcRequest, RpcResponse, RpcSuccess, StateOperations, rpc_failure


def dispatch_research(target: StateOperations, request: RpcRequest) -> RpcResponse:
    try:
        version = operator_change(target, request.params)
    except ResearchError as error:
        return rpc_failure(str(error), "research request rejected")
    return RpcSuccess({"version": version})
