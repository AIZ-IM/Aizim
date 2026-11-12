from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import yaml

from .executable import OMP_VERSION, OmpExecutableError

__all__ = ["OMP_VERSION", "check_magpie", "stage_runtime"]
_MAX_CONFIG_BYTES = 4 * 1024 * 1024


def _load(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError
        value = yaml.safe_load(raw)
        if type(value) is not dict:
            raise ValueError
        return value
    except (OSError, ValueError, yaml.YAMLError):
        raise OmpExecutableError("OMP_MAGPIE_CONFIG_UNAVAILABLE") from None


def stage_runtime(scratch: Path, environment: Mapping[str, str]) -> tuple[dict[str, str], str]:
    """Copy declarative magpie model settings; never copy auth or ambient configuration."""
    home = Path(environment.get("HOME", str(Path.home())))
    source = Path(environment.get("PI_CODING_AGENT_DIR", str(home / ".omp/agent")))
    config = _load(source / "config.yml")
    catalog = _load(source / "models.yml")
    try:
        selector = config["modelRoles"]["default"]
        provider = catalog["providers"]["magpie"]
        endpoint = provider["baseUrl"]
        api = provider["api"]
        if (type(selector) is not str or not selector.startswith("magpie/")
                or type(endpoint) is not str
                or api not in {"openai-completions", "openai-responses"}
                or any(char in selector or char in endpoint for char in "$`{}\n\r")):
            raise ValueError
        url = urlsplit(endpoint)
        host = url.hostname
        if (url.scheme not in {"http", "https"} or not host or url.username or url.password
                or url.query or url.fragment
                or not (host == "localhost" or ipaddress.ip_address(host).is_loopback)):
            raise ValueError
        model_id = selector.removeprefix("magpie/")
        candidates = [model for model in provider["models"] if type(model) is dict
                      and type(model.get("id")) is str
                      and (model_id == model["id"] or model_id.startswith(model["id"] + ":"))]
        model = max(candidates, key=lambda item: len(item["id"]))
        # Primitive fields cannot expand environment variables, run commands, or load modules.
        descriptor = {"id": model["id"], "name": model["id"]}
        for key in ("contextWindow", "maxTokens"):
            if type(model.get(key)) is int and model[key] > 0:
                descriptor[key] = model[key]
        if type(model.get("reasoning")) is bool:
            descriptor["reasoning"] = model["reasoning"]
        if type(model.get("input")) is list and all(
            item in {"text", "image"} for item in model["input"]
        ):
            descriptor["input"] = model["input"]
    except (KeyError, TypeError, ValueError):
        raise OmpExecutableError("OMP_MAGPIE_CONFIG_INVALID") from None
    runtime = scratch / "omp"
    agent = runtime / "agent"
    for directory in (runtime / "home", agent, runtime / "natives", runtime / "sessions"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = {
        "modelRoles": {"default": selector},
        "tools": {"xdev": False},
        "advisor": {"enabled": False},
        "memory": {"enabled": False},
        "providers": {"cacheWarming": "off"},
    }
    providers = {"providers": {"magpie": {
        "baseUrl": endpoint, "api": api, "auth": "none", "models": [descriptor],
    }}}
    for name, document in (("config.yml", settings), ("models.yml", providers)):
        target = agent / name
        target.write_text(json.dumps(document), encoding="utf-8")
        target.chmod(0o600)
    child_environment = {
        "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(runtime / "home"), "TMPDIR": str(scratch),
        "PI_CODING_AGENT_DIR": str(agent), "PI_NATIVES_DIR": str(runtime / "natives"),
    }
    return child_environment, endpoint


def check_magpie(endpoint: str) -> None:
    """Require a responding unauthenticated local model gateway, without inference."""
    try:
        opener = build_opener(ProxyHandler({}))
        request = Request(endpoint.rstrip("/") + "/models", headers={"Accept": "application/json"})
        with opener.open(request, timeout=10) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        document = json.loads(raw)
        if type(document) is not dict or type(document.get("data")) is not list:
            raise ValueError
    except Exception:
        raise OmpExecutableError("MAGPIE_GATEWAY_UNAVAILABLE_OR_AUTH_REQUIRED") from None
