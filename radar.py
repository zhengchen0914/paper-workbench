"""选题雷达：从 arXiv 拉候选论文，评分后交给选题池。

设计取舍：
- 只用 arXiv 官方 Atom API，不抓页面、不绕验证；
- 评分是**可解释的加权规则**，不是模型打分——这样「为什么这篇值得写」能被复核；
- 每篇都保留命中的关键词与理由，避免变成又一个黑盒推荐流。
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

import affiliations

ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "PaperWorkbench/1.0 (local research tool; contact: local user)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# 顶会/顶刊线索：出现在 arxiv:comment 里通常意味着已中稿
VENUE_PATTERN = re.compile(
    r"\b(CVPR|ICCV|ECCV|AAAI|NeurIPS|NIPS|ICLR|ICML|ACM\s*MM|TPAMI|TIP|TMM|"
    r"ICRA|WACV|BMVC|IJCAI|SIGGRAPH)\b",
    re.IGNORECASE,
)
CODE_PATTERN = re.compile(r"github\.com|gitlab\.com|huggingface\.co", re.IGNORECASE)

# 时效按年计：一年内算新（含半年），一到三年算过渡，三年以上算过时
FRESH_DAYS = 365
STALE_DAYS = 365 * 3

# 期刊常写全称（journal_ref 里），而质量档位要的是缩写，这里做一层映射
JOURNAL_ALIASES: dict[str, str] = {
    "TPAMI": "pattern analysis and machine intelligence",
    "TIP": "transactions on image processing",
    "TMM": "transactions on multimedia",
    "IJCV": "international journal of computer vision",
    "TNNLS": "transactions on neural networks and learning systems",
}


class RadarError(RuntimeError):
    """抓取失败，携带对用户可读的说明。"""


def fetch_candidates(
    keywords: list[str],
    *,
    per_keyword: int = 15,
    max_age_days: int = 1095,
    timeout: float = 30.0,
    pause_seconds: float = 1.0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按关键词抓取候选，返回 (候选列表, 错误列表)。

    单个关键词失败不影响其余；arXiv 建议请求之间留间隔，这里默认 1 秒。
    """
    cleaned = [k.strip() for k in keywords if k.strip()]
    if not cleaned:
        return [], ["没有配置关键词"]

    collected: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        for index, keyword in enumerate(cleaned):
            if index:
                time.sleep(pause_seconds)
            try:
                entries = _query(client, keyword, per_keyword)
            except RadarError as exc:
                errors.append(f"{keyword}：{exc}")
                continue
            for entry in entries:
                candidate = _to_candidate(entry, keyword)
                if candidate is None:
                    continue
                if _too_old(candidate["published"], max_age_days):
                    continue
                key = candidate["external_id"]
                # 同一篇被多个关键词命中时保留先到的，但记下多个来源词
                if key in collected:
                    existing = collected[key]
                    if keyword not in existing["keyword"]:
                        existing["keyword"] = f"{existing['keyword']} / {keyword}"
                    continue
                collected[key] = candidate

    ordered = sorted(
        collected.values(),
        key=lambda item: (item["score"], item["published"]),
        reverse=True,
    )
    return ordered, errors


def _query(client: httpx.Client, keyword: str, per_keyword: int) -> list[ET.Element]:
    params = {
        "search_query": f'abs:"{keyword}"',
        "start": 0,
        "max_results": max(1, min(per_keyword, 50)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        response = client.get(ARXIV_API, params=params)
    except httpx.HTTPError as exc:
        raise RadarError(f"网络错误：{exc}") from exc

    if response.status_code >= 400:
        raise RadarError(f"arXiv 返回 {response.status_code}")
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise RadarError(f"响应无法解析：{exc}") from exc
    return root.findall("atom:entry", NS)


def _to_candidate(entry: ET.Element, keyword: str) -> dict[str, Any] | None:
    raw_id = _text(entry, "atom:id")
    title = _clean(_text(entry, "atom:title"))
    if not raw_id or not title:
        return None

    external_id = raw_id.rsplit("/abs/", 1)[-1]
    summary = _clean(_text(entry, "atom:summary"))
    authors = [
        name.text.strip()
        for name in entry.findall("atom:author/atom:name", NS)
        if name.text and name.text.strip()
    ]
    pdf_url = ""
    abs_url = raw_id
    for link in entry.findall("atom:link", NS):
        href = link.get("href") or ""
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = href
        elif link.get("rel") == "alternate":
            abs_url = href
    categories = [
        category.get("term", "")
        for category in entry.findall("atom:category", NS)
        if category.get("term")
    ]
    comment = _clean(_text(entry, "arxiv:comment"))
    journal_ref = _clean(_text(entry, "arxiv:journal_ref"))

    candidate = {
        "source": "arxiv",
        "external_id": external_id,
        "title": title,
        "authors": ", ".join(authors[:6]) + (" 等" if len(authors) > 6 else ""),
        "abstract": summary,
        "published": _text(entry, "atom:published")[:10],
        "url": abs_url,
        "pdf_url": pdf_url,
        "comment": comment,
        "journal_ref": journal_ref,
        "categories": ", ".join(categories[:3]),
        "keyword": keyword,
    }
    score, reasons = score_candidate(candidate)
    candidate["score"] = score
    candidate["reasons"] = reasons
    return candidate


# 元数据侧的权重（合计 1.0），最终与摘要分按 30/70 合成
META_WEIGHTS = {
    "venue": 0.45,   # 顶会/顶刊
    "inst": 0.25,    # 作者单位
    "code": 0.15,    # 开源代码
    "fresh": 0.15,   # 时效
}

CONTENT_WEIGHT = 0.70
META_WEIGHT = 0.30


def metadata_score(
    candidate: dict[str, Any], *, today: datetime | None = None
) -> tuple[float, list[str], dict[str, float]]:
    """元数据分（0–10），不掺摘要判断。返回 (分, 理由, 各维度)。

    每个维度先归一化到 0–10，再加权——这样各项可比，不会因为「多打几条标签」
    就把分数堆上去。
    """
    reasons: list[str] = []
    parts: dict[str, float] = {}

    # 顶会/顶刊
    venue_text = f"{candidate.get('comment', '') or ''} {candidate.get('journal_ref', '') or ''}"
    venue_match = VENUE_PATTERN.search(venue_text)
    venue_lower = venue_text.lower()
    venue_name = venue_match.group(1).upper() if venue_match else next(
        (short for short, full in JOURNAL_ALIASES.items() if full in venue_lower), ""
    )
    if venue_name:
        parts["venue"] = 10.0
        reasons.append(f"疑似已中 {venue_name}")
    elif (candidate.get("journal_ref") or "").strip():
        parts["venue"] = 7.0
        reasons.append("已有期刊出处")
    else:
        parts["venue"] = 0.0

    # 作者单位
    institutions = candidate.get("institutions") or []
    if affiliations.is_top(institutions):
        parts["inst"] = 10.0
        reasons.append(f"强机构：{affiliations.describe(institutions)}")
    elif institutions:
        parts["inst"] = 5.0
        reasons.append(f"机构：{affiliations.describe(institutions)}")
    else:
        parts["inst"] = 0.0

    # 开源代码
    blob = f"{candidate.get('title', '')} {candidate.get('comment', '')} {candidate.get('abstract', '')}"
    if CODE_PATTERN.search(blob):
        parts["code"] = 10.0
        reasons.append("附开源代码")
    else:
        parts["code"] = 0.0

    # 时效（按年）
    age = _age_days(candidate.get("published", ""), today=today)
    if age is None:
        parts["fresh"] = 0.0
    elif age <= FRESH_DAYS:
        parts["fresh"] = 10.0
        reasons.append(f"{age} 天前发布（一年内）")
    elif age <= STALE_DAYS:
        parts["fresh"] = 6.0
        reasons.append(f"{age // 365} 年前发布")
    else:
        parts["fresh"] = 2.0
        reasons.append("超过 3 年，按过时处理")

    total = sum(parts[key] * weight for key, weight in META_WEIGHTS.items())
    return round(total, 1), reasons, parts


def combine_scores(content: float, meta: float) -> float:
    """摘要分占 70%、元数据占 30%，合成 0–10 的加权总分。"""
    return round(content * CONTENT_WEIGHT + meta * META_WEIGHT, 1)


def score_candidate(
    candidate: dict[str, Any], *, today: datetime | None = None
) -> tuple[float, list[str]]:
    """只算元数据分（兼容旧调用）；完整分要用 combine_scores。"""
    total, reasons, _ = metadata_score(candidate, today=today)
    return total, reasons


def _age_days(published: str, *, today: datetime | None = None) -> int | None:
    try:
        date = datetime.strptime(published[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    now = today or datetime.now(timezone.utc)
    return max(0, (now - date).days)


def _too_old(published: str, max_age_days: int) -> bool:
    if max_age_days <= 0:
        return False
    age = _age_days(published)
    return age is not None and age > max_age_days


def _text(entry: ET.Element, path: str) -> str:
    node = entry.find(path, NS)
    return (node.text or "").strip() if node is not None and node.text else ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def download_pdf(pdf_url: str, target_path: Any, *, timeout: float = 120.0) -> int:
    """下载 PDF 到本地，返回字节数。"""
    if not pdf_url:
        raise RadarError("该候选没有 PDF 链接")
    try:
        with httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            response = client.get(pdf_url)
    except httpx.HTTPError as exc:
        raise RadarError(f"下载失败：{exc}") from exc
    if response.status_code >= 400:
        raise RadarError(f"下载失败：HTTP {response.status_code}")
    if not response.content.startswith(b"%PDF"):
        raise RadarError("下载到的不是 PDF（可能是反爬页面）")
    target_path.write_bytes(response.content)
    return len(response.content)
