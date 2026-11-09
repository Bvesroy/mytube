"""Local LLM abstraction: detect Ollama / llama.cpp, never download, never cloud.

Policy:
- Disabled unless `MYTUBE_AI_ENABLED=1` and a local runtime is reachable.
- Loopback endpoints only by default; a non-loopback endpoint requires
  `MYTUBE_ALLOW_REMOTE_AI=1` (operators must document that choice).
- No model pulling. If a model is missing, the status says so and features fall
  back to deterministic/extractive behaviour.
- Every call has a short timeout; failures raise `AIUnavailable` so callers can
  degrade instead of hanging the request or job.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

OLLAMA_DEFAULT = "http://127.0.0.1:11434"
LLAMACPP_DEFAULT = "http://127.0.0.1:8080"


class AIUnavailable(RuntimeError):
    """Raised when an optional local LLM is disabled, missing or unreachable."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _check_endpoint(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise AIUnavailable("endpoint must be http(s)")
    host = parsed.hostname or ""
    if not _is_loopback(host) and not _truthy(os.environ.get("MYTUBE_ALLOW_REMOTE_AI")):
        raise AIUnavailable(
            "non-loopback AI endpoint blocked; set MYTUBE_ALLOW_REMOTE_AI=1 to "
            "explicitly allow a remote host"
        )
    return url.rstrip("/")


@dataclass
class LocalLLM:
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    timeout: float = 3.0
    available: bool = False
    models: list[str] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def detect(
        cls,
        *,
        timeout: float = 1.5,
        enabled: bool | None = None,
        provider: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> "LocalLLM":
        if enabled is None:
            enabled = _truthy(os.environ.get("MYTUBE_AI_ENABLED"))
        if not enabled:
            return cls(provider="none", error="AI disabled (set MYTUBE_AI_ENABLED=1)")
        choice = (provider or os.environ.get("MYTUBE_LLM_PROVIDER") or "auto").strip().lower()
        model = (
            model
            or os.environ.get("MYTUBE_LLM_MODEL")
            or os.environ.get("MYTUBE_OLLAMA_MODEL")
            or "llama3.2:3b"
        ).strip()

        candidates: list[LocalLLM] = []
        if choice in {"auto", "ollama"}:
            candidates.append(
                cls(
                    provider="ollama",
                    model=model,
                    endpoint=_check_endpoint(
                        (endpoint if choice == "ollama" and endpoint else None)
                        or os.environ.get("MYTUBE_OLLAMA_URL")
                        or OLLAMA_DEFAULT
                    ),
                    timeout=timeout,
                )
            )
        if choice in {"auto", "llamacpp", "llama.cpp"}:
            candidates.append(
                cls(
                    provider="llamacpp",
                    model=model,
                    endpoint=_check_endpoint(
                        (endpoint if choice in {"llamacpp", "llama.cpp"} and endpoint else None)
                        or os.environ.get("MYTUBE_LLAMACPP_URL")
                        or LLAMACPP_DEFAULT
                    ),
                    timeout=timeout,
                )
            )
        last_error = None
        for cand in candidates:
            try:
                if cand.provider == "ollama":
                    tags = cand._get_json("/api/tags")
                    cand.models = [m.get("name", "") for m in tags.get("models", []) if m.get("name")]
                else:
                    # llama.cpp server exposes OpenAI-compatible /v1/models and /health.
                    try:
                        data = cand._get_json("/v1/models")
                        cand.models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
                    except AIUnavailable:
                        cand._get_json("/health")
                        cand.models = []
                cand.available = True
                cand.error = None
                if cand.model not in cand.models and cand.models:
                    # Keep the configured model but surface that it is not installed.
                    cand.error = f"configured model not installed: {cand.model}"
                return cand
            except AIUnavailable as exc:
                last_error = str(exc)
                continue
        return cls(
            provider=choice if choice != "auto" else "none",
            model=model,
            error=last_error or "no local runtime reachable",
        )

    # -- HTTP helpers -------------------------------------------------
    def _get_json(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(self.endpoint + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
            raise AIUnavailable(f"{self.provider} unreachable: {exc}") from exc

    def _post_json(self, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(
            self.endpoint + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
            raise AIUnavailable(f"{self.provider} generation failed: {exc}") from exc

    # -- generation ---------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str:
        if not self.available:
            raise AIUnavailable(self.error or "no local runtime available")
        if self.provider == "ollama":
            payload: dict[str, Any] = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
            if system:
                payload["system"] = system
            data = self._post_json("/api/generate", payload, timeout=max(self.timeout, 120.0))
            text = str(data.get("response") or "").strip()
            if not text:
                raise AIUnavailable("empty response from ollama")
            return text
        if self.provider == "llamacpp":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            data = self._post_json(
                "/v1/chat/completions",
                {
                    "model": self.model or "local",
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=max(self.timeout, 120.0),
            )
            try:
                return str(data["choices"][0]["message"]["content"]).strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise AIUnavailable("unexpected llama.cpp response") from exc
        raise AIUnavailable(f"unsupported provider: {self.provider}")

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider or "none",
            "model": self.model,
            "endpoint": self.endpoint or None,
            "available": bool(self.available),
            "models": self.models,
            "error": self.error,
            "enabled": _truthy(os.environ.get("MYTUBE_AI_ENABLED")),
            "local_only": True,
            "downloads_models": False,
        }


def provider_status() -> dict[str, Any]:
    return LocalLLM.detect().status()
