"""OpenAI 兼容端点的最小客户端。

只依赖 httpx，不引入官方 SDK：
- 端点地址由 base_url 归一化得到，兼容「以 /v1 结尾」与「完整 /chat/completions」两种写法。
- 流式解析 SSE，逐块回调，便于把长文生成进度实时写进数据库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx


class LLMError(RuntimeError):
    """模型调用失败，携带对用户可读的说明。"""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 8192
    timeout: float = 900.0

    def chat_endpoint(self) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            raise LLMError("未配置模型接口地址（base_url）")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def validate(self) -> None:
        if not self.api_key:
            raise LLMError("未配置 API Key，请到「设置」页填写")
        if not self.model:
            raise LLMError("未配置模型名称，请到「设置」页填写")


def _headers(config: LLMConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def _payload(config: LLMConfig, messages: list[dict[str, str]], *, stream: bool) -> dict:
    return {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": stream,
    }


def _iter_sse_deltas(lines: Iterable[bytes | str]) -> Iterable[str]:
    """从 SSE 行流里提取 delta 文本，忽略 keep-alive 与结束标记。

    httpx 的 iter_lines() 给出的是 str（已解码），但不同版本/传输层也可能给 bytes，
    所以两种都要接受——早期版本这里只处理 bytes，导致真实流式一上来就 AttributeError。
    """
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = payload.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
        if isinstance(text, str) and text:
            yield text


def chat_stream(
    config: LLMConfig,
    messages: list[dict[str, str]],
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """流式对话，返回完整文本。on_delta 会在每个增量到达时被调用。"""
    config.validate()
    collected: list[str] = []
    try:
        with httpx.Client(timeout=config.timeout) as client:
            with client.stream(
                "POST",
                config.chat_endpoint(),
                headers=_headers(config),
                json=_payload(config, messages, stream=True),
            ) as response:
                if response.status_code >= 400:
                    detail = response.read().decode("utf-8", errors="replace")[:600]
                    raise LLMError(f"模型返回 {response.status_code}：{detail}")
                for delta in _iter_sse_deltas(response.iter_lines()):
                    collected.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
    except httpx.HTTPError as exc:
        raise LLMError(f"网络请求失败：{exc}") from exc

    text = "".join(collected).strip()
    if not text:
        raise LLMError("模型没有返回任何内容（可能触发了内容过滤或超时）")
    return text


def chat_once(config: LLMConfig, messages: list[dict[str, str]]) -> str:
    """非流式对话，用于连通性测试与短结构化输出。

    冷启动时首次请求可能要几十秒，超时给足。
    """
    config.validate()
    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(
                config.chat_endpoint(),
                headers=_headers(config),
                json=_payload(config, messages, stream=False),
            )
    except httpx.HTTPError as exc:
        raise LLMError(f"网络请求失败：{exc}") from exc
    if response.status_code >= 400:
        raise LLMError(f"模型返回 {response.status_code}：{response.text[:400]}")
    try:
        data = response.json()
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise LLMError(f"响应结构无法解析：{exc}") from exc


def config_from_settings(settings: dict[str, str], *, model: str = "") -> LLMConfig:
    """把设置表里的字符串转成 LLMConfig，容错处理非法数值。"""

    def _float(key: str, fallback: float) -> float:
        try:
            return float(settings.get(key) or fallback)
        except (TypeError, ValueError):
            return fallback

    def _int(key: str, fallback: int) -> int:
        try:
            return int(float(settings.get(key) or fallback))
        except (TypeError, ValueError):
            return fallback

    return LLMConfig(
        base_url=settings.get("llm_base_url", ""),
        api_key=settings.get("llm_api_key", ""),
        model=model or settings.get("llm_model", ""),
        temperature=_float("llm_temperature", 0.3),
        max_tokens=_int("llm_max_tokens", 8192),
    )
