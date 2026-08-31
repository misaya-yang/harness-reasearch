"""Configuration loading and safe provider endpoint handling."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ConfigError(ValueError):
    """Raised when a required experiment configuration value is invalid."""


@dataclass(frozen=True)
class ProviderConfig:
    """Provider settings used by a live or dry-run Responses request."""

    name: str
    model: str
    api_key_env: str
    responses_url: str
    timeout_seconds: float = 120.0
    max_output_tokens: int = 800
    temperature: float | None = 0.0
    reasoning_effort: str | None = "none"


@dataclass(frozen=True)
class ExperimentConfig:
    """Experiment paths and condition settings."""

    dataset: Path
    trace_path: Path
    result_path: Path
    conditions: tuple[str, ...]
    replicates: int = 1
    max_tasks: int | None = None


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def responses_endpoint_from_url(url: str) -> str:
    """Return an OpenAI Responses endpoint from a base or supplied app URL.

    The transformation is intentionally narrow: the supplied Alibaba
    ``/apps/anthropic`` path maps to the workspace's OpenAI-compatible
    ``/compatible-mode/v1/responses`` path. A URL already ending in
    ``/responses`` is returned unchanged.
    """

    parsed = urlsplit(_require_string(url, "responses URL"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("responses URL must be an absolute HTTP(S) URL")

    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        response_path = path
    elif path.endswith("/apps/anthropic"):
        response_path = f'{path.removesuffix("/apps/anthropic")}/compatible-mode/v1/responses'
    elif path.endswith("/compatible-mode/v1"):
        response_path = f"{path}/responses"
    else:
        response_path = f"{path}/responses"
    return urlunsplit((parsed.scheme, parsed.netloc, response_path, parsed.query, ""))


def _resolve_path(config_path: Path, value: object, name: str) -> Path:
    raw = Path(_require_string(value, name))
    if raw.is_absolute():
        return raw
    return (config_path.parent.parent / raw).resolve()


def load_config(path: str | Path) -> tuple[ProviderConfig, ExperimentConfig]:
    """Load a JSON configuration without reading the API key."""

    config_path = Path(path).expanduser().resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be an object")

    provider_raw = raw.get("provider")
    experiment_raw = raw.get("experiment")
    if not isinstance(provider_raw, dict) or not isinstance(experiment_raw, dict):
        raise ConfigError("configuration must contain provider and experiment objects")

    model = _require_string(provider_raw.get("model"), "provider.model")
    response_url = responses_endpoint_from_url(
        _require_string(provider_raw.get("responses_url"), "provider.responses_url")
    )
    timeout = float(provider_raw.get("timeout_seconds", 120))
    max_output_tokens = int(provider_raw.get("max_output_tokens", 800))
    temperature_raw = provider_raw.get("temperature", 0.0)
    temperature = None if temperature_raw is None else float(temperature_raw)
    reasoning_effort_raw = provider_raw.get("reasoning_effort", "none")
    reasoning_effort = None if reasoning_effort_raw is None else _require_string(reasoning_effort_raw, "provider.reasoning_effort")
    if reasoning_effort not in {None, "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ConfigError("provider.reasoning_effort is not a supported Responses effort")
    if timeout <= 0 or max_output_tokens <= 0:
        raise ConfigError("provider timeout and max_output_tokens must be positive")

    conditions_raw = experiment_raw.get("conditions", [])
    if not isinstance(conditions_raw, list) or not all(isinstance(item, str) for item in conditions_raw):
        raise ConfigError("experiment.conditions must be a list of strings")
    conditions = tuple(item.strip() for item in conditions_raw if item.strip())
    if not conditions:
        raise ConfigError("experiment.conditions must not be empty")

    replicates = int(experiment_raw.get("replicates", 1))
    max_tasks_raw = experiment_raw.get("max_tasks")
    max_tasks = None if max_tasks_raw is None else int(max_tasks_raw)
    if replicates <= 0 or (max_tasks is not None and max_tasks <= 0):
        raise ConfigError("replicates and max_tasks must be positive when provided")

    provider = ProviderConfig(
        name=_require_string(provider_raw.get("name"), "provider.name"),
        model=model,
        api_key_env=_require_string(provider_raw.get("api_key_env"), "provider.api_key_env"),
        responses_url=response_url,
        timeout_seconds=timeout,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )
    experiment = ExperimentConfig(
        dataset=_resolve_path(config_path, experiment_raw.get("dataset"), "experiment.dataset"),
        trace_path=_resolve_path(config_path, experiment_raw.get("trace_path"), "experiment.trace_path"),
        result_path=_resolve_path(config_path, experiment_raw.get("result_path"), "experiment.result_path"),
        conditions=conditions,
        replicates=replicates,
        max_tasks=max_tasks,
    )
    return provider, experiment


def read_api_key(provider: ProviderConfig) -> str:
    """Read a key only for a live request and never expose its value."""

    value = os.environ.get(provider.api_key_env, "").strip()
    if not value:
        raise ConfigError(
            f"missing API key in environment variable {provider.api_key_env}; "
            "use --dry-run for a no-key check"
        )
    return value
