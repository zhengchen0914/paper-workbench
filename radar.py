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


class RadarError(RuntimeError):
    """抓取失败，携带对用户可读的说明。"""


def fetch_candidates(
    keywords: list[str],
    *,
    per_keyword: int = 15,
    max_age_days: int = 180,
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
        "categories": ", ".join(categories[:3]),
        "keyword": keyword,
    }
    score, reasons = score_candidate(candidate)
    candidate["score"] = score
    candidate["reasons"] = reasons
    return candidate


def score_candidate(candidate: dict[str, Any], *, today: datetime | None = None) -> tuple[int, list[str]]:
    """可解释的加权打分（满分 10）。每条加分都附一句理由。"""
    score = 0
    reasons: list[str] = []

    blob = f"{candidate.get('title', '')} {candidate.get('comment', '')} {candidate.get('abstract', '')}"

    venue_match = VENUE_PATTERN.search(candidate.get("comment", "") or "")
    if venue_match:
        score += 3
        reasons.append(f"疑似已中 {venue_match.group(1).upper()}")

    if CODE_PATTERN.search(blob):
        score += 2
        reasons.append("附开源代码")

    keyword = (candidate.get("keyword") or "").split(" / ")[0]
    if keyword and keyword.lower() in candidate.get("title", "").lower():
        score += 2
        reasons.append("标题直接命中关键词")

    age = _age_days(candidate.get("published", ""), today=today)
    if age is not None:
        if age <= 7:
            score += 3
            reasons.append(f"{age} 天前发布")
        elif age <= 30:
            score += 2
            reasons.append(f"{age} 天前发布")
        elif age <= 90:
            score += 1
            reasons.append(f"{age} 天前发布")

    return min(score, 10), reasons


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
