"""作者单位查询：arXiv 官方元数据不含单位，走 OpenAlex 补。

设计取舍：
- 只查 OpenAlex（免 key、有 DOI→arXiv 映射），不做多源合并；
- 查不到就是空列表，**不猜、不降级**——新预印本常常还没被收录，
  这种情况不扣分，只是少了这条证据；
- 顶层机构用白名单匹配 `display_name`，命中给分，其余机构信息只记账不给分。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

OPENALEX_WORK = "https://api.openalex.org/works/https://doi.org/10.48550/arXiv.{aid}"
USER_AGENT = "PaperWorkbench/1.0 (local research tool; mailto:local@example.com)"

# 公认的强机构/强实验室。匹配是 display_name 的小写子串，所以写短一点更稳。
TOP_INSTITUTIONS: tuple[str, ...] = (
    # 北美
    "massachusetts institute of technology",
    "stanford",
    "carnegie mellon",
    "university of california",
    "berkeley",
    "princeton",
    "cornell",
    "university of washington",
    "johns hopkins",
    "university of toronto",
    # 欧洲
    "university of oxford",
    "university of cambridge",
    "eth zurich",
    "epfl",
    "max planck",
    "inria",
    "imperial college",
    "university college london",
    # 亚洲
    "tsinghua",
    "peking university",
    "zhejiang university",
    "shanghai jiao tong",
    "fudan",
    "university of science and technology of china",
    "harbin institute",
    "chinese academy of sciences",
    "shanghai ai laboratory",
    "university of hong kong",
    "chinese university of hong kong",
    "national university of singapore",
    "nanyang technological",
    "kaist",
    "seoul national",
    "university of tokyo",
    # 工业实验室
    "openai",
    "deepmind",
    "google",
    "microsoft",
    "meta",
    "nvidia",
    "apple",
    "amazon",
    "tencent",
    "alibaba",
    "baidu",
    "huawei",
    "bytedance",
    "sensetime",
    "jpmorgan",
)

_cache: dict[str, list[str]] = {}


def _arxiv_id(external_id: str) -> str:
    """2610.36572v1 → 2610.36572"""
    return (external_id or "").split("v")[0]


def lookup(external_id: str, *, timeout: float = 12.0) -> list[str]:
    """按 arXiv ID 查作者单位（去重、最多 6 个）。查不到返回空列表。"""
    aid = _arxiv_id(external_id)
    if not aid or "/" in aid:
        return []
    if aid in _cache:
        return _cache[aid]

    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(OPENALEX_WORK.format(aid=aid))
        if response.status_code != 200:
            _cache[aid] = []
            return []
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError):
        _cache[aid] = []
        return []

    names: list[str] = []
    for authorship in payload.get("authorships") or []:
        for inst in authorship.get("institutions") or []:
            name = (inst.get("display_name") or "").strip()
            if name and name not in names:
                names.append(name)
    result = names[:6]
    _cache[aid] = result
    return result


def is_top(institutions: list[str]) -> bool:
    """是否有白名单里的机构。"""
    lowered = [name.lower() for name in institutions]
    return any(keyword in name for name in lowered for keyword in TOP_INSTITUTIONS)


def describe(institutions: list[str]) -> str:
    return "、".join(institutions[:3])


def prefetch(candidates: list[dict[str, Any]], *, pause: float = 0.12) -> int:
    """给一批候选补上 institutions 字段（就地修改），返回查到机构的条数。

    串行 + 短间隔：OpenAlex 对匿名调用限速 10 次/秒，这里留足余量。
    """
    found = 0
    for candidate in candidates:
        if "institutions" in candidate:
            continue
        names = lookup(candidate.get("external_id", ""))
        candidate["institutions"] = names
        if names:
            found += 1
        time.sleep(pause)
    return found
