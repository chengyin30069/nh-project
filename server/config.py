"""YAML configuration loading for the server and Docker deployment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


CONFIG_KEYS = {
    "auth": {"cookie", "user_agent"},
    "server": {
        "host",
        "port",
        "library_host",
        "library_port",
        "base_path",
        "allowed_networks",
        "trusted_proxies",
    },
    "paths": {"storage", "download_script", "legacy_cookie_file"},
    "download": {"max_retry", "media_servers", "parallel"},
    "cache": {
        "html_ttl_seconds",
        "api_ttl_seconds",
        "html_max_age_seconds",
        "html_max_bytes",
        "extract_max_bytes",
        "preview_max_age_seconds",
        "preview_max_bytes",
        "sweep_interval_seconds",
    },
}

ENV_FIELDS = {
    ("auth", "cookie"): "NH_COOKIE",
    ("auth", "user_agent"): "NH_USER_AGENT",
    ("server", "host"): "NH_SERVER_HOST",
    ("server", "port"): "NH_SERVER_PORT",
    ("server", "library_host"): "NH_LIBRARY_HOST",
    ("server", "library_port"): "NH_LIBRARY_PORT",
    ("server", "base_path"): "NH_BASE_PATH",
    ("paths", "storage"): "NH_FOLDER_PATH",
    ("paths", "download_script"): "NH_DOWNLOAD_SCRIPT",
    ("paths", "legacy_cookie_file"): "NH_COOKIE_FILE",
    ("download", "max_retry"): "NH_MAX_RETRY",
    ("download", "media_servers"): "NH_MEDIA_SERVER_LIST",
    ("download", "parallel"): "NH_PARALLEL",
    ("cache", "html_ttl_seconds"): "NH_HTML_CACHE_TTL_SECONDS",
    ("cache", "api_ttl_seconds"): "NH_API_CACHE_TTL_SECONDS",
    ("cache", "html_max_age_seconds"): "NH_HTML_CACHE_MAX_AGE_SECONDS",
    ("cache", "html_max_bytes"): "NH_HTML_CACHE_MAX_BYTES",
    ("cache", "extract_max_bytes"): "NH_EXTRACT_CACHE_MAX_BYTES",
    ("cache", "preview_max_age_seconds"): "NH_PREVIEW_CACHE_MAX_AGE_SECONDS",
    ("cache", "preview_max_bytes"): "NH_PREVIEW_CACHE_MAX_BYTES",
    ("cache", "sweep_interval_seconds"): "NH_CACHE_SWEEP_INTERVAL_SECONDS",
}


def find_config_path(explicit: str | None, project_root: Path) -> tuple[Path | None, bool]:
    """Return the selected YAML path and whether its presence is mandatory."""

    value = explicit or os.environ.get("NH_CONFIG_FILE")
    if value:
        return Path(value).expanduser().resolve(), True
    default = project_root / "config.yaml"
    return (default, False) if default.exists() else (None, False)


def load_yaml_config(path: Path | None, *, required: bool = False) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        if required:
            raise ValueError(f"configuration file not found: {path}")
        return {}
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "YAML configuration requires PyYAML (install requirements-server.txt or the py3-yaml package)"
        ) from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("YAML configuration root must be a mapping")
    unknown_sections = set(value) - set(CONFIG_KEYS)
    if unknown_sections:
        raise ValueError(f"unknown YAML configuration sections: {', '.join(sorted(unknown_sections))}")
    for section, allowed in CONFIG_KEYS.items():
        section_value = value.get(section, {})
        if not isinstance(section_value, dict):
            raise ValueError(f"YAML section {section!r} must be a mapping")
        unknown = set(section_value) - allowed
        if unknown:
            raise ValueError(f"unknown keys in YAML section {section!r}: {', '.join(sorted(unknown))}")
    _validate_config(value)
    return value


def config_environment(config: dict[str, Any], config_path: Path | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for keys, env_name in ENV_FIELDS.items():
        value = config.get(keys[0], {}).get(keys[1])
        if value is None:
            continue
        if keys == ("download", "media_servers"):
            value = " ".join(str(item) for item in value)
        if keys[0] == "paths":
            value = _resolve_config_path(str(value), config_path)
        env[env_name] = str(value)
    networks = config.get("server", {}).get("allowed_networks")
    if networks is not None:
        env["NH_ALLOWED_NETWORKS"] = ",".join(str(item) for item in networks)
    proxies = config.get("server", {}).get("trusted_proxies")
    if proxies is not None:
        env["NH_TRUSTED_PROXIES"] = ",".join(str(item) for item in proxies)
    return env


def _resolve_config_path(value: str, config_path: Path | None) -> str:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute() and config_path is not None:
        expanded = config_path.parent / expanded
    return str(expanded.resolve())


def _validate_config(config: dict[str, Any]) -> None:
    auth = config.get("auth", {})
    for name in ("cookie", "user_agent"):
        if name in auth and not isinstance(auth[name], str):
            raise ValueError(f"auth.{name} must be a string")
    server = config.get("server", {})
    for name in ("port", "library_port"):
        if name in server and (not isinstance(server[name], int) or isinstance(server[name], bool) or not 1 <= server[name] <= 65535):
            raise ValueError(f"server.{name} must be an integer from 1 through 65535")
    base_path = server.get("base_path")
    if base_path is not None and not isinstance(base_path, str):
        raise ValueError("server.base_path must be a string")
    for name in ("allowed_networks", "trusted_proxies"):
        networks = server.get(name)
        if networks is not None and (
            not isinstance(networks, list) or not all(isinstance(item, str) for item in networks)
        ):
            raise ValueError(f"server.{name} must be a list of CIDR strings")
    media_servers = config.get("download", {}).get("media_servers")
    if media_servers is not None and (
        not isinstance(media_servers, list)
        or not media_servers
        or not all(isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 9 for item in media_servers)
    ):
        raise ValueError("download.media_servers must be a non-empty list containing integers 1 through 9")
    for section in ("download", "cache"):
        for name, value in config.get(section, {}).items():
            if name == "media_servers":
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{section}.{name} must be a positive integer")
