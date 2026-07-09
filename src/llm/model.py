from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


JsonObject = dict[str, Any]
DEFAULT_OPENROUTER_MODEL = "minimax/minimax-m3"
DEFAULT_OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelRequest:
    messages: list[ChatMessage]
    temperature: float = 0.0
    response_schema: JsonObject | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    content: str
    model: str | None = None
    usage: JsonObject | None = None
    raw: Any = None


class LLMClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one model completion for a provider-neutral chat request."""


class ModelNotConfiguredError(RuntimeError):
    pass


class OpenRouterApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


@dataclass(frozen=True)
class OpenRouterClient:
    model: str = DEFAULT_OPENROUTER_MODEL
    api_key: str | None = None
    api_url: str = DEFAULT_OPENROUTER_API_URL
    timeout: float = 60.0
    max_tokens: int | None = None
    site_url: str | None = None
    app_name: str | None = None
    use_response_schema: bool = True

    def resolved_api_key(self) -> str:
        api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ModelNotConfiguredError(
                "OpenRouter API key not configured. Set OPENROUTER_API_KEY "
                "or pass --openrouter-api-key."
            )
        return api_key

    def payload_for_request(self, request: ModelRequest) -> JsonObject:
        payload: JsonObject = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": request.temperature,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if request.response_schema is not None and self.use_response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "commit_classification",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        return payload

    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.resolved_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "vd-llm-classification/1.0",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        return headers

    def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self.payload_for_request(request)
        http_request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OpenRouterApiError(
                f"OpenRouter API error {exc.code}: {body}",
                status_code=exc.code,
                body=body,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OpenRouterApiError(f"OpenRouter API request failed: {exc}") from exc

        raw = json.loads(body)
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not isinstance(choices, list) or not choices:
            raise OpenRouterApiError(f"OpenRouter API response did not include choices: {body}")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise OpenRouterApiError(f"OpenRouter API response choice was invalid: {body}")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise OpenRouterApiError(f"OpenRouter API response did not include a message: {body}")
        content = _message_content_to_text(message.get("content")).strip()
        if not content:
            raise OpenRouterApiError(f"OpenRouter API response message was empty: {body}")
        return ModelResponse(
            content=content,
            model=(raw.get("model") or self.model) if isinstance(raw, dict) else self.model,
            usage=raw.get("usage") if isinstance(raw, dict) else None,
            raw=raw,
        )
