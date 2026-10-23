"""科研知识库：把解读里的模块抽成「创新点」条目。

设计要点（来自方案定稿）：
- **模块级粒度**：一条 insight 对应解读里一个 `### N.` 模块，不合并成"论文级一条"。
- **只从解读抽**：不直接读 PDF——没有解读做基础，抽出来的"机制"多半是模型脑补。
- **机制用受控词表**：模型只能从预置的 30 个机制里选，选不到才提议新机制，
  且新机制默认 `verified=0`，需人工确认。否则「频域增强/小波频域/频率域建模」
  会碎成三张卡，机制卡直接废掉。
- **证据必须可回查**：每条要引用解读原文的一句，人工确认时能对着看。
"""

from __future__ import annotations

import json
import re
from typing import Any

import db

# 受控机制词表。新增机制必须先人工确认，避免碎片化。
MECHANISM_PRESETS: tuple[tuple[str, str], ...] = (
    ("频域增强", "在频率域处理特征，利用高低频分离来强化细节或抑制噪声"),
    ("小波变换", "用小波分解把特征拆成多尺度子带"),
    ("原型学习", "学习一组语义原型来聚合或过滤特征"),
    ("掩码注意力", "用预测掩码约束注意力作用范围"),
    ("交叉注意力", "在两个特征序列之间做注意力交互"),
    ("自注意力", "同一序列内部建模长程依赖"),
    ("扩散重建", "用扩散过程做重建式异常判别"),
    ("生成式合成", "用生成模型合成异常样本做训练"),
    ("知识蒸馏", "用教师模型指导学生模型"),
    ("对比学习", "用正负样本对拉近或推远表示"),
    ("自监督预训练", "用代理任务从无标注数据学表示"),
    ("记忆库", "显式存储正常样本特征供检索比对"),
    ("特征金字塔", "多尺度特征融合"),
    ("多尺度融合", "跨尺度信息聚合"),
    ("提示学习", "用可学习提示词适配下游任务"),
    ("视觉语言对齐", "对齐图像与文本表示"),
    ("零样本迁移", "不依赖目标类别的训练样本"),
    ("弱监督", "用图像级标签等弱标注训练"),
    ("半监督", "少量标注加大量无标注"),
    ("合成异常", "人工构造异常样本"),
    ("重建式检测", "以重建误差作为异常分数"),
    ("判别式检测", "直接训练分类或判别网络"),
    ("学生教师模型", "教师网络与学生网络的输出差异"),
    ("归一化流", "用可逆变换建模分布"),
    ("图神经网络", "在图结构上传播信息"),
    ("3D点云", "在点云数据上做检测或分割"),
    ("时序建模", "建模时间维度的依赖"),
    ("不确定性估计", "输出预测的置信度或方差"),
    ("边界感知", "显式建模目标边界"),
    ("轻量化设计", "以参数量或计算量为主要优化目标"),
)

MECHANISM_NAMES = tuple(name for name, _ in MECHANISM_PRESETS)

NOVELTY_TYPES = ("架构", "表示", "损失", "训练策略", "数据", "评测")

INSIGHT_SYSTEM = """你是计算机视觉领域的研究者，任务是把一篇论文解读拆成若干条**独立可检索的创新点**。

## 输入

一份中文论文解读，包含 `## 核心创新` 章节（下设 `### N. 模块名` 子节）以及 `## 实验结果`、`## 局限性`。

## 输出

只输出一个 JSON 对象，不要代码块围栏，不要任何解释：

{
  "insights": [
    {
      "title": "一句话创新点，动词短语，不超过 30 字",
      "problem": "它要解决的旧问题。必须是「现有方法哪里做不到」，不是「本文做了什么」",
      "mechanism": "核心机制，2-3 句，讲清输入输出与关键设计",
      "novelty_type": "架构 | 表示 | 损失 | 训练策略 | 数据 | 评测 六选一",
      "mechanisms": ["从受控词表选，1-3 个"],
      "new_mechanisms": [{"name": "词表里没有但确实关键的机制", "definition": "一句话定义"}],
      "applies_to": "适用场景，一句话",
      "limitation": "这条创新点自身的局限；解读没写就留空字符串",
      "evidence": "从解读原文里摘一句 20-80 字的原话作为依据"
    }
  ]
}

## 硬约束

1. 每个 `### N. 模块` 对应一条 insight，不要合并、不要拆得更细。
2. 只写解读里出现过的内容。解读没说的机制、没给的数字，一律不补。
3. `mechanisms` 只能从下面这个词表里选：
   {mechanism_list}
4. **词表里没有真正匹配的，就留空数组 `[]`，同时把真实机制写进 `new_mechanisms`。**
   禁止"挑一个最接近的凑数"——把「主元素二值化」标成「小波变换」，比留空更糟，
   因为它会污染机制卡的聚合。宁可空着，也不要错标。
5. `new_mechanisms` 最多 2 个，名字用 4–8 个字的技术名词，definition 一句话。
6. `evidence` 必须从解读原文**逐字复制**一段连续文字（20–80 字），不能改写、不能总结。
   如果找不到能逐字复制的句子，就把 `evidence` 留空字符串——**留空比改写强**，
   因为改写的"引文"会让复核失去意义。
7. `problem` 与 `mechanism` 不要互相重复：前者是"缺什么"，后者是"怎么补的"。
8. 如果解读里只有 1 个模块，就只输出 1 条；不要为凑数编造。
"""


def build_messages(reading: dict[str, Any], paper: dict[str, Any] | None) -> list[dict[str, str]]:
    """解读 → 抽取请求。"""
    system = INSIGHT_SYSTEM.replace("{mechanism_list}", "、".join(MECHANISM_NAMES))

    hints = [f"解读标题：{reading.get('title', '')}"]
    if paper:
        hints.append(f"论文文件：{paper.get('filename', '')}")
        if paper.get("venue"):
            hints.append(f"出处：{paper['venue']}")

    user = (
        "从下面这份解读里抽取创新点。\n\n"
        "## 背景\n" + "\n".join(hints) + "\n\n"
        "## 解读原文\n" + (reading.get("content") or "")
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_insights(raw: str) -> list[dict[str, Any]]:
    """解析模型输出，容错处理代码块围栏与前后噪音。"""
    text = (raw or "").strip()
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []

    items = payload.get("insights")
    if not isinstance(items, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        if not title:
            continue
        mechanisms = [
            name
            for name in (item.get("mechanisms") or [])
            if isinstance(name, str) and name.strip() in MECHANISM_NAMES
        ]
        new_mechanisms = []
        for extra in item.get("new_mechanisms") or []:
            if not isinstance(extra, dict):
                continue
            name = _text(extra.get("name"))
            if name and name not in MECHANISM_NAMES:
                new_mechanisms.append(
                    {"name": name, "definition": _text(extra.get("definition"))}
                )
        novelty = _text(item.get("novelty_type"))
        cleaned.append(
            {
                "ordinal": index,
                "title": title[:120],
                "problem": _text(item.get("problem")),
                "mechanism": _text(item.get("mechanism")),
                "novelty_type": novelty if novelty in NOVELTY_TYPES else "",
                "mechanisms": mechanisms[:3],
                "new_mechanisms": new_mechanisms[:2],
                "applies_to": _text(item.get("applies_to")),
                "limitation": _text(item.get("limitation")),
                "evidence": _text(item.get("evidence"))[:400],
            }
        )
    return cleaned


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def evidence_in_source(evidence: str, content: str) -> bool:
    """引文是否真的出现在解读原文里。

    去掉所有空白再比——模型常在中文里插空格，那不是改写，不该算不通过。
    这一步是确定性的，不依赖模型自评：改写过的"引文"会让人工复核失去意义。
    """
    needle = re.sub(r"\s+", "", evidence or "")
    if len(needle) < 8:
        return False
    haystack = re.sub(r"\s+", "", content or "")
    return needle in haystack


_SENTENCE_SPLIT = re.compile(r"[。；\n]+")


def best_source_sentence(evidence: str, content: str, *, threshold: float = 0.45) -> tuple[str, float]:
    """引文被改写时，回原文找最接近的那一句。

    实测模型会稳定地把原句重组成「核心创新是…」这类总结，喊提示词没用。
    与其让复核者对着一段假引文猜，不如把真正的原句找出来摆在他面前。
    用字符级相似度（difflib）而不是模型——这一步必须是确定性的。
    """
    from difflib import SequenceMatcher

    needle = re.sub(r"\s+", "", evidence or "")
    if len(needle) < 8:
        return "", 0.0

    best, best_ratio = "", 0.0
    for chunk in _SENTENCE_SPLIT.split(content or ""):
        candidate = re.sub(r"\s+", "", chunk).strip()
        if not (12 <= len(candidate) <= 260):
            continue
        ratio = SequenceMatcher(None, needle, candidate).ratio()
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    if best_ratio < threshold:
        return "", best_ratio
    return best, best_ratio


# ---------------------------------------------------------------- 落库


def ensure_mechanisms() -> None:
    """把受控词表写进 mechanisms 表（幂等）。预置项 verified=1。"""
    existing = {row["name"] for row in db.query("SELECT name FROM mechanisms")}
    timestamp = db.now()
    for name, definition in MECHANISM_PRESETS:
        if name in existing:
            continue
        db.execute(
            "INSERT INTO mechanisms(uid, name, definition, verified, created_at) "
            "VALUES(?,?,?,?,?)",
            (db.new_uid("mech"), name, definition, 1, timestamp),
        )


def replace_insights(
    draft: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    theme: str = "",
) -> int:
    """写入某篇解读的洞察。同篇重抽时整体替换，避免重复堆积。"""
    draft_uid = draft["uid"]
    with db.session() as conn:
        conn.execute("DELETE FROM insight_mechanisms WHERE insight_uid IN "
                     "(SELECT uid FROM insights WHERE draft_uid=?)", (draft_uid,))
        conn.execute("DELETE FROM insights WHERE draft_uid=?", (draft_uid,))

    timestamp = db.now()
    source_text = draft.get("content") or ""
    for item in items:
        insight_uid = db.new_uid("ins")
        evidence_ok = 1 if evidence_in_source(item["evidence"], source_text) else 0
        matched, score = ("", 0.0)
        if not evidence_ok:
            matched, score = best_source_sentence(item["evidence"], source_text)
        db.execute(
            "INSERT INTO insights(uid, paper_uid, draft_uid, draft_version, ordinal, "
            "title, problem, mechanism, novelty_type, applies_to, limitation, evidence, "
            "evidence_ok, evidence_source, evidence_score, theme, status, created_at, "
            "updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                insight_uid,
                draft.get("paper_uid", ""),
                draft_uid,
                int(draft.get("version") or 0),
                int(item["ordinal"]),
                item["title"],
                item["problem"],
                item["mechanism"],
                item["novelty_type"],
                item["applies_to"],
                item["limitation"],
                item["evidence"],
                evidence_ok,
                matched,
                round(score, 2),
                theme,
                "pending",
                timestamp,
                timestamp,
            ),
        )
        for name in item.get("mechanisms", []):
            mech = db.one("SELECT uid FROM mechanisms WHERE name=?", (name,))
            if mech is None:
                continue
            db.execute(
                "INSERT OR IGNORE INTO insight_mechanisms(insight_uid, mechanism_uid, "
                "variant_note) VALUES(?,?,?)",
                (insight_uid, mech["uid"], ""),
            )
        for extra in item.get("new_mechanisms", []):
            mech_uid = db.new_uid("mech")
            db.execute(
                "INSERT OR IGNORE INTO mechanisms(uid, name, definition, verified, created_at) "
                "VALUES(?,?,?,?,?)",
                (mech_uid, extra["name"], extra["definition"], 0, timestamp),
            )
            row = db.one("SELECT uid FROM mechanisms WHERE name=?", (extra["name"],))
            if row:
                db.execute(
                    "INSERT OR IGNORE INTO insight_mechanisms(insight_uid, mechanism_uid, "
                    "variant_note) VALUES(?,?,?)",
                    (insight_uid, row["uid"], "模型提议的新机制"),
                )
    db.log_event(draft_uid, "insights:extracted", f"{len(items)} 条")
    return len(items)


def count_for_draft(draft_uid: str) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM insights WHERE draft_uid=?", (draft_uid,))
    return int(row["n"]) if row else 0


def split_core_section(content: str) -> str:
    """只取「核心创新」章节做抽取，减少无关上下文。找不到就退回全文。"""
    text = content or ""
    match = re.search(r"^##\s*核心创新\s*$", text, flags=re.M)
    if not match:
        return text
    tail = text[match.end() :]
    stop = re.search(r"^##\s", tail, flags=re.M)
    return (text[match.start() : match.end()] + (tail[: stop.start()] if stop else tail)).strip()
