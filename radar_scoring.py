"""摘要质量打分：雷达加权分里占 60% 的那部分。

为什么单独一个模块：这是全项目唯一「用模型判断质量」的地方，
必须把评分维度写死、把理由留下来，否则又会变成不可复核的黑箱。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 四个维度，权重和为 1
DIMENSIONS: tuple[tuple[str, float, str], ...] = (
    ("problem", 0.30, "问题价值：它要解决的问题是不是真问题、有没有实际意义"),
    ("novelty", 0.35, "方法新颖度：相对已有做法的实质增量，而不是换模块"),
    ("evidence", 0.20, "证据强度：摘要里给出的实验规模、对比基线、指标是否支撑结论"),
    ("clarity", 0.15, "表述清晰度：摘要能否让人准确说出它做了什么"),
)

SYSTEM = """你是计算机视觉方向的资深审稿人。给你一篇论文的标题与摘要，
请按四个维度打分（0–10 的整数），并给一句话理由。

## 打分尺度（请严格区分，不要都给 7 分）

- 9–10：提出新问题或新范式，摘要里就能看出扎实证据
- 7–8：有清晰且实质的贡献，实验设置可信
- 5–6：合格的增量改进，换了模块或数据集，结论成立但不出人意料
- 3–4：贡献微弱，主要是组合已有方法，或只在单一数据集上验证
- 0–2：摘要含糊，看不出做了什么，或明显缺乏证据

## 四个维度

{dimensions}

## 硬约束

1. **只根据摘要里写的内容打分**，不要因为机构、名气或你的先验知识加分。
2. 摘要没提实验就扣 evidence，不要假设它有。
3. 分数要有区分度：同一批论文里应当有高有低。
4. 只输出 JSON，不要围栏：
{{"problem": 7, "novelty": 6, "evidence": 5, "clarity": 8, "note": "一句话说明扣分点"}}
"""


def build_messages(candidate: dict[str, Any]) -> list[dict[str, str]]:
    dims = "\n".join(f"- `{key}`：{desc}" for key, _, desc in DIMENSIONS)
    system = SYSTEM.replace("{dimensions}", dims)
    user = (
        f"标题：{candidate.get('title', '')}\n"
        f"会议线索：{candidate.get('comment', '') or '（无）'}\n\n"
        f"摘要：\n{candidate.get('abstract', '')[:4000]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse(raw: str) -> dict[str, Any] | None:
    """解析模型输出，返回 {score, dims, note}；解析失败返回 None。"""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    dims: dict[str, int] = {}
    for key, _, _ in DIMENSIONS:
        try:
            value = int(round(float(payload.get(key, -1))))
        except (TypeError, ValueError):
            return None
        if value < 0 or value > 10:
            return None
        dims[key] = value

    total = sum(dims[key] * weight for key, weight, _ in DIMENSIONS)
    return {
        "score": round(total, 2),
        "dims": dims,
        "note": str(payload.get("note", ""))[:200],
    }


def breakdown(dims: dict[str, int]) -> str:
    """把维度分数压成一行，便于展示。"""
    return " · ".join(f"{label.split('：')[0]} {dims.get(key, 0)}" for key, _, label in DIMENSIONS)
