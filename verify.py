"""数字溯源：把生成稿里的每个数字回查原始 PDF 文本。

为什么值得单独做一个模块：
- 模型最危险的不是写得差，而是把数字写错还显得很确定；
- 百分号、小数位、四舍五入会造成「不一致但其实正确」的假警报，必须归一化后再比；
- 把「哪些数字原文里找不到」直接摆在终审页上，人工复核才有抓手。

判定顺序：字面命中 → 去百分号 → 数值邻近（允许末位四舍五入）→ 判为未命中。
"""

from __future__ import annotations

import re
from typing import Any

# 抓取数字及其上下文；不抓纯页码引用（如 "p5"）以外的字母数字混排
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(%?)")

_IGNORE_TOKENS = {
    "0",  # 单独出现的 0 多来自列表编号或「0 成本」这类口语，噪声太大
}


def _source_vocabulary(source: str) -> tuple[set[str], list[float]]:
    literals: set[str] = set()
    values: list[float] = []
    for match in _NUMBER.finditer(source or ""):
        raw = match.group(1)
        literals.add(raw)
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return literals, values


def trace_numbers(generated: str, source: str) -> dict[str, Any]:
    """返回 {total, matched, unmatched, details}。"""
    if not (generated or "").strip() or not (source or "").strip():
        return {"total": 0, "matched": 0, "unmatched": 0, "details": []}

    literals, values = _source_vocabulary(source)
    details: list[dict[str, Any]] = []
    seen: set[str] = set()

    for match in _NUMBER.finditer(generated):
        raw = match.group(1)
        percent = match.group(2)
        key = raw + percent
        if key in seen or raw in _IGNORE_TOKENS:
            continue
        seen.add(key)

        verdict, method = _judge(raw, percent, literals, values)
        if verdict:
            continue
        start = max(0, match.start() - 14)
        end = min(len(generated), match.end() + 14)
        details.append(
            {
                "number": key,
                "context": generated[start:end].replace("\n", " "),
            }
        )

    total = len(seen)
    unmatched = len(details)
    return {
        "total": total,
        "matched": total - unmatched,
        "unmatched": unmatched,
        "details": details[:20],
    }


def _judge(
    raw: str,
    percent: str,
    literals: set[str],
    values: list[float],
) -> tuple[bool, str]:
    # 1) 字面命中（数字本身在原文出现过）
    if raw in literals:
        return True, "literal"
    # 2) 只差百分号：原文写 92.0，生成写 92.0%
    if percent and raw in literals:
        return True, "percent"

    # 3) 数值邻近：允许末位四舍五入
    try:
        target = float(raw)
    except ValueError:
        return False, "unparsed"

    decimals = len(raw.split(".")[1]) if "." in raw else 0
    tolerance = 0.5 * (10 ** -decimals) if decimals else 0.5
    for value in values:
        if abs(value - target) <= tolerance:
            return True, "rounding"
    return False, "missing"
