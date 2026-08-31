"""Minimal dependency-free client for an OpenAI-compatible Responses endpoint."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ProviderConfig, read_api_key


class ResponsesError(RuntimeError):
    """Raised for a provider transport or response-shape error."""


@dataclass(frozen=True)
class ResponseResult:
    """Sanitized result returned to experiment code."""

    output_text: str
    usage: dict[str, Any]
    request_id: str | None
    returned_model: str | None
    raw: dict[str, Any]
    latency_ms: float


def _read_json(response: HTTPResponse) -> dict[str, Any]:
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResponsesError("provider returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise ResponsesError("provider response must be a JSON object")
    return payload


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct

    pieces: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        pieces.append(part["text"])
            elif isinstance(item.get("text"), str):
                pieces.append(item["text"])
    if pieces:
        return "\n".join(pieces)

    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


class ResponsesClient:
    """Small client with no SDK dependency and no secret persistence."""

    def __init__(self, provider: ProviderConfig, *, dry_run: bool = False) -> None:
        self.provider = provider
        self.dry_run = dry_run
        self._api_key = None if dry_run else read_api_key(provider)

    def complete(
        self,
        prompt: str,
        *,
        instructions: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ResponseResult:
        payload: dict[str, Any] = {
            "model": self.provider.model,
            "input": prompt,
            "max_output_tokens": self.provider.max_output_tokens,
            "store": False,
        }
        if instructions:
            payload["instructions"] = instructions
        if self.provider.temperature is not None:
            payload["temperature"] = self.provider.temperature
        if self.provider.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.provider.reasoning_effort}

        if self.dry_run:
            return ResponseResult(
                output_text="",
                usage={},
                request_id=None,
                returned_model=self.provider.model,
                raw={"dry_run": True, "request": payload},
                latency_ms=0.0,
            )

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.provider.responses_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.provider.timeout_seconds) as response:
                payload_response = _read_json(response)
                request_id = response.headers.get("x-request-id")
        except HTTPError as exc:
            raise ResponsesError(f"provider request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise ResponsesError(f"provider request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ResponsesError("provider request timed out") from exc

        usage = payload_response.get("usage")
        normalized_usage = usage if isinstance(usage, dict) else {}
        returned_model = (
            payload_response.get("model")
            if isinstance(payload_response.get("model"), str)
            else None
        )
        if returned_model is not None and returned_model != self.provider.model:
            raise ResponsesError("provider response model does not match configured model")
        return ResponseResult(
            output_text=_extract_output_text(payload_response),
            usage=normalized_usage,
            request_id=request_id,
            returned_model=returned_model,
            raw=payload_response,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
