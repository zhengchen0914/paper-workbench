"""GitHub 开源项目榜单：抓取、解析、限流处理。

三条来自实测的硬约束（决定本模块的形态）：
1. **查询式必须精确**。`anomaly detection in:name,description,readme` 会把任何 README 里
   提过该词的仓库捞进来——搜「异常检测」第一名是 awesome-go。所以只用 `topic:` 或
   带引号的精确短语，本模块默认查询式都遵守这条。
2. **star 排序救得了噪声，救不了"命中总数"**。`dsh in:name` 命中 5 万，但前几名确实是
   DSH 生态项目——所以榜单只对头部负责，total_count 只作参考。
3. **未认证搜索限流 10 次/分钟**。一个榜一次请求，刷新多了会撞 403；配 Token 后是 30 次/分钟。
   这里把剩余额度和重置时间一起抛给上层，让界面能说实话。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

SEARCH_URL = "https://api.github.com/search/repositories"
USER_AGENT = "PaperWorkbench/1.0 (+https://github.com/zhengchen0914/paper-workbench)"


class GitHubError(RuntimeError):
    """抓取失败，消息面向用户。"""


class RateLimitError(GitHubError):
    """触发限流，带重置时间。"""

    def __init__(self, message: str, reset_epoch: int | None = None) -> None:
        super().__init__(message)
        self.reset_epoch = reset_epoch

    def wait_hint(self) -> str:
        if not self.reset_epoch:
            return "稍后再试"
        remain = max(0, self.reset_epoch - int(time.time()))
        return f"约 {remain} 秒后恢复（{time.strftime('%H:%M:%S', time.localtime(self.reset_epoch))}）"


@dataclass(frozen=True)
class BoardResult:
    items: list[dict[str, Any]]
    total_count: int
    rate_remaining: int | None
    rate_limit: int | None
    fetched_at: str


def fetch_board(
    query: str,
    *,
    per_page: int = 30,
    token: str = "",
    timeout: float = 30.0,
) -> BoardResult:
    """抓一个榜单。查询式为空或过长直接拒绝，避免把 GUI 当搜索框用。"""
    cleaned = (query or "").strip()
    if not cleaned:
        raise GitHubError("查询式为空")
    if len(cleaned) > 256:
        raise GitHubError("查询式过长（GitHub 限制 256 字符）")

    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params = {
        "q": cleaned,
        "sort": "stars",
        "order": "desc",
        "per_page": max(1, min(per_page, 100)),
    }

    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            response = client.get(SEARCH_URL, params=params)
    except httpx.HTTPError as exc:
        raise GitHubError(f"网络错误：{exc}") from exc

    remaining = _int_header(response, "x-ratelimit-remaining")
    limit = _int_header(response, "x-ratelimit-limit")

    if response.status_code in (403, 429):
        reset = _int_header(response, "x-ratelimit-reset")
        raise RateLimitError(
            "GitHub 搜索限流" + ("（未配置 Token 时只有 10 次/分钟）" if not token else ""),
            reset_epoch=reset,
        )
    if response.status_code >= 400:
        raise GitHubError(f"GitHub 返回 {response.status_code}：{response.text[:200]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubError(f"响应无法解析：{exc}") from exc

    items = [parse_item(item) for item in payload.get("items", [])]
    return BoardResult(
        items=items,
        total_count=int(payload.get("total_count") or 0),
        rate_remaining=remaining,
        rate_limit=limit,
        fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def parse_item(item: dict[str, Any]) -> dict[str, Any]:
    """把 GitHub 的 repo 对象压成榜单需要的字段。"""
    return {
        "full_name": item.get("full_name") or "",
        "url": item.get("html_url") or "",
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "language": item.get("language") or "",
        "description": (item.get("description") or "").strip(),
        "topics": ", ".join((item.get("topics") or [])[:6]),
        "pushed_at": (item.get("pushed_at") or "")[:10],
        "owner": (item.get("owner") or {}).get("login", ""),
    }


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


# 预置榜单：查询式都经过实测，能给出干净的头部结果。
# 想改就改——榜单的权威性来自查询定义，不来自程序。
DEFAULT_BOARDS: tuple[dict[str, Any], ...] = (
    {
        "key": "ai",
        "name": "AI 开源总榜",
        "query": "topic:ai stars:>3000",
        "note": "全领域 AI 项目，按 star 排。头部多是 Agent / 工具类项目。",
        "per_page": 30,
    },
    {
        "key": "anomaly",
        "name": "工业异常检测",
        "query": "topic:anomaly-detection stars:>100",
        "note": "MVTec / 工业质检方向的库与资源合集。",
        "per_page": 30,
    },
    {
        "key": "defect",
        "name": "缺陷检测 / COD",
        "query": "topic:defect-detection",
        "note": "表面缺陷检测与伪装目标检测的官方实现。",
        "per_page": 30,
    },
    {
        "key": "skills",
        "name": "Agent Skill 生态",
        "query": "topic:agent-skills stars:>200",
        "note": "给 AI 助手用的技能包仓库。",
        "per_page": 30,
    },
    {
        "key": "dsh",
        "name": "DSH 插件生态",
        "query": "dsh in:name",
        "note": "命中总数噪声较大（dsh 三字母会撞车），只看 star 排序的头部即可。",
        "per_page": 30,
    },
)
