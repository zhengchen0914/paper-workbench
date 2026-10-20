"""发布物料：解析模型返回的 JSON、做确定性合规检查。

合规检查走本地词表，不依赖模型——同样的文本永远得到同样的结果，
这样终审页上的「是否可以发」才是可复核的判断，而不是模型的自我评价。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 分级：危险（必须改）/ 慎用（建议改）
PROHIBITED_WORDS: tuple[tuple[str, str], ...] = (
    # 绝对化用语
    ("最强", "危险"),
    ("第一", "慎用"),
    ("唯一", "危险"),
    ("顶级", "危险"),
    ("国家级", "危险"),
    ("最佳", "慎用"),
    ("最高级", "危险"),
    ("绝对", "慎用"),
    ("100%", "危险"),
    ("百分百", "慎用"),
    ("史上", "慎用"),
    # 绝对化倾向的形容词（这一类最容易在技术稿里脱口而出）
    ("完美", "慎用"),
    ("彻底", "慎用"),
    ("前所未有", "危险"),
    ("无与伦比", "危险"),
    ("业界首创", "危险"),
    ("全球领先", "危险"),
    ("遥遥领先", "危险"),
    # 夸张营销
    ("震惊", "慎用"),
    ("颠覆", "慎用"),
    ("秒杀", "慎用"),
    ("必看", "慎用"),
    ("炸裂", "慎用"),
    ("封神", "慎用"),
    # 诱导行为
    ("点击领取", "危险"),
    ("免费领取", "危险"),
    ("限时免费", "危险"),
    ("转发抽奖", "危险"),
    ("扫码领", "危险"),
    ("加微信", "危险"),
    ("私聊", "慎用"),
    ("加群", "慎用"),
    # 金融/医疗承诺
    ("稳赚", "危险"),
    ("保本", "危险"),
    ("零风险", "危险"),
    ("必赚", "危险"),
    ("翻倍", "慎用"),
    ("治愈", "危险"),
    ("疗效", "危险"),
    # 冒用官方
    ("微信官方", "慎用"),
    ("腾讯官方", "慎用"),
)


def parse_package(raw: str) -> dict[str, Any]:
    """把模型输出解析成结构化物料，容错处理代码块围栏与前后噪音。"""
    text = (raw or "").strip()
    if not text:
        return _empty_package()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return _empty_package()

    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return _empty_package()
    if not isinstance(obj, dict):
        return _empty_package()

    titles: list[dict[str, Any]] = []
    for item in obj.get("titles") or []:
        if isinstance(item, str):
            titles.append({"text": item.strip(), "angle": "", "score": 0})
            continue
        if not isinstance(item, dict):
            continue
        text_value = str(item.get("text") or "").strip()
        if not text_value:
            continue
        titles.append(
            {
                "text": text_value,
                "angle": str(item.get("angle") or "").strip(),
                "score": _to_int(item.get("score")),
            }
        )

    cover = obj.get("cover") if isinstance(obj.get("cover"), dict) else {}
    tags = [str(tag).strip() for tag in (obj.get("tags") or []) if str(tag).strip()]
    for tag in tags:
        if not tag.startswith("#"):
            tags[tags.index(tag)] = f"#{tag}"

    return {
        "titles": titles,
        "summary": str(obj.get("summary") or "").strip(),
        "cover": {
            "main": str(cover.get("main") or "").strip(),
            "sub": str(cover.get("sub") or "").strip(),
        },
        "tags": tags,
    }


def _empty_package() -> dict[str, Any]:
    return {"titles": [], "summary": "", "cover": {"main": "", "sub": ""}, "tags": []}


def empty_package() -> dict[str, Any]:
    """供作业层在物料生成失败时占位。"""
    return _empty_package()


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def check_prohibited(text: str) -> list[dict[str, Any]]:
    """返回命中项：词、等级、次数、上下文片段。"""
    hits: list[dict[str, Any]] = []
    if not text:
        return hits
    for word, level in PROHIBITED_WORDS:
        count = text.count(word)
        if count == 0:
            continue
        contexts: list[str] = []
        for match in re.finditer(re.escape(word), text):
            left = max(0, match.start() - 12)
            right = min(len(text), match.end() + 12)
            snippet = text[left:right].replace("\n", " ")
            contexts.append(f"…{snippet}…")
            if len(contexts) >= 2:
                break
        hits.append(
            {"word": word, "level": level, "count": count, "contexts": contexts}
        )
    hits.sort(key=lambda item: (item["level"] != "危险", -item["count"]))
    return hits


def summary_stats(hits: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "danger": sum(1 for hit in hits if hit["level"] == "危险"),
        "caution": sum(1 for hit in hits if hit["level"] == "慎用"),
    }
