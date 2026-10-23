"""作业执行：单消费者队列 + 后台线程，状态实时写库。

为什么是串行队列而不是"点击即并发"：
- 模型接口有速率限制，并发跑几篇只会互相拖慢；
- SQLite 写入集中在一个消费者里，避免多线程争锁；
- 「排队中」对使用者是可预期状态，而不是界面卡住。

作业类型：
- reading  论文 PDF → 结构化解读
- article  解读 → 公众号正文 + 发布物料
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

import db
import knowledge
import llm
import packaging
import pdfx
import prompts

_QUEUE: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False
_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def is_running(draft_uid: str) -> bool:
    with _LOCK:
        return draft_uid in _RUNNING


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(target=_worker_loop, daemon=True, name="job-worker").start()
        _WORKER_STARTED = True


def _worker_loop() -> None:
    while True:
        kind, job_uid, payload = _QUEUE.get()
        try:
            if kind == "reading":
                _run_reading(
                    job_uid,
                    payload["draft_uid"],
                    payload["engine"],
                    payload["focus"],
                )
            elif kind == "insights":
                _run_insights(job_uid, payload["draft_uid"])
            elif kind == "article":
                _run_article(
                    job_uid,
                    payload["reading_uid"],
                    payload["article_uid"],
                    payload["engine"],
                    payload["angle"],
                )
            elif kind == "roundup":
                _run_roundup(job_uid, payload["article_uid"], payload["board"], payload["repos"])
            elif kind == "weekly":
                _run_weekly(
                    job_uid,
                    payload["article_uid"],
                    payload["board"],
                    payload["github"],
                    payload["papers"],
                )
        except Exception as exc:  # 兜底：worker 不能因为单个作业死掉
            _set_job(job_uid, "failed", str(exc) or exc.__class__.__name__)
        finally:
            _QUEUE.task_done()


# ---------------------------------------------------------------- 入队


def start_reading(draft_uid: str, *, engine: str, focus: str = "") -> str:
    """把解读作业排进队列，返回 job uid。"""
    draft = db.one("SELECT * FROM drafts WHERE uid=?", (draft_uid,))
    if draft is None:
        raise ValueError("稿件不存在")

    with _LOCK:
        if draft_uid in _RUNNING:
            raise RuntimeError("该稿件已在队列中或正在运行")
        _RUNNING.add(draft_uid)

    job_uid = db.new_uid("job")
    db.execute(
        "INSERT INTO jobs(uid, kind, paper_uid, draft_uid, status, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_uid, "reading", draft["paper_uid"], draft_uid, "queued", "排队中", db.now()),
    )
    db.execute(
        "UPDATE drafts SET status=?, engine=?, error='', updated_at=? WHERE uid=?",
        ("queued", engine, db.now(), draft_uid),
    )
    db.log_event(draft_uid, "generate:queued", f"engine={engine}")

    _ensure_worker()
    _QUEUE.put(
        ("reading", job_uid, {"draft_uid": draft_uid, "engine": engine, "focus": focus})
    )
    return job_uid


def start_batch(paper_uids: list[str], *, engine: str = "builtin") -> int:
    """按选题批量排解读作业，返回成功入队数量。"""
    if not paper_uids:
        return 0
    queued = 0
    for paper_uid in paper_uids:
        draft = db.one(
            "SELECT * FROM drafts WHERE paper_uid=? AND kind='reading' "
            "ORDER BY created_at DESC LIMIT 1",
            (paper_uid,),
        )
        if draft is None or draft["status"] in {"ready", "queued", "running"}:
            continue
        if is_running(draft["uid"]):
            continue
        try:
            start_reading(draft["uid"], engine=engine)
            queued += 1
        except Exception:
            continue
    return queued


def start_article(
    reading_uid: str, *, engine: str, angle: str = "", preset: str = "deep"
) -> str:
    """解读 → 公众号正文 + 发布物料。返回 article draft uid。"""
    article_uid = ensure_article(reading_uid)
    with _LOCK:
        if article_uid in _RUNNING:
            raise RuntimeError("这篇文章已在队列中或正在运行")
        _RUNNING.add(article_uid)

    job_uid = db.new_uid("job")
    db.execute(
        "INSERT INTO jobs(uid, kind, paper_uid, draft_uid, status, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_uid, "article", "", article_uid, "queued", "排队中", db.now()),
    )
    db.execute(
        "UPDATE drafts SET status=?, engine=?, preset=?, error='', updated_at=? WHERE uid=?",
        ("queued", engine, preset, db.now(), article_uid),
    )
    db.log_event(article_uid, "article:queued", f"engine={engine} preset={preset}")

    _ensure_worker()
    _QUEUE.put(
        (
            "article",
            job_uid,
            {
                "reading_uid": reading_uid,
                "article_uid": article_uid,
                "engine": engine,
                "angle": angle,
            },
        )
    )
    return article_uid


def ensure_article(reading_uid: str) -> str:
    """取这条解读对应的公众号稿件，没有就建一个。"""
    existing = db.one(
        "SELECT uid FROM drafts WHERE parent_uid=? AND kind='article' "
        "ORDER BY created_at DESC LIMIT 1",
        (reading_uid,),
    )
    if existing:
        return existing["uid"]
    reading = db.one("SELECT * FROM drafts WHERE uid=?", (reading_uid,))
    if reading is None:
        raise ValueError("解读稿件不存在")
    article_uid = db.new_uid("article")
    timestamp = db.now()
    db.execute(
        "INSERT INTO drafts(uid, paper_uid, kind, title, status, parent_uid, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            article_uid,
            reading["paper_uid"],
            "article",
            reading["title"],
            "empty",
            reading_uid,
            timestamp,
            timestamp,
        ),
    )
    return article_uid


# ---------------------------------------------------------------- 执行


def _run_reading(job_uid: str, draft_uid: str, engine: str, focus: str) -> None:
    try:
        draft = db.one("SELECT * FROM drafts WHERE uid=?", (draft_uid,))
        paper = db.one("SELECT * FROM papers WHERE uid=?", (draft["paper_uid"],))
        if paper is None:
            raise RuntimeError("找不到关联的论文记录")

        db.execute(
            "UPDATE drafts SET status='running', updated_at=? WHERE uid=?",
            (db.now(), draft_uid),
        )
        _set_job(job_uid, "running", "正在提取 PDF 文本")

        extracted = pdfx.extract(paper["path"])
        if not extracted["has_text_layer"]:
            raise RuntimeError(
                "这份 PDF 没有文本层（可能是扫描件），当前版本无法自动解读。"
                "请换一份带文本层的 PDF，或改用「交给 DSH」引擎。"
            )

        settings = db.get_settings()
        limit = int(float(settings.get("max_paper_chars") or 120000))
        text, truncated = pdfx.truncate(extracted["text"], limit)
        _set_job(job_uid, "running", f"已读取 {extracted['pages']} 页 / {len(text)} 字")

        if engine == "dsh":
            _hand_over_to_dsh(job_uid, draft_uid, paper, truncated=truncated, focus=focus)
            return

        config = llm.config_from_settings(settings)
        messages = prompts.build_reading_messages(
            paper, text, truncated=truncated, focus=focus
        )
        _set_job(job_uid, "running", "模型生成中…")

        buffer: list[str] = []
        state = {"flushed": 0}

        def on_delta(chunk: str) -> None:
            buffer.append(chunk)
            total = sum(len(part) for part in buffer)
            if total - state["flushed"] >= 400:
                state["flushed"] = total
                _write_partial(draft_uid, "".join(buffer), total, job_uid)

        body = llm.chat_stream(config, messages, on_delta=on_delta)
        title = _title_from_markdown(body) or paper["title"] or "未命名解读"

        db.execute(
            "UPDATE drafts SET content=?, title=?, status=?, model=?, error='', "
            "version=version+1, updated_at=? WHERE uid=?",
            (body, title, "ready", config.model, db.now(), draft_uid),
        )
        _set_job(job_uid, "done", f"完成，共 {len(body)} 字")
        db.log_event(draft_uid, "generate:done", f"{len(body)} 字 / {config.model}")

        # 解读完成后自动排队抽取创新点；失败不影响解读本身
        try:
            start_insights(draft_uid)
        except Exception:
            pass

    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        db.execute(
            "UPDATE drafts SET status=?, error=?, updated_at=? WHERE uid=?",
            ("failed", message, db.now(), draft_uid),
        )
        _set_job(job_uid, "failed", message)
        db.log_event(draft_uid, "generate:failed", message)
    finally:
        with _LOCK:
            _RUNNING.discard(draft_uid)


def _run_article(
    job_uid: str,
    reading_uid: str,
    article_uid: str,
    engine: str,
    angle: str,
) -> None:
    try:
        reading = db.one("SELECT * FROM drafts WHERE uid=?", (reading_uid,))
        if reading is None or not (reading["content"] or "").strip():
            raise RuntimeError("解读正文为空，先生成或粘贴解读内容")
        paper = None
        if reading["paper_uid"]:
            paper = db.one("SELECT * FROM papers WHERE uid=?", (reading["paper_uid"],))

        db.execute(
            "UPDATE drafts SET status='running', updated_at=? WHERE uid=?",
            (db.now(), article_uid),
        )

        if engine == "dsh":
            body = prompts.build_dsh_article_task(reading, paper, angle=angle)
            task_path = db.TASKS_DIR / f"{article_uid}.md"
            task_path.write_text(body, encoding="utf-8")
            db.execute(
                "UPDATE drafts SET status=?, error='', updated_at=? WHERE uid=?",
                ("awaiting", db.now(), article_uid),
            )
            _set_job(job_uid, "handed_over", f"任务已生成：{task_path.name}")
            return

        settings = db.get_settings()
        config = llm.config_from_settings(settings)
        article = db.one("SELECT preset FROM drafts WHERE uid=?", (article_uid,)) or {}
        messages = prompts.build_article_messages(
            reading, paper, angle=angle, preset=article.get("preset") or "deep"
        )
        _set_job(job_uid, "running", "正在改写正文…")

        buffer: list[str] = []
        state = {"flushed": 0}

        def on_delta(chunk: str) -> None:
            buffer.append(chunk)
            total = sum(len(part) for part in buffer)
            if total - state["flushed"] >= 400:
                state["flushed"] = total
                _write_partial(article_uid, "".join(buffer), total, job_uid)

        body = llm.chat_stream(config, messages, on_delta=on_delta)
        title = _title_from_markdown(body) or reading["title"]
        db.execute(
            "UPDATE drafts SET content=?, title=?, model=?, version=version+1, "
            "error='', updated_at=? WHERE uid=?",
            (body, title, config.model, db.now(), article_uid),
        )
        _set_job(job_uid, "running", f"正文完成 {len(body)} 字，正在生成发布物料…")

        meta, warning = _build_package(config, body, title)
        db.execute(
            "UPDATE drafts SET meta_json=?, status=?, error=?, updated_at=? WHERE uid=?",
            (
                json.dumps(meta, ensure_ascii=False),
                "review",
                warning,
                db.now(),
                article_uid,
            ),
        )
        _set_job(job_uid, "done", f"完成：正文 {len(body)} 字，标题候选 {len(meta['titles'])} 个")
        db.log_event(article_uid, "article:done", f"{len(body)} 字")

    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        db.execute(
            "UPDATE drafts SET status=?, error=?, updated_at=? WHERE uid=?",
            ("failed", message, db.now(), article_uid),
        )
        _set_job(job_uid, "failed", message)
        db.log_event(article_uid, "article:failed", message)
    finally:
        with _LOCK:
            _RUNNING.discard(article_uid)


def _hand_over_to_dsh(
    job_uid: str,
    draft_uid: str,
    paper: dict[str, Any],
    *,
    truncated: bool,
    focus: str,
) -> None:
    """生成 DSH 任务文件，把执行权交给会话。"""
    task_body = prompts.build_dsh_task(paper, truncated=truncated, focus=focus)
    task_path = db.TASKS_DIR / f"{draft_uid}.md"
    task_path.write_text(task_body, encoding="utf-8")

    db.execute(
        "UPDATE drafts SET status=?, engine=?, error='', updated_at=? WHERE uid=?",
        ("awaiting", "dsh", db.now(), draft_uid),
    )
    _set_job(job_uid, "handed_over", f"任务已生成：{task_path.name}")
    db.log_event(draft_uid, "generate:handover", str(task_path))


# ---------------------------------------------------------------- 榜单盘点


def start_roundup(
    board: dict[str, Any],
    repos: list[dict[str, Any]],
    *,
    engine: str = "builtin",
) -> str:
    """榜单里挑出的项目 → 盘点文章草稿。返回 article draft uid。"""
    if not repos:
        raise ValueError("没有选中任何项目")

    article_uid = db.new_uid("article")
    timestamp = db.now()
    title = f"{board['name']}｜Top {len(repos)} 盘点"
    db.execute(
        "INSERT INTO drafts(uid, paper_uid, kind, title, status, engine, preset, "
        "parent_uid, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            article_uid,
            "",
            "article",
            title,
            "queued",
            engine,
            "roundup",
            "",
            timestamp,
            timestamp,
        ),
    )
    job_uid = db.new_uid("job")
    db.execute(
        "INSERT INTO jobs(uid, kind, paper_uid, draft_uid, status, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_uid, "roundup", "", article_uid, "queued", "排队中", timestamp),
    )
    db.log_event(article_uid, "roundup:queued", f"{board['key']} / {len(repos)} 项")

    with _LOCK:
        _RUNNING.add(article_uid)
    _ensure_worker()
    _QUEUE.put(
        (
            "roundup",
            job_uid,
            {
                "article_uid": article_uid,
                "board": {
                    "key": board["key"],
                    "name": board["name"],
                    "query": board.get("query", ""),
                    "note": board.get("note", ""),
                },
                "repos": repos,
            },
        )
    )
    return article_uid


def start_weekly(
    board: dict[str, Any],
    github_items: list[dict[str, Any]],
    paper_items: list[dict[str, Any]],
) -> str:
    """本周涨星 + 本周新论文 → 周资讯文章。返回 article draft uid。"""
    if not github_items and not paper_items:
        raise ValueError("本周没有可用素材（先刷新榜单或抓取雷达）")

    article_uid = db.new_uid("article")
    timestamp = db.now()
    title = f"{board['name']}｜本周资讯"
    db.execute(
        "INSERT INTO drafts(uid, paper_uid, kind, title, status, engine, preset, "
        "parent_uid, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (article_uid, "", "article", title, "queued", "builtin", "weekly", "", timestamp, timestamp),
    )
    job_uid = db.new_uid("job")
    db.execute(
        "INSERT INTO jobs(uid, kind, paper_uid, draft_uid, status, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_uid, "weekly", "", article_uid, "queued", "排队中", timestamp),
    )
    db.log_event(
        article_uid, "weekly:queued", f"{len(github_items)} 个仓库 / {len(paper_items)} 篇论文"
    )

    with _LOCK:
        _RUNNING.add(article_uid)
    _ensure_worker()
    _QUEUE.put(
        (
            "weekly",
            job_uid,
            {
                "article_uid": article_uid,
                "board": {
                    "key": board["key"],
                    "name": board["name"],
                    "query": board.get("query", ""),
                },
                "github": github_items,
                "papers": paper_items,
            },
        )
    )
    return article_uid


def _run_weekly(
    job_uid: str,
    article_uid: str,
    board: dict[str, Any],
    github_items: list[dict[str, Any]],
    paper_items: list[dict[str, Any]],
) -> None:
    try:
        messages = prompts.build_weekly_messages(
            board["name"], github_items, paper_items, board_query=board.get("query", "")
        )
        _run_generated(
            job_uid,
            article_uid,
            messages,
            fallback_title=f"{board['name']}｜本周资讯",
            log_tag="weekly",
        )
    except Exception as exc:
        _fail(job_uid, article_uid, exc, "weekly")
    finally:
        with _LOCK:
            _RUNNING.discard(article_uid)


def _run_generated(
    job_uid: str,
    article_uid: str,
    messages: list[dict[str, str]],
    *,
    fallback_title: str,
    log_tag: str,
) -> None:
    """正文流式生成 + 发布物料。盘点与周资讯共用这条路径。"""
    db.execute(
        "UPDATE drafts SET status='running', updated_at=? WHERE uid=?",
        (db.now(), article_uid),
    )
    settings = db.get_settings()
    config = llm.config_from_settings(settings)
    _set_job(job_uid, "running", "正在生成正文…")

    buffer: list[str] = []
    state = {"flushed": 0}

    def on_delta(chunk: str) -> None:
        buffer.append(chunk)
        total = sum(len(part) for part in buffer)
        if total - state["flushed"] >= 400:
            state["flushed"] = total
            _write_partial(article_uid, "".join(buffer), total, job_uid)

    body = llm.chat_stream(config, messages, on_delta=on_delta)
    title = _title_from_markdown(body) or fallback_title
    db.execute(
        "UPDATE drafts SET content=?, title=?, model=?, version=version+1, "
        "error='', updated_at=? WHERE uid=?",
        (body, title, config.model, db.now(), article_uid),
    )
    _set_job(job_uid, "running", f"正文完成 {len(body)} 字，正在生成发布物料…")

    meta, warning = _build_package(config, body, title)
    db.execute(
        "UPDATE drafts SET meta_json=?, status=?, error=?, updated_at=? WHERE uid=?",
        (json.dumps(meta, ensure_ascii=False), "review", warning, db.now(), article_uid),
    )
    _set_job(job_uid, "done", f"完成：{len(body)} 字，标题候选 {len(meta['titles'])} 个")
    db.log_event(article_uid, f"{log_tag}:done", f"{len(body)} 字")


def _fail(job_uid: str, article_uid: str, exc: Exception, log_tag: str) -> None:
    message = str(exc) or exc.__class__.__name__
    db.execute(
        "UPDATE drafts SET status=?, error=?, updated_at=? WHERE uid=?",
        ("failed", message, db.now(), article_uid),
    )
    _set_job(job_uid, "failed", message)
    db.log_event(article_uid, f"{log_tag}:failed", message)


def _run_roundup(
    job_uid: str,
    article_uid: str,
    board: dict[str, Any],
    repos: list[dict[str, Any]],
) -> None:
    try:
        messages = prompts.build_roundup_messages(
            board["name"],
            repos,
            board_query=board.get("query", ""),
            note=board.get("note", ""),
        )
        _run_generated(
            job_uid,
            article_uid,
            messages,
            fallback_title=f"{board['name']} 盘点",
            log_tag="roundup",
        )
    except Exception as exc:
        _fail(job_uid, article_uid, exc, "roundup")
    finally:
        with _LOCK:
            _RUNNING.discard(article_uid)


# ---------------------------------------------------------------- 知识库抽取


def start_insights(draft_uid: str, *, force: bool = False) -> str | None:
    """把解读送去做创新点抽取。已抽过且非强制时直接返回 None。"""
    draft = db.one("SELECT * FROM drafts WHERE uid=?", (draft_uid,))
    if draft is None or not (draft["content"] or "").strip():
        raise ValueError("解读正文为空，无法抽取创新点")
    if not force and draft.get("extract_skip"):
        return None
    if not force and knowledge.count_for_draft(draft_uid) > 0:
        return None

    job_uid = db.new_uid("job")
    timestamp = db.now()
    db.execute(
        "INSERT INTO jobs(uid, kind, paper_uid, draft_uid, status, message, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_uid, "insights", draft["paper_uid"], draft_uid, "queued", "排队中", timestamp),
    )
    db.log_event(draft_uid, "insights:queued", "auto" if not force else "manual")
    _ensure_worker()
    _QUEUE.put(("insights", job_uid, {"draft_uid": draft_uid}))
    return job_uid


def _run_insights(job_uid: str, draft_uid: str) -> None:
    try:
        draft = db.one("SELECT * FROM drafts WHERE uid=?", (draft_uid,))
        if draft is None:
            raise RuntimeError("解读不存在")
        paper = None
        if draft["paper_uid"]:
            paper = db.one("SELECT * FROM papers WHERE uid=?", (draft["paper_uid"],))

        _set_job(job_uid, "running", "正在抽取创新点…")
        config = llm.config_from_settings(db.get_settings())
        messages = knowledge.build_messages(draft, paper)
        raw = llm.chat_once(config, messages)
        items = knowledge.parse_insights(raw)
        if not items:
            raise RuntimeError("模型没有返回可解析的创新点（可能输出格式跑偏）")

        theme = (paper or {}).get("theme", "") or ""
        count = knowledge.replace_insights(draft, items, theme=theme)
        _set_job(job_uid, "done", f"抽出 {count} 条创新点，待确认")
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        _set_job(job_uid, "failed", message)
        db.log_event(draft_uid, "insights:failed", message)


# ---------------------------------------------------------------- 物料与工具


def repackage(article_uid: str) -> None:
    """只重跑发布物料，不动正文。"""
    article = db.one("SELECT * FROM drafts WHERE uid=?", (article_uid,))
    if article is None or not (article["content"] or "").strip():
        raise RuntimeError("正文为空，无法生成物料")
    settings = db.get_settings()
    config = llm.config_from_settings(settings)
    meta, warning = _build_package(config, article["content"], article["title"])
    db.execute(
        "UPDATE drafts SET meta_json=?, status=?, error=?, updated_at=? WHERE uid=?",
        (
            json.dumps(meta, ensure_ascii=False),
            "review",
            warning,
            db.now(),
            article_uid,
        ),
    )


def _build_package(config: llm.LLMConfig, body: str, title: str) -> tuple[dict[str, Any], str]:
    """调用模型生成标题候选/摘要/封面/标签；失败不阻断正文。"""
    try:
        raw = llm.chat_once(config, prompts.build_package_messages(body, title_hint=title))
    except llm.LLMError as exc:
        return packaging.empty_package(), f"发布物料生成失败：{exc}"
    meta = packaging.parse_package(raw)
    if not meta["titles"]:
        return meta, "发布物料解析失败，可点「重新生成物料」重试"
    return meta, ""


def _write_partial(draft_uid: str, content: str, total: int, job_uid: str) -> None:
    db.execute(
        "UPDATE drafts SET content=?, updated_at=? WHERE uid=?",
        (content, db.now(), draft_uid),
    )
    _set_job(job_uid, "running", f"已生成 {total} 字")


def _set_job(job_uid: str, status: str, message: str) -> None:
    if status in {"done", "failed", "handed_over"}:
        db.execute(
            "UPDATE jobs SET status=?, message=?, error=?, finished_at=? WHERE uid=?",
            (status, message, message if status == "failed" else "", db.now(), job_uid),
        )
        return
    db.execute(
        "UPDATE jobs SET status=?, message=?, started_at=CASE WHEN started_at='' "
        "THEN ? ELSE started_at END WHERE uid=?",
        (status, message, db.now(), job_uid),
    )


def _title_from_markdown(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()[:200]
        if stripped and not stripped.startswith(">"):
            return stripped[:200]
    return ""


def latest_job(draft_uid: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT * FROM jobs WHERE draft_uid=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (draft_uid,),
    )
