"""SQLite 持久化层：表结构、连接、通用查询。

设计原则：
- 单文件库，WAL 模式，允许后台作业线程与请求线程并发读写。
- 所有时间戳为本地时区 ISO 字符串，便于直接显示。
- 版本号用于稿件回退（终审退回或批量重跑时回到上一版）。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

WORKBENCH_DIR = Path(__file__).resolve().parent
DATA_DIR = WORKBENCH_DIR / "data"
PAPERS_DIR = DATA_DIR / "papers"
EXPORT_DIR = DATA_DIR / "exports"
TASKS_DIR = DATA_DIR / "dsh-tasks"
ASSETS_DIR = DATA_DIR / "assets"
DB_PATH = DATA_DIR / "workbench.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    uid         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    filename    TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    pages       INTEGER NOT NULL DEFAULT 0,
    char_count  INTEGER NOT NULL DEFAULT 0,
    venue       TEXT NOT NULL DEFAULT '',
    theme       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'import',
    status      TEXT NOT NULL DEFAULT 'parsed',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_papers_path ON papers(path);

CREATE TABLE IF NOT EXISTS drafts (
    uid         TEXT PRIMARY KEY,
    paper_uid   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'reading',
    title       TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'empty',
    engine      TEXT NOT NULL DEFAULT 'builtin',
    model       TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 1,
    source_path TEXT NOT NULL DEFAULT '',
    parent_uid  TEXT NOT NULL DEFAULT '',
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_paper ON drafts(paper_uid, kind);

CREATE TABLE IF NOT EXISTS jobs (
    uid         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    paper_uid   TEXT NOT NULL DEFAULT '',
    draft_uid   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    message     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_draft ON jobs(draft_uid, created_at);

CREATE TABLE IF NOT EXISTS assets (
    uid         TEXT PRIMARY KEY,
    draft_uid   TEXT NOT NULL DEFAULT '',
    paper_uid   TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'figure',
    path        TEXT NOT NULL DEFAULT '',
    caption     TEXT NOT NULL DEFAULT '',
    anchor      TEXT NOT NULL DEFAULT '',
    page_no     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    uid         TEXT PRIMARY KEY,
    subject     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS candidates (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL DEFAULT 'arxiv',
    external_id TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    authors     TEXT NOT NULL DEFAULT '',
    abstract    TEXT NOT NULL DEFAULT '',
    published   TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    pdf_url     TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    categories  TEXT NOT NULL DEFAULT '',
    keyword     TEXT NOT NULL DEFAULT '',
    score       INTEGER NOT NULL DEFAULT 0,
    reasons     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',
    paper_uid   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_source_ext ON candidates(source, external_id);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status, score);

CREATE TABLE IF NOT EXISTS boards (
    uid             TEXT PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    query           TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    per_page        INTEGER NOT NULL DEFAULT 30,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_fetch      TEXT NOT NULL DEFAULT '',
    total_count     INTEGER NOT NULL DEFAULT 0,
    rate_remaining  INTEGER NOT NULL DEFAULT -1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS board_entries (
    uid         TEXT PRIMARY KEY,
    board_key   TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    full_name   TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    stars       INTEGER NOT NULL DEFAULT 0,
    prev_stars  INTEGER NOT NULL DEFAULT 0,
    forks       INTEGER NOT NULL DEFAULT 0,
    language    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    topics      TEXT NOT NULL DEFAULT '',
    pushed_at   TEXT NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_board_entries ON board_entries(board_key, rank);

-- 快照历史：每次刷新追加一行（按日去重），周涨幅靠它算。
-- board_entries 只存"当前"，没有历史就算不出「本周涨了多少」。
CREATE TABLE IF NOT EXISTS board_history (
    uid           TEXT PRIMARY KEY,
    board_key     TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    stars         INTEGER NOT NULL DEFAULT 0,
    captured_date TEXT NOT NULL,
    captured_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_board_history
    ON board_history(board_key, full_name, captured_date);
CREATE INDEX IF NOT EXISTS idx_board_history_lookup
    ON board_history(board_key, captured_date);

-- ---------------------------------------------------------------- 科研知识库

-- 一条 insight = 解读里的一个模块级创新点
CREATE TABLE IF NOT EXISTS insights (
    uid           TEXT PRIMARY KEY,
    paper_uid     TEXT NOT NULL DEFAULT '',
    draft_uid     TEXT NOT NULL DEFAULT '',
    draft_version INTEGER NOT NULL DEFAULT 0,
    ordinal       INTEGER NOT NULL DEFAULT 0,
    title         TEXT NOT NULL,
    problem       TEXT NOT NULL DEFAULT '',
    mechanism     TEXT NOT NULL DEFAULT '',
    novelty_type  TEXT NOT NULL DEFAULT '',
    applies_to    TEXT NOT NULL DEFAULT '',
    limitation    TEXT NOT NULL DEFAULT '',
    evidence      TEXT NOT NULL DEFAULT '',
    theme         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_insights_draft_ordinal ON insights(draft_uid, ordinal);
CREATE INDEX IF NOT EXISTS idx_insights_status ON insights(status, created_at);

-- 机制卡：受控词表，verified=0 表示由模型提议、待人工确认
CREATE TABLE IF NOT EXISTS mechanisms (
    uid         TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    definition  TEXT NOT NULL DEFAULT '',
    verified    INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insight_mechanisms (
    insight_uid   TEXT NOT NULL,
    mechanism_uid TEXT NOT NULL,
    variant_note  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (insight_uid, mechanism_uid)
);
CREATE INDEX IF NOT EXISTS idx_insight_mech ON insight_mechanisms(mechanism_uid);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "llm_api_key": "",
    "llm_model": "qwen-plus",
    "llm_temperature": "0.3",
    "llm_max_tokens": "8192",
    "max_paper_chars": "120000",
    "radar_keywords": (
        "surface defect detection\n"
        "anomaly detection\n"
        "camouflaged object detection\n"
        "zero-shot anomaly segmentation"
    ),
    "radar_per_keyword": "15",
    "radar_max_age_days": "1095",
    # 摘要评分两次调用之间的间隔（秒）。实测突发约 4 次、之后约 1 次/5 秒，
    # 留余量取 8 秒；撞到 429 时作业会退避重试
    "radar_score_interval": "8",
    "radar_last_fetch": "",
    "github_token": "",
}


def now() -> str:
    """当前本地时间，秒级 ISO 字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    # 大 UPDATE 不再依赖系统临时目录（那条路径出过 CANTOPEN）
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    for directory in (DATA_DIR, PAPERS_DIR, EXPORT_DIR, TASKS_DIR, ASSETS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        # 索引依赖新增列，必须在补列之后创建
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_drafts_parent ON drafts(parent_uid, kind)"
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
    seed_boards()
    seed_mechanisms()


def seed_mechanisms() -> None:
    """写入受控机制词表（幂等）。延迟导入避免循环依赖。"""
    import knowledge

    knowledge.ensure_mechanisms()


def seed_boards() -> None:
    """首次启动写入预置榜单；已存在的 key 不覆盖（用户可能改过查询式）。"""
    import github_boards

    existing = {row["key"] for row in query("SELECT key FROM boards")}
    timestamp = now()
    for spec in github_boards.DEFAULT_BOARDS:
        if spec["key"] in existing:
            continue
        execute(
            "INSERT INTO boards(uid, key, name, query, note, per_page, enabled, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                new_uid("board"),
                spec["key"],
                spec["name"],
                spec["query"],
                spec.get("note", ""),
                int(spec.get("per_page", 30)),
                1,
                timestamp,
                timestamp,
            ),
        )


# 增量列：老库不需要重建，启动时补齐即可
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("drafts", "parent_uid", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "meta_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("papers", "selected", "INTEGER NOT NULL DEFAULT 0"),
    ("papers", "note", "TEXT NOT NULL DEFAULT ''"),
    ("assets", "page_no", "INTEGER NOT NULL DEFAULT 0"),
    ("drafts", "theme", "TEXT NOT NULL DEFAULT 'classic'"),
    ("drafts", "preset", "TEXT NOT NULL DEFAULT 'deep'"),
    ("insights", "evidence_ok", "INTEGER NOT NULL DEFAULT 0"),
    ("insights", "evidence_source", "TEXT NOT NULL DEFAULT ''"),
    ("insights", "evidence_score", "REAL NOT NULL DEFAULT 0"),
    # 待抽取队列里的「忽略」：跳过这篇，不再自动抽取
    ("drafts", "extract_skip", "INTEGER NOT NULL DEFAULT 0"),
    # 机制英文名：中文译名常是模型意译，英文原名才准
    ("mechanisms", "name_en", "TEXT NOT NULL DEFAULT ''"),
    # 期刊出处：参会的写在 comment 里，期刊的写在 journal_ref 里
    ("candidates", "journal_ref", "TEXT NOT NULL DEFAULT ''"),
    # 作者单位：arXiv 不给，抓取时走 OpenAlex 补，顿号分隔
    ("candidates", "institutions", "TEXT NOT NULL DEFAULT ''"),
    # 加权分拆解：摘要分（模型，60%）、元数据分（40%）、维度明细；-1 表示还没评
    ("candidates", "content_score", "REAL NOT NULL DEFAULT -1"),
    # 摘要分标准化后的值（4–9 拉伸到 0–10），最终加权用这个
    ("candidates", "content_norm", "REAL NOT NULL DEFAULT -1"),
    ("candidates", "meta_score", "REAL NOT NULL DEFAULT 0"),
    ("candidates", "score_detail", "TEXT NOT NULL DEFAULT ''"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = _retry(lambda conn: conn.execute(sql, params).fetchall())
    return [dict(row) for row in rows]


def one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    _retry(lambda conn: conn.execute(sql, params))


def _retry(action: Callable[[sqlite3.Connection], Any], *, attempts: int = 3) -> Any:
    """执行一次读写；遇到 WAL 半损坏类的 CANTOPEN 时重连重试。

    服务被强杀后，SQLite 偶尔会留下不完整的 -wal/-shm，第一次写入报
    `unable to open database file`，重连一次即可自愈。重试只针对这一种错误，
    其它异常照常抛出，不掩盖真实问题。
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with session() as conn:
                return action(conn)
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc):
                raise
            last = exc
            time.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise last


def get_settings() -> dict[str, str]:
    rows = query("SELECT key, value FROM settings")
    merged = dict(DEFAULT_SETTINGS)
    merged.update({row["key"]: row["value"] for row in rows})
    return merged


def save_settings(values: dict[str, str]) -> None:
    with session() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )


def log_event(subject: str, action: str, detail: str = "") -> None:
    execute(
        "INSERT INTO events(uid, subject, action, detail, created_at) VALUES(?,?,?,?,?)",
        (new_uid("ev"), subject, action, detail, now()),
    )
