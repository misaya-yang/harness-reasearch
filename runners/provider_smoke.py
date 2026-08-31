"""Run one minimal Responses API smoke request without persisting credentials."""

from __future__ import annotations

import argparse
import json

from .client import ResponsesClient, ResponsesError
from .config import ConfigError, load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/experiment.default.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        provider, _ = load_config(args.config)
        client = ResponsesClient(provider, dry_run=args.dry_run)
        result = client.complete(
            'Return exactly this JSON object and no markdown: {"smoke_test":"ok"}',
            metadata={"experiment": "provider_smoke"},
        )
        if not args.dry_run:
            if result.returned_model != provider.model:
                raise ResponsesError(
                    "provider response model does not match the configured model"
                )
            try:
                smoke_payload = json.loads(result.output_text)
            except json.JSONDecodeError as exc:
                raise ResponsesError("smoke response was not valid JSON") from exc
            if smoke_payload != {"smoke_test": "ok"}:
                raise ResponsesError("smoke response did not match the requested object")
    except (ConfigError, ResponsesError) as exc:
        parser.error(str(exc))
    print(f"provider={provider.name}")
    print(f"model={provider.model}")
    print(f"endpoint={provider.responses_url}")
    print(f"dry_run={args.dry_run}")
    if not args.dry_run:
        print(f"returned_model={result.returned_model}")
        print(f"latency_ms={result.latency_ms:.1f}")
        print(f"usage={result.usage}")
        print(f"output={result.output_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
