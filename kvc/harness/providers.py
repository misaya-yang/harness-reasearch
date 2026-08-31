"""Frozen custom-provider catalogs for KVC runs (written to agent-dir/models.json).

Keys are referenced by env interpolation ("$KVC_API_KEY") so a literal key is
never persisted. The compat block was calibrated in M0 smoke A against the
Aliyun Singapore MaaS gateway with qwen3.8-flash:

* gateway rejects role "developer"          -> supportsDeveloperRole: false
* gateway wants classic max_tokens          -> maxTokensField: max_tokens
* request noise reduction                   -> supportsStore: false
* thinking control per plan (thinking off)  -> thinkingFormat: qwen
  (top-level enable_thinking: boolean, verified in the captured request body)
"""

from __future__ import annotations

from typing import Any

DASHSCOPE_BASE_URL = (
    "https://ws-smqn3wel83c2p9wd.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
)
QWEN_FLASH_ID = "qwen3.8-flash"
KEY_ENV_NAME = "KVC_API_KEY"


def dashscope_models_json(
    model_id: str = QWEN_FLASH_ID,
    key_env: str = KEY_ENV_NAME,
    base_url: str = DASHSCOPE_BASE_URL,
) -> dict[str, Any]:
    return {
        "providers": {
            "dashscope-intl": {
                "name": "Aliyun MaaS Singapore",
                "baseUrl": base_url,
                "apiKey": f"${key_env}",
                "api": "openai-completions",
                "models": [
                    {
                        "id": model_id,
                        "name": "Qwen3.8 Flash",
                        "reasoning": True,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 131072,
                        "maxTokens": 16384,
                        "compat": {
                            "thinkingFormat": "qwen",
                            "supportsDeveloperRole": False,
                            "supportsStore": False,
                            "maxTokensField": "max_tokens",
                        },
                    }
                ],
            }
        }
    }
