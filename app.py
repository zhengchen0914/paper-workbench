"""论文工坊 —— 本地网页工作台（M1）。

流水线：论文 PDF → 结构化解读 → （M2 起）公众号文章 → 终审 → 导出。

本文件只做三件事：路由、表单处理、模板渲染。
业务逻辑在 jobs（作业）、llm（模型）、pdfx（解析）、prompts（提示词）里。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import markdown as mdlib
import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import db
import export
import github_boards
import jobs
import knowledge
import llm
import packaging
import pdfx
import prompts
import radar
import themes
import verify
import version

WORKBENCH_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = WORKBENCH_DIR.parent
TEMPLATES_DIR = WORKBENCH_DIR / "templates"
STATIC_DIR = WORKBENCH_DIR / "static"
DONE_DIR = WORKSPACE_DIR / "已完成"
NOTES_DIR = WORKSPACE_DIR / "论文解读"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="论文工坊", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
db.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=db.ASSETS_DIR), name="assets")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STATUS_LABELS = {
    "empty": "待生成",
    "queued": "排队中",
    "running": "生成中",
    "ready": "已完成",
    "failed": "失败",
    "awaiting": "待 DSH 产出",
    "review": "待终审",
    "approved": "已通过",
}

ENGINE_LABELS = {"builtin": "内置模型", "dsh": "交给 DSH"}


def trace_for(content: str, paper: dict[str, Any] | None) -> dict[str, Any] | None:
    """生成稿 vs 原始 PDF 的数字溯源。没有 PDF 或没有正文就返回 None。"""
    if not paper or not (content or "").strip():
        return None
    try:
        source = pdfx.source_text(paper["path"])
    except pdfx.PDFError:
        return None
    return verify.trace_numbers(content, source)


def render(request: Request, name: str, **context: Any) -> HTMLResponse:
    base = {
        "status_labels": STATUS_LABELS,
        "engine_labels": ENGINE_LABELS,
        "version": version.__version__,
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base)


def markdown_to_html(text: str) -> Markup:
    if not text.strip():
        return Markup("<p class='empty-hint'>还没有内容</p>")
    html = mdlib.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )
    return Markup(html)


def papers_overview() -> list[dict[str, Any]]:
    """论文 + 它对应的解读稿件。"""
    rows = db.query(
        "SELECT p.*, d.uid AS draft_uid, d.status AS draft_status, d.title AS draft_title, "
        "d.content AS draft_content, d.error AS draft_error "
        "FROM papers p LEFT JOIN drafts d ON d.paper_uid = p.uid AND d.kind = 'reading' "
        "ORDER BY p.created_at DESC, p.rowid DESC"
    )
    for row in rows:
        row["word_count"] = len((row.get("draft_content") or "").strip())
    return rows


def orphan_drafts() -> list[dict[str, Any]]:
    """从既有 Markdown 导入的解读（没有对应 PDF 记录）。"""
    return db.query(
        "SELECT * FROM drafts WHERE paper_uid='' AND kind='reading' "
        "ORDER BY created_at DESC, rowid DESC"
    )


# ---------------------------------------------------------------- 看板


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    counts = {
        "papers": (db.one("SELECT COUNT(*) AS n FROM papers") or {"n": 0})["n"],
        "reading": (
            db.one("SELECT COUNT(*) AS n FROM drafts WHERE kind='reading' AND status='ready'")
            or {"n": 0}
        )["n"],
        "review": (
            db.one("SELECT COUNT(*) AS n FROM drafts WHERE kind='article' AND status='review'")
            or {"n": 0}
        )["n"],
        "running": (
            db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='running'") or {"n": 0}
        )["n"],
        "failed": (
            db.one("SELECT COUNT(*) AS n FROM drafts WHERE status='failed'") or {"n": 0}
        )["n"],
    }
    recent = db.query(
        "SELECT d.*, p.filename FROM drafts d LEFT JOIN papers p ON p.uid = d.paper_uid "
        "WHERE d.status != 'empty' ORDER BY d.updated_at DESC LIMIT 8"
    )
    settings = db.get_settings()
    configured = bool(settings.get("llm_api_key"))
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        counts=counts,
        recent=recent,
        configured=configured,
        model=settings.get("llm_model", ""),
    )


# ---------------------------------------------------------------- 论文库


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, msg: str = "") -> HTMLResponse:
    return render(
        request,
        "library.html",
        active="library",
        papers=papers_overview(),
        orphans=orphan_drafts(),
        scan_dir=str(DONE_DIR),
        notes_dir=str(NOTES_DIR),
        msg=msg,
    )


@app.post("/library/scan")
def scan_library(directory: str = Form(default="")) -> RedirectResponse:
    target = Path(directory) if directory.strip() else DONE_DIR
    added, skipped = _register_pdfs(target)
    return RedirectResponse(
        f"/library?msg=扫描完成：新增 {added} 篇，跳过已存在 {skipped} 篇", status_code=303
    )


def _register_pdfs(target: Path) -> tuple[int, int]:
    if not target.exists():
        return 0, 0
    added = 0
    skipped = 0
    for pdf_path in sorted(target.glob("*.pdf")):
        existing = db.one("SELECT uid FROM papers WHERE path=?", (str(pdf_path),))
        if existing:
            skipped += 1
            continue
        _register_pdf(pdf_path)
        added += 1
    return added, skipped


def _register_pdf(pdf_path: Path, *, source: str = "import") -> str:
    """登记单篇 PDF 并建好空的解读稿件，返回 paper uid。"""
    paper_uid = db.new_uid("paper")
    info: dict[str, Any] = {"pages": 0, "char_count": 0, "title_guess": pdf_path.stem}
    try:
        info = pdfx.extract(pdf_path)
    except pdfx.PDFError:
        pass
    timestamp = db.now()
    db.execute(
        "INSERT INTO papers(uid, title, filename, path, pages, char_count, source, "
        "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            paper_uid,
            info.get("title_guess") or pdf_path.stem,
            pdf_path.name,
            str(pdf_path),
            int(info.get("pages") or 0),
            int(info.get("char_count") or 0),
            source,
            "parsed" if info.get("has_text_layer") else "no_text",
            timestamp,
            timestamp,
        ),
    )
    db.execute(
        "INSERT INTO drafts(uid, paper_uid, kind, title, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            db.new_uid("draft"),
            paper_uid,
            "reading",
            pdf_path.stem,
            "empty",
            timestamp,
            timestamp,
        ),
    )
    return paper_uid


@app.post("/library/upload")
async def upload_pdf(file: UploadFile = File(...)) -> RedirectResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return RedirectResponse("/library?msg=只支持 PDF 文件", status_code=303)
    safe_name = Path(file.filename).name
    target = db.PAPERS_DIR / safe_name
    with target.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    added, _ = _register_pdfs(db.PAPERS_DIR)
    return RedirectResponse(f"/library?msg=上传完成（新增 {added} 篇）", status_code=303)


@app.post("/library/import-notes")
def import_notes(directory: str = Form(default="")) -> RedirectResponse:
    target = Path(directory) if directory.strip() else NOTES_DIR
    if not target.exists():
        return RedirectResponse(f"/library?msg=目录不存在：{target}", status_code=303)
    added = 0
    for note_path in sorted(target.glob("*.md")):
        existing = db.one("SELECT uid FROM drafts WHERE source_path=?", (str(note_path),))
        if existing:
            continue
        content = note_path.read_text(encoding="utf-8")
        title = _title_from_markdown(content) or note_path.stem
        timestamp = db.now()
        db.execute(
            "INSERT INTO drafts(uid, paper_uid, kind, title, content, status, engine, "
            "source_path, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                db.new_uid("draft"),
                "",
                "reading",
                title,
                content,
                "ready",
                "import",
                str(note_path),
                timestamp,
                timestamp,
            ),
        )
        added += 1
    return RedirectResponse(f"/library?msg=已导入既有解读 {added} 篇", status_code=303)


def _title_from_markdown(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()[:200]
    return ""


# ---------------------------------------------------------------- 稿件台


@app.get("/draft/{uid}", response_class=HTMLResponse)
def draft_page(request: Request, uid: str) -> Response:
    draft = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if draft is None:
        return RedirectResponse("/library?msg=稿件不存在", status_code=303)
    paper = None
    if draft["paper_uid"]:
        paper = db.one("SELECT * FROM papers WHERE uid=?", (draft["paper_uid"],))
    task_file = db.TASKS_DIR / f"{uid}.md"
    return render(
        request,
        "draft.html",
        active="library",
        draft=draft,
        paper=paper,
        preview=markdown_to_html(draft["content"]),
        job=jobs.latest_job(uid),
        task_exists=task_file.exists(),
        task_path=str(task_file),
        has_key=bool(db.get_settings().get("llm_api_key")),
        trace=trace_for(draft["content"], paper),
        presets=prompts.preset_options(),
        insight_count=knowledge.count_for_draft(uid),
    )


@app.get("/draft/{uid}/panel", response_class=HTMLResponse)
def draft_panel(request: Request, uid: str) -> Response:
    draft = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if draft is None:
        return HTMLResponse("<p>稿件不存在</p>")
    if draft["status"] != "running":
        # 生成结束：让前端整页刷新一次，把内容灌进编辑框
        return Response(headers={"HX-Refresh": "true"})
    return render(
        request,
        "_panel.html",
        draft=draft,
        preview=markdown_to_html(draft["content"]),
        job=jobs.latest_job(uid),
    )


@app.post("/draft/{uid}/generate")
def generate(uid: str, engine: str = Form(default="builtin"), focus: str = Form(default="")):
    try:
        jobs.start_reading(uid, engine=engine, focus=focus)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return RedirectResponse(f"/draft/{uid}?err={message}", status_code=303)
    return RedirectResponse(f"/draft/{uid}", status_code=303)


@app.post("/draft/{uid}/save")
def save_draft(uid: str, content: str = Form(default=""), title: str = Form(default="")):
    draft = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if draft is None:
        return RedirectResponse("/library?msg=稿件不存在", status_code=303)
    next_title = title.strip() or _title_from_markdown(content) or draft["title"]
    db.execute(
        "UPDATE drafts SET content=?, title=?, status=?, error='', version=version+1, "
        "updated_at=? WHERE uid=?",
        (content, next_title, "ready" if content.strip() else "empty", db.now(), uid),
    )
    db.log_event(uid, "draft:save", f"{len(content)} 字")
    return RedirectResponse(f"/draft/{uid}", status_code=303)


@app.get("/drafts", response_class=HTMLResponse)
def drafts_list(request: Request, kind: str = "reading", msg: str = "", err: str = "") -> HTMLResponse:
    rows = db.query(
        "SELECT d.*, p.filename FROM drafts d LEFT JOIN papers p ON p.uid = d.paper_uid "
        "WHERE d.kind=? AND d.status != 'empty' ORDER BY d.updated_at DESC",
        (kind,),
    )
    return render(
        request, "drafts.html", active="drafts", drafts=rows, kind=kind, msg=msg, err=err
    )


# ---------------------------------------------------------------- 导出到本地文件夹

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_filename(text: str, fallback: str = "draft") -> str:
    cleaned = _BAD_FILENAME.sub("_", (text or "").strip()).strip(" .")
    return (cleaned or fallback)[:80]


def write_draft_file(draft: dict[str, Any]) -> Path:
    """把稿件写成磁盘文件，返回路径。

    解读存 .md（带 BOM，Windows 记事本打开中文不乱码）；
    公众号文章额外存一份自包含 .html，双击就能看排版效果。
    """
    db.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    title = _safe_filename(draft.get("title") or draft["uid"])
    content = draft.get("content") or ""
    if draft.get("kind") == "article":
        body = export.render_fragment(content, draft.get("theme") or "classic")
        page = export.standalone_page(
            title=draft.get("title") or "未命名文章",
            body_html=body,
            meta={"theme": draft.get("theme") or "classic"},
        )
        path = db.EXPORT_DIR / f"{title}.html"
        path.write_text(page, encoding="utf-8")
        return path
    path = db.EXPORT_DIR / f"{title}.md"
    path.write_text(content, encoding="utf-8-sig")
    return path


def reveal_path(path: Path) -> bool:
    """在系统文件管理器里定位到该文件。返回是否成功发起调用。

    注意：`explorer /select,<路径>` 必须让 `/select,` 和引号紧挨着，
    所以用**字符串**命令行，不能用列表形式——列表形式会拼成
    `"/select,C:\a b\f.zip"`，把 /select 一起裹进引号，explorer 直接无视。
    """
    target = str(path.resolve())
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(f'explorer /select,"{target}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except OSError as exc:  # 无桌面环境时别把请求打挂
        print(f"[reveal] 打开文件夹失败: {exc}")
        return False


def _export_dir_hint(path: Path) -> str:
    """给用户的落点说明：即使窗口没弹出来，也能自己找到文件。"""
    return f"已导出：{path}"


def _safe_back(back: str, default: str) -> str:
    """只允许回到站内路径，避免被拼成外链。"""
    return back if back.startswith("/") and not back.startswith("//") else default


def _back_with(back: str, key: str, value: str, default: str = "/drafts") -> str:
    """在回跳地址上追加参数。

    不能用 f"{back}?{key}=..."：back 里可能已经有 ?kind=article，
    拼出 /drafts?kind=article?msg=... 这种坏 URL，提示就丢了。
    """
    target = _safe_back(back, default)
    joiner = "&" if "?" in target else "?"
    return f"{target}{joiner}{key}={quote(value)}"


@app.post("/draft/{uid}/reveal")
def draft_reveal(uid: str, back: str = Form(default="/drafts")):
    draft = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if draft is None:
        return RedirectResponse("/drafts?err=稿件不存在", status_code=303)
    try:
        path = write_draft_file(draft)
    except OSError as exc:
        return RedirectResponse(f"/drafts?err=导出失败：{exc}", status_code=303)
    reveal_path(path)
    return RedirectResponse(
        _back_with(back, "msg", _export_dir_hint(path)),
        status_code=303,
    )


@app.post("/exports/open")
def exports_open(back: str = Form(default="/drafts")):
    db.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    reveal_path(db.EXPORT_DIR)
    return RedirectResponse(_back_with(back, "msg", f"已打开导出文件夹：{db.EXPORT_DIR}"), status_code=303)


# ---------------------------------------------------------------- 公众号文章


@app.post("/draft/{uid}/article")
def create_article(
    uid: str,
    engine: str = Form(default="builtin"),
    angle: str = Form(default=""),
    preset: str = Form(default="deep"),
):
    try:
        article_uid = jobs.start_article(uid, engine=engine, angle=angle, preset=preset)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return RedirectResponse(f"/draft/{uid}?err={message}", status_code=303)
    return RedirectResponse(f"/article/{article_uid}", status_code=303)


def article_bundle(uid: str) -> dict[str, Any] | None:
    article = db.one("SELECT * FROM drafts WHERE uid=? AND kind='article'", (uid,))
    if article is None:
        return None
    reading = None
    if article["parent_uid"]:
        reading = db.one("SELECT * FROM drafts WHERE uid=?", (article["parent_uid"],))
    paper = None
    if article["paper_uid"]:
        paper = db.one("SELECT * FROM papers WHERE uid=?", (article["paper_uid"],))

    try:
        raw_meta = json.loads(article["meta_json"] or "{}")
    except json.JSONDecodeError:
        raw_meta = {}
    meta = {
        "titles": raw_meta.get("titles") or [],
        "summary": raw_meta.get("summary") or "",
        "cover": raw_meta.get("cover") or {"main": "", "sub": ""},
        "tags": raw_meta.get("tags") or [],
    }
    hits = packaging.check_prohibited(article["content"])
    assets = []
    if article["paper_uid"]:
        assets = db.query(
            "SELECT * FROM assets WHERE paper_uid=? ORDER BY page_no, created_at",
            (article["paper_uid"],),
        )
        for asset in assets:
            asset["url"] = f"/assets/{article['paper_uid']}/{Path(asset['path']).name}"
    theme_key = article["theme"] or themes.DEFAULT_THEME
    preset_key, preset_def = prompts.get_preset(article["preset"])
    word_count = len(re.sub(r"\s+", "", article["content"] or ""))
    length_state = "ok"
    if word_count > preset_def["max"] * 1.15:
        length_state = "over"
    elif word_count and word_count < preset_def["min"] * 0.85:
        length_state = "under"
    return {
        "article": article,
        "reading": reading,
        "paper": paper,
        "meta": meta,
        "hits": hits,
        "stats": packaging.summary_stats(hits),
        "preview": markdown_to_html(article["content"]),
        "themed_preview": Markup(export.render_fragment(article["content"], theme_key)),
        "theme": themes.get_theme(theme_key),
        "themes": themes.THEMES,
        "preset_name": preset_def["name"],
        "preset_min": preset_def["min"],
        "preset_max": preset_def["max"],
        "word_count": word_count,
        "length_state": length_state,
        "assets": assets,
        "figure_count": (article["content"] or "").count("🖼️"),
        "trace": trace_for(article["content"], paper),
    }


@app.get("/article/{uid}", response_class=HTMLResponse)
def article_review(request: Request, uid: str, msg: str = "") -> Response:
    bundle = article_bundle(uid)
    if bundle is None:
        return RedirectResponse("/drafts", status_code=303)
    return render(
        request,
        "article.html",
        active="drafts",
        **bundle,
        job=jobs.latest_job(uid),
        has_key=bool(db.get_settings().get("llm_api_key")),
        msg=msg,
    )


@app.get("/article/{uid}/panel", response_class=HTMLResponse)
def article_panel(request: Request, uid: str) -> Response:
    article = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if article is None:
        return HTMLResponse("<p>稿件不存在</p>")
    if article["status"] != "running":
        return Response(headers={"HX-Refresh": "true"})
    return render(
        request,
        "_article_panel.html",
        article=article,
        preview=markdown_to_html(article["content"]),
        job=jobs.latest_job(uid),
    )


@app.post("/article/{uid}/save")
def save_article(uid: str, content: str = Form(default=""), title: str = Form(default="")):
    article = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if article is None:
        return RedirectResponse("/drafts", status_code=303)
    next_title = title.strip() or article["title"]
    db.execute(
        "UPDATE drafts SET content=?, title=?, status=?, version=version+1, "
        "error='', updated_at=? WHERE uid=?",
        (
            content,
            next_title,
            "review" if content.strip() else "empty",
            db.now(),
            uid,
        ),
    )
    db.log_event(uid, "article:save", f"{len(content)} 字")
    return RedirectResponse(f"/article/{uid}", status_code=303)


@app.post("/article/{uid}/repackage")
def repackage_article(uid: str):
    try:
        jobs.repackage(uid)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        db.execute(
            "UPDATE drafts SET error=?, updated_at=? WHERE uid=?", (message, db.now(), uid)
        )
    return RedirectResponse(f"/article/{uid}", status_code=303)


@app.post("/article/{uid}/approve")
def approve_article(uid: str):
    db.execute(
        "UPDATE drafts SET status=?, updated_at=? WHERE uid=?", ("approved", db.now(), uid)
    )
    db.log_event(uid, "article:approve", "")
    return RedirectResponse(f"/article/{uid}", status_code=303)


@app.post("/article/{uid}/reopen")
def reopen_article(uid: str):
    db.execute(
        "UPDATE drafts SET status=?, updated_at=? WHERE uid=?", ("review", db.now(), uid)
    )
    return RedirectResponse(f"/article/{uid}", status_code=303)


@app.post("/article/{uid}/retitle")
def retitle_article(uid: str, title: str = Form(default="")):
    if title.strip():
        db.execute(
            "UPDATE drafts SET title=?, updated_at=? WHERE uid=?",
            (title.strip(), db.now(), uid),
        )
    return RedirectResponse(f"/article/{uid}", status_code=303)


@app.get("/article/{uid}/export", response_class=HTMLResponse)
def export_article(uid: str) -> Response:
    bundle = article_bundle(uid)
    if bundle is None:
        return RedirectResponse("/drafts", status_code=303)
    article = bundle["article"]
    paper = bundle["paper"]
    theme = themes.get_theme(article["theme"])
    html = export.render_fragment(article["content"], theme.key)
    page = export.standalone_page(
        title=article["title"] or "未命名文章",
        body_html=html,
        meta={
            "title": article["title"],
            "venue": (paper or {}).get("venue", ""),
            "model": article["model"],
            "theme": theme.name,
        },
    )
    return HTMLResponse(page)


@app.post("/article/{uid}/theme")
def set_article_theme(uid: str, theme: str = Form(default="classic")):
    if theme not in themes.THEME_MAP:
        return RedirectResponse(f"/article/{uid}?msg=未知主题", status_code=303)
    db.execute(
        "UPDATE drafts SET theme=?, updated_at=? WHERE uid=?", (theme, db.now(), uid)
    )
    db.log_event(uid, "article:theme", theme)
    return RedirectResponse(f"/article/{uid}", status_code=303)


@app.get("/article/{uid}/export.md")
def export_article_markdown(uid: str) -> Response:
    article = db.one("SELECT * FROM drafts WHERE uid=?", (uid,))
    if article is None:
        return RedirectResponse("/drafts", status_code=303)
    payload = article["content"].encode("utf-8")
    filename = _safe_filename(article["title"] or "article") + ".md"
    # 头部只能放 latin-1，中文文件名走 RFC 5987 的 filename*
    disposition = (
        f'attachment; filename="article.md"; filename*=UTF-8\'\'{quote(filename)}'
    )
    return Response(
        payload,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip()
    return cleaned[:80] or "article"


# ---------------------------------------------------------------- 配图与交付包


@app.post("/article/{uid}/figures")
def extract_figures(uid: str):
    bundle = article_bundle(uid)
    if bundle is None or bundle["paper"] is None:
        return RedirectResponse(
            f"/article/{uid}?msg=这篇没有关联 PDF，无法抽取配图", status_code=303
        )
    paper = bundle["paper"]
    out_dir = db.ASSETS_DIR / paper["uid"]
    # 重新抽取前清掉上一轮产物，避免旧的大图继续占空间
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    try:
        images = pdfx.extract_images(paper["path"], out_dir)
    except pdfx.PDFError as exc:
        return RedirectResponse(f"/article/{uid}?msg=抽图失败：{exc}", status_code=303)

    db.execute("DELETE FROM assets WHERE paper_uid=?", (paper["uid"],))
    for image in images:
        db.execute(
            "INSERT INTO assets(uid, draft_uid, paper_uid, kind, path, caption, "
            "page_no, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                db.new_uid("asset"),
                uid,
                paper["uid"],
                "figure",
                image["path"],
                image["caption"],
                int(image.get("page") or 0),
                db.now(),
            ),
        )
    db.log_event(uid, "figures:extract", f"{len(images)} 张")
    return RedirectResponse(
        f"/article/{uid}?msg=已抽取 {len(images)} 张配图候选", status_code=303
    )


@app.get("/article/{uid}/package.zip")
def export_package(uid: str) -> Response:
    bundle = article_bundle(uid)
    if bundle is None:
        return RedirectResponse("/drafts", status_code=303)
    payload, filename = build_package(bundle)
    disposition = (
        f'attachment; filename="package.zip"; filename*=UTF-8\'\'{quote(filename)}'
    )
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


def build_package(bundle: dict[str, Any]) -> tuple[bytes, str]:
    """打包交付物：正文 md/html + 物料.json + 配图。返回 (zip 字节, 文件名)。"""
    article = bundle["article"]
    theme = themes.get_theme(article["theme"])
    body_html = export.render_fragment(article["content"], theme.key)
    page_html = export.standalone_page(
        title=article["title"] or "未命名文章",
        body_html=body_html,
        meta={"title": article["title"], "model": article["model"], "theme": theme.name},
    )
    manifest = {
        "title": article["title"],
        "theme": theme.name,
        "titles": bundle["meta"]["titles"],
        "summary": bundle["meta"]["summary"],
        "cover": bundle["meta"]["cover"],
        "tags": bundle["meta"]["tags"],
        "prohibited_hits": [
            {"word": hit["word"], "level": hit["level"], "count": hit["count"]}
            for hit in bundle["hits"]
        ],
        "figure_count": bundle["figure_count"],
        "model": article["model"],
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle_zip:
        bundle_zip.writestr("正文.md", article["content"])
        bundle_zip.writestr("正文.html", page_html)
        bundle_zip.writestr(
            "物料.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        for asset in bundle["assets"]:
            asset_path = Path(asset["path"])
            if asset_path.exists():
                bundle_zip.write(asset_path, arcname=f"图片/{asset_path.name}")

    filename = _safe_filename(article["title"] or "article") + ".zip"
    return buffer.getvalue(), filename


@app.post("/article/{uid}/export-to-folder")
def article_export_to_folder(uid: str):
    """把交付包写到 data/exports 并打开文件夹——比浏览器静默下载更有反馈。"""
    bundle = article_bundle(uid)
    if bundle is None:
        return RedirectResponse("/drafts?err=文章不存在", status_code=303)
    article = bundle["article"]
    try:
        db.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        payload, filename = build_package(bundle)
        zip_path = db.EXPORT_DIR / filename
        zip_path.write_bytes(payload)
        write_draft_file({**article, "kind": "article"})
    except OSError as exc:
        return RedirectResponse(
            _back_with(f"/article/{uid}", "err", f"导出失败：{exc}"), status_code=303
        )
    reveal_path(zip_path)
    return RedirectResponse(
        _back_with(f"/article/{uid}", "msg", _export_dir_hint(zip_path)),
        status_code=303,
    )


# ---------------------------------------------------------------- 选题雷达（闸门 0）


@app.get("/radar", response_class=HTMLResponse)
def radar_page(request: Request, status: str = "new", msg: str = "") -> HTMLResponse:
    if status == "all":
        rows = db.query(
            "SELECT * FROM candidates ORDER BY score DESC, published DESC"
        )
    else:
        rows = db.query(
            "SELECT * FROM candidates WHERE status=? ORDER BY score DESC, published DESC",
            (status,),
        )
    counts = {
        row["status"]: row["n"]
        for row in db.query("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")
    }
    settings = db.get_settings()
    keywords = [
        line.strip()
        for line in (settings.get("radar_keywords") or "").splitlines()
        if line.strip()
    ]
    return render(
        request,
        "radar.html",
        active="radar",
        candidates=rows,
        status=status,
        counts=counts,
        keywords=keywords,
        last_fetch=settings.get("radar_last_fetch", ""),
        msg=msg,
    )


@app.post("/radar/fetch")
def radar_fetch():
    settings = db.get_settings()
    keywords = [
        line.strip()
        for line in (settings.get("radar_keywords") or "").splitlines()
        if line.strip()
    ]
    if not keywords:
        return RedirectResponse("/settings?msg=先在设置里填雷达关键词", status_code=303)

    try:
        per_keyword = int(float(settings.get("radar_per_keyword") or 15))
        max_age = int(float(settings.get("radar_max_age_days") or 1095))
    except (TypeError, ValueError):
        per_keyword, max_age = 15, 1095

    try:
        found, errors = radar.fetch_candidates(
            keywords, per_keyword=per_keyword, max_age_days=max_age
        )
    except Exception as exc:  # 网络整体不可用时给出明确提示
        return RedirectResponse(f"/radar?msg=抓取失败：{exc}", status_code=303)

    added = updated = 0
    for candidate in found:
        existing = db.one(
            "SELECT uid FROM candidates WHERE source=? AND external_id=?",
            (candidate["source"], candidate["external_id"]),
        )
        reasons = "；".join(candidate["reasons"])
        if existing:
            db.execute(
                "UPDATE candidates SET title=?, authors=?, abstract=?, published=?, "
                "url=?, pdf_url=?, comment=?, categories=?, keyword=?, score=?, "
                "reasons=?, updated_at=? WHERE uid=?",
                (
                    candidate["title"],
                    candidate["authors"],
                    candidate["abstract"],
                    candidate["published"],
                    candidate["url"],
                    candidate["pdf_url"],
                    candidate["comment"],
                    candidate["categories"],
                    candidate["keyword"],
                    candidate["score"],
                    reasons,
                    db.now(),
                    existing["uid"],
                ),
            )
            updated += 1
        else:
            timestamp = db.now()
            db.execute(
                "INSERT INTO candidates(uid, source, external_id, title, authors, abstract, "
                "published, url, pdf_url, comment, journal_ref, categories, keyword, score, "
                "reasons, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    db.new_uid("cand"),
                    candidate["source"],
                    candidate["external_id"],
                    candidate["title"],
                    candidate["authors"],
                    candidate["abstract"],
                    candidate["published"],
                    candidate["url"],
                    candidate["pdf_url"],
                    candidate["comment"],
                    candidate.get("journal_ref", ""),
                    candidate["categories"],
                    candidate["keyword"],
                    candidate["score"],
                    reasons,
                    "new",
                    timestamp,
                    timestamp,
                ),
            )
            added += 1

    db.save_settings({"radar_last_fetch": db.now()})
    db.log_event("radar", "fetch", f"新增 {added} / 更新 {updated}")
    suffix = f"，{len(errors)} 个关键词失败" if errors else ""
    return RedirectResponse(
        f"/radar?msg=抓取完成：新增 {added} 篇，更新 {updated} 篇{suffix}", status_code=303
    )


@app.post("/radar/{uid}/import")
def radar_import(uid: str):
    candidate = db.one("SELECT * FROM candidates WHERE uid=?", (uid,))
    if candidate is None:
        return RedirectResponse("/radar?msg=候选不存在", status_code=303)
    if candidate["status"] == "imported" and candidate["paper_uid"]:
        return RedirectResponse("/radar?msg=这篇已经导入过了&status=imported", status_code=303)

    safe = _safe_filename(candidate["title"])[:60]
    filename = f"arxiv_{candidate['external_id'].replace('/', '_')}_{safe}.pdf"
    target = db.PAPERS_DIR / filename
    try:
        size = radar.download_pdf(candidate["pdf_url"], target)
    except radar.RadarError as exc:
        return RedirectResponse(f"/radar?msg=下载失败：{exc}", status_code=303)
    except OSError as exc:
        return RedirectResponse(f"/radar?msg=写入失败：{exc}", status_code=303)

    existing = db.one("SELECT uid FROM papers WHERE path=?", (str(target),))
    paper_uid = existing["uid"] if existing else _register_pdf(target, source="radar")
    db.execute(
        "UPDATE candidates SET status=?, paper_uid=?, updated_at=? WHERE uid=?",
        ("imported", paper_uid, db.now(), uid),
    )
    db.log_event(uid, "radar:import", f"{size} 字节")
    return RedirectResponse(
        f"/radar?msg=已下载并建档（{size // 1024} KB），可在论文库里开始解读&status=imported",
        status_code=303,
    )


@app.post("/radar/{uid}/dismiss")
def radar_dismiss(uid: str):
    db.execute(
        "UPDATE candidates SET status=?, updated_at=? WHERE uid=?", ("dismissed", db.now(), uid)
    )
    return RedirectResponse("/radar", status_code=303)


@app.post("/radar/{uid}/restore")
def radar_restore(uid: str):
    db.execute(
        "UPDATE candidates SET status=?, updated_at=? WHERE uid=?", ("new", db.now(), uid)
    )
    return RedirectResponse("/radar?status=all", status_code=303)


# ---------------------------------------------------------------- GitHub 榜单


def board_snapshot(key: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM board_entries WHERE board_key=? ORDER BY rank", (key,)
    )
    for row in rows:
        row["delta"] = int(row["stars"]) - int(row["prev_stars"])
    return rows


WEEK_DAYS = 7


def weekly_baseline(board_key: str, days: int = WEEK_DAYS) -> tuple[dict[str, int], str]:
    """取「days 天前」的星数基线。

    返回 (基线表, 口径)。口径：
    - week   有超过 days 天的历史快照，算的是真实周涨幅
    - last   历史不足，退化为「相对上次刷新」
    - none   没有任何历史
    """
    cutoff = (datetime.now().astimezone().date() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT full_name, stars FROM board_history WHERE board_key=? AND captured_date<=? "
        "ORDER BY captured_date DESC",
        (board_key, cutoff),
    )
    baseline: dict[str, int] = {}
    for row in rows:
        baseline.setdefault(row["full_name"], int(row["stars"]))
    if baseline:
        return baseline, "week"

    total = db.one("SELECT COUNT(*) AS n FROM board_history WHERE board_key=?", (board_key,))
    return {}, ("last" if (total and total["n"]) else "none")


def weekly_entries(board_key: str) -> tuple[list[dict[str, Any]], str]:
    """本周涨星榜：按涨幅降序。没有周基线时退化为相对上次刷新。"""
    entries = board_snapshot(board_key)
    baseline, basis = weekly_baseline(board_key)

    for row in entries:
        if basis == "week":
            if row["full_name"] in baseline:
                row["week_delta"] = int(row["stars"]) - baseline[row["full_name"]]
                row["is_new"] = False
            else:
                row["week_delta"] = 0
                row["is_new"] = True
        elif basis == "last":
            row["week_delta"] = row["delta"]
            row["is_new"] = False
        else:
            row["week_delta"] = 0
            row["is_new"] = False

    entries.sort(key=lambda item: (-int(item["week_delta"]), int(item["rank"])))
    for index, row in enumerate(entries, start=1):
        row["weekly_rank"] = index
    return entries, basis


BASIS_LABELS = {
    "week": "相对 7 天前快照（真实周涨幅）",
    "last": "历史不足 7 天，暂用「相对上次刷新」的口径",
    "none": "尚无快照历史，先点一次刷新",
}


@app.get("/boards", response_class=HTMLResponse)
def boards_page(
    request: Request, board: str = "", view: str = "total", msg: str = ""
) -> HTMLResponse:
    boards = db.query("SELECT * FROM boards WHERE enabled=1 ORDER BY rowid")
    current = None
    entries: list[dict[str, Any]] = []
    weekly: list[dict[str, Any]] = []
    basis = "none"
    if boards:
        current = next((item for item in boards if item["key"] == board), boards[0])
        entries = board_snapshot(current["key"])
        weekly, basis = weekly_entries(current["key"])
    token = bool(db.get_settings().get("github_token"))
    week_cutoff = (
        datetime.now().astimezone().date() - timedelta(days=WEEK_DAYS)
    ).isoformat()
    papers_week = db.query(
        "SELECT * FROM candidates WHERE status!='imported' AND published>=? "
        "ORDER BY score DESC, published DESC LIMIT 10",
        (week_cutoff,),
    )
    has_gains = any(
        (row.get("week_delta") or 0) > 0 or row.get("is_new") for row in weekly
    )
    return render(
        request,
        "boards.html",
        active="boards",
        boards=boards,
        current=current,
        entries=entries,
        weekly=weekly,
        basis=basis,
        basis_label=BASIS_LABELS.get(basis, ""),
        has_gains=has_gains,
        view=("weekly" if view == "weekly" else "total"),
        papers_week=papers_week,
        week_cutoff=week_cutoff,
        has_token=token,
        msg=msg,
    )


@app.post("/boards/{key}/refresh")
def boards_refresh(key: str):
    board = db.one("SELECT * FROM boards WHERE key=?", (key,))
    if board is None:
        return RedirectResponse("/boards?msg=榜单不存在", status_code=303)

    settings = db.get_settings()
    previous = {
        row["full_name"]: int(row["stars"])
        for row in db.query(
            "SELECT full_name, stars FROM board_entries WHERE board_key=?", (key,)
        )
    }
    try:
        result = github_boards.fetch_board(
            board["query"],
            per_page=int(board["per_page"] or 30),
            token=settings.get("github_token", ""),
        )
    except github_boards.RateLimitError as exc:
        return RedirectResponse(
            f"/boards?board={key}&msg=触发限流：{exc}，{exc.wait_hint()}", status_code=303
        )
    except github_boards.GitHubError as exc:
        return RedirectResponse(f"/boards?board={key}&msg=抓取失败：{exc}", status_code=303)

    db.execute("DELETE FROM board_entries WHERE board_key=?", (key,))
    today = datetime.now().astimezone().date().isoformat()
    for rank, item in enumerate(result.items, start=1):
        db.execute(
            "INSERT INTO board_entries(uid, board_key, rank, full_name, url, stars, "
            "prev_stars, forks, language, description, topics, pushed_at, fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                db.new_uid("entry"),
                key,
                rank,
                item["full_name"],
                item["url"],
                item["stars"],
                previous.get(item["full_name"], item["stars"]),
                item["forks"],
                item["language"],
                item["description"],
                item["topics"],
                item["pushed_at"],
                result.fetched_at,
            ),
        )
        # 同日重复刷新只保留最后一次，避免同一天多条快照把周涨幅算歪
        db.execute(
            "INSERT INTO board_history(uid, board_key, full_name, stars, captured_date, "
            "captured_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(board_key, full_name, captured_date) DO UPDATE SET "
            "stars=excluded.stars, captured_at=excluded.captured_at",
            (
                db.new_uid("hist"),
                key,
                item["full_name"],
                item["stars"],
                today,
                result.fetched_at,
            ),
        )
    db.execute(
        "UPDATE boards SET last_fetch=?, total_count=?, rate_remaining=?, updated_at=? "
        "WHERE key=?",
        (
            result.fetched_at,
            result.total_count,
            result.rate_remaining if result.rate_remaining is not None else -1,
            db.now(),
            key,
        ),
    )
    db.log_event(key, "board:refresh", f"{len(result.items)} 项")
    quota = (
        f"，剩余配额 {result.rate_remaining}/{result.rate_limit}"
        if result.rate_remaining is not None
        else ""
    )
    return RedirectResponse(
        f"/boards?board={key}&msg=已刷新 {len(result.items)} 项，命中总数 "
        f"{result.total_count:,}{quota}",
        status_code=303,
    )


@app.post("/boards/new")
def boards_new(
    name: str = Form(default=""),
    query: str = Form(default=""),
    note: str = Form(default=""),
    per_page: str = Form(default="30"),
):
    name = name.strip()
    query = query.strip()
    if not name or not query:
        return RedirectResponse("/boards?msg=名称和查询式都不能为空", status_code=303)
    key = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:24] or db.new_uid("b")[:10]
    if db.one("SELECT key FROM boards WHERE key=?", (key,)):
        key = f"{key}-{db.new_uid('x')[:4]}"
    try:
        page = max(1, min(int(float(per_page or 30)), 100))
    except (TypeError, ValueError):
        page = 30
    timestamp = db.now()
    db.execute(
        "INSERT INTO boards(uid, key, name, query, note, per_page, enabled, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (db.new_uid("board"), key, name, query, note.strip(), page, 1, timestamp, timestamp),
    )
    return RedirectResponse(f"/boards?board={key}&msg=已添加榜单，点刷新抓取", status_code=303)


@app.post("/boards/{key}/save")
def boards_save(
    key: str,
    name: str = Form(default=""),
    query: str = Form(default=""),
    note: str = Form(default=""),
    per_page: str = Form(default="30"),
):
    board = db.one("SELECT * FROM boards WHERE key=?", (key,))
    if board is None:
        return RedirectResponse("/boards?msg=榜单不存在", status_code=303)
    try:
        page = max(1, min(int(float(per_page or 30)), 100))
    except (TypeError, ValueError):
        page = 30
    db.execute(
        "UPDATE boards SET name=?, query=?, note=?, per_page=?, updated_at=? WHERE key=?",
        (
            name.strip() or board["name"],
            query.strip() or board["query"],
            note.strip(),
            page,
            db.now(),
            key,
        ),
    )
    return RedirectResponse(f"/boards?board={key}&msg=已保存", status_code=303)


@app.post("/boards/{key}/delete")
def boards_delete(key: str):
    db.execute("DELETE FROM board_entries WHERE board_key=?", (key,))
    db.execute("DELETE FROM boards WHERE key=?", (key,))
    return RedirectResponse("/boards?msg=已删除榜单", status_code=303)


@app.get("/boards/{key}/export.md")
def boards_export(key: str) -> Response:
    board = db.one("SELECT * FROM boards WHERE key=?", (key,))
    if board is None:
        return RedirectResponse("/boards?msg=榜单不存在", status_code=303)
    entries = board_snapshot(key)
    lines = [
        f"# {board['name']}",
        "",
        f"> 数据来源：GitHub Search API，按 star 数降序 · 检索式 `{board['query']}`",
        f"> 抓取时间：{board['last_fetch'] or '未抓取'} · 命中总数 {board['total_count']:,}",
        "",
        "| 排名 | 项目 | Star | 语言 | 更新 | 说明 |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for row in entries:
        desc = (row["description"] or "").replace("|", "／").replace("\n", " ")[:60]
        lines.append(
            f"| {row['rank']} | [{row['full_name']}]({row['url']}) | {row['stars']:,} | "
            f"{row['language'] or '-'} | {row['pushed_at']} | {desc} |"
        )
    payload = "\n".join(lines).encode("utf-8")
    filename = f"{_safe_filename(board['name'])}.md"
    disposition = (
        f'attachment; filename="board.md"; filename*=UTF-8\'\'{quote(filename)}'
    )
    return Response(
        payload,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@app.post("/boards/{key}/roundup")
def boards_roundup(key: str, repos: list[str] = Form(default=[])):
    board = db.one("SELECT * FROM boards WHERE key=?", (key,))
    if board is None:
        return RedirectResponse("/boards?msg=榜单不存在", status_code=303)
    if not repos:
        return RedirectResponse(
            f"/boards?board={key}&msg=先勾选要写进文章的项目", status_code=303
        )
    entries = board_snapshot(key)
    picked = [row for row in entries if row["full_name"] in set(repos)]
    picked.sort(key=lambda row: int(row["rank"]))
    try:
        article_uid = jobs.start_roundup(board, picked)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return RedirectResponse(f"/boards?board={key}&msg=创建失败：{message}", status_code=303)
    return RedirectResponse(f"/article/{article_uid}", status_code=303)


@app.post("/boards/{key}/weekly")
def boards_weekly(
    key: str,
    repos: list[str] = Form(default=[]),
    papers: list[str] = Form(default=[]),
):
    """本周涨星 + 本周新论文 → 周资讯文章。"""
    board = db.one("SELECT * FROM boards WHERE key=?", (key,))
    if board is None:
        return RedirectResponse("/boards?msg=榜单不存在", status_code=303)
    if not repos and not papers:
        return RedirectResponse(
            f"/boards?board={key}&view=weekly&msg=至少勾一项素材", status_code=303
        )

    weekly_rows, _ = weekly_entries(key)
    picked_github = [row for row in weekly_rows if row["full_name"] in set(repos)]
    picked_papers: list[dict[str, Any]] = []
    if papers:
        wanted = set(papers)
        picked_papers = [
            row
            for row in db.query("SELECT * FROM candidates ORDER BY score DESC")
            if row["uid"] in wanted
        ]
    try:
        article_uid = jobs.start_weekly(board, picked_github, picked_papers)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return RedirectResponse(
            f"/boards?board={key}&view=weekly&msg=创建失败：{message}", status_code=303
        )
    return RedirectResponse(f"/article/{article_uid}", status_code=303)


# ---------------------------------------------------------------- 选题池（闸门 1）


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, msg: str = "") -> HTMLResponse:
    rows = db.query(
        "SELECT p.*, d.uid AS draft_uid, d.status AS draft_status, d.content AS draft_content "
        "FROM papers p LEFT JOIN drafts d ON d.paper_uid = p.uid AND d.kind = 'reading' "
        "ORDER BY p.selected DESC, p.created_at DESC, p.rowid DESC"
    )
    for row in rows:
        row["word_count"] = len((row.get("draft_content") or "").strip())
        row["figure_note"] = ""
    selected = [row for row in rows if row["selected"]]
    return render(
        request,
        "inbox.html",
        active="inbox",
        papers=rows,
        selected=selected,
        selected_ready=sum(1 for row in selected if row["draft_status"] == "ready"),
        msg=msg,
    )


@app.post("/inbox/{uid}/select")
def toggle_select(uid: str, selected: int = Form(default=0)):
    db.execute(
        "UPDATE papers SET selected=?, updated_at=? WHERE uid=?",
        (1 if selected else 0, db.now(), uid),
    )
    return RedirectResponse("/inbox", status_code=303)


@app.post("/inbox/batch")
def start_batch(engine: str = Form(default="builtin")):
    rows = db.query("SELECT uid FROM papers WHERE selected=1")
    queued = jobs.start_batch([row["uid"] for row in rows], engine=engine)
    return RedirectResponse(f"/inbox?msg=已排入队列 {queued} 篇", status_code=303)


# ---------------------------------------------------------------- 科研知识库


def insight_rows(
    *,
    status: str = "pending",
    mechanism: str = "",
    novelty_type: str = "",
    query_text: str = "",
) -> list[dict[str, Any]]:
    sql = [
        "SELECT i.*, p.title AS paper_title, p.filename AS paper_filename, "
        "(SELECT GROUP_CONCAT(m.name, ' / ') FROM insight_mechanisms im "
        " JOIN mechanisms m ON m.uid = im.mechanism_uid WHERE im.insight_uid = i.uid) "
        "AS mech_names "
        "FROM insights i LEFT JOIN papers p ON p.uid = i.paper_uid WHERE 1=1"
    ]
    params: list[Any] = []
    if status and status != "all":
        sql.append("AND i.status = ?")
        params.append(status)
    if novelty_type:
        sql.append("AND i.novelty_type = ?")
        params.append(novelty_type)
    if mechanism:
        sql.append(
            "AND EXISTS (SELECT 1 FROM insight_mechanisms im JOIN mechanisms m "
            "ON m.uid = im.mechanism_uid WHERE im.insight_uid = i.uid AND m.name = ?)"
        )
        params.append(mechanism)
    if query_text.strip():
        like = f"%{query_text.strip()}%"
        sql.append(
            "AND (i.title LIKE ? OR i.problem LIKE ? OR i.mechanism LIKE ? "
            "OR i.applies_to LIKE ?)"
        )
        params.extend([like, like, like, like])
    sql.append("ORDER BY i.paper_uid, i.ordinal")
    return db.query(" ".join(sql), tuple(params))


def group_by_paper(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按论文分组：一次看一篇的 2-4 条，比翻 68 张卡高效。"""
    groups: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["paper_uid"] or f"__draft__{row['draft_uid']}"
        group = index.get(key)
        if group is None:
            group = {
                "key": key,
                "paper_uid": row["paper_uid"],
                "title": row["paper_title"] or row["paper_filename"] or "（无关联论文）",
                "entries": [],
            }
            index[key] = group
            groups.append(group)
        group["entries"].append(row)
    return groups


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(
    request: Request,
    status: str = "pending",
    mechanism: str = "",
    type: str = "",
    q: str = "",
    msg: str = "",
) -> HTMLResponse:
    rows = insight_rows(status=status, mechanism=mechanism, novelty_type=type, query_text=q)
    groups = group_by_paper(rows)
    counts = {
        row["status"]: row["n"]
        for row in db.query("SELECT status, COUNT(*) AS n FROM insights GROUP BY status")
    }
    mechanisms = db.query(
        "SELECT m.name, m.name_en, m.verified, COUNT(im.insight_uid) AS n FROM mechanisms m "
        "LEFT JOIN insight_mechanisms im ON im.mechanism_uid = m.uid "
        "GROUP BY m.uid ORDER BY n DESC, m.name"
    )
    # 还没抽取过的解读：按更新时间倒序，一次处理一篇
    extractable = db.query(
        "SELECT d.uid, d.title, d.updated_at, LENGTH(d.content) AS chars, "
        "p.filename AS paper_filename "
        "FROM drafts d LEFT JOIN papers p ON p.uid = d.paper_uid "
        "WHERE d.kind='reading' AND d.content != '' AND d.extract_skip=0 "
        "AND NOT EXISTS (SELECT 1 FROM insights i WHERE i.draft_uid = d.uid) "
        "ORDER BY d.updated_at DESC"
    )
    skipped = db.query(
        "SELECT d.uid, d.title, d.updated_at FROM drafts d "
        "WHERE d.kind='reading' AND d.content != '' AND d.extract_skip=1 "
        "AND NOT EXISTS (SELECT 1 FROM insights i WHERE i.draft_uid = d.uid) "
        "ORDER BY d.updated_at DESC"
    )
    return render(
        request,
        "knowledge.html",
        active="knowledge",
        groups=groups,
        total=len(rows),
        counts=counts,
        mechanisms=mechanisms,
        status=status,
        mechanism=mechanism,
        novelty_type=type,
        q=q,
        novelty_types=knowledge.NOVELTY_TYPES,
        extractable=extractable,
        skipped=skipped,
        msg=msg,
    )


def insight_detail(uid: str) -> dict[str, Any] | None:
    row = db.one(
        "SELECT i.*, p.title AS paper_title, p.filename AS paper_filename "
        "FROM insights i LEFT JOIN papers p ON p.uid = i.paper_uid WHERE i.uid = ?",
        (uid,),
    )
    if row is None:
        return None
    row["mechanisms"] = db.query(
        "SELECT m.uid, m.name, m.name_en, m.verified, im.variant_note FROM insight_mechanisms im "
        "JOIN mechanisms m ON m.uid = im.mechanism_uid WHERE im.insight_uid = ? "
        "ORDER BY m.name",
        (uid,),
    )
    row["related"] = db.query(
        "SELECT DISTINCT i2.uid, i2.title, i2.status, p2.title AS paper_title "
        "FROM insight_mechanisms im1 "
        "JOIN insight_mechanisms im2 ON im2.mechanism_uid = im1.mechanism_uid "
        "JOIN insights i2 ON i2.uid = im2.insight_uid "
        "LEFT JOIN papers p2 ON p2.uid = i2.paper_uid "
        "WHERE im1.insight_uid = ? AND i2.uid != ? LIMIT 12",
        (uid, uid),
    )
    return row


@app.get("/knowledge/{uid}", response_class=HTMLResponse)
def knowledge_detail_page(request: Request, uid: str, msg: str = "") -> Response:
    row = insight_detail(uid)
    if row is None:
        return RedirectResponse("/knowledge?msg=条目不存在", status_code=303)
    return render(
        request,
        "knowledge_detail.html",
        active="knowledge",
        item=row,
        novelty_types=knowledge.NOVELTY_TYPES,
        all_mechanisms=db.query("SELECT uid, name FROM mechanisms ORDER BY verified, name"),
        msg=msg,
    )


@app.post("/knowledge/{uid}/verify")
def knowledge_verify(uid: str):
    db.execute(
        "UPDATE insights SET status='verified', updated_at=? WHERE uid=?", (db.now(), uid)
    )
    db.log_event(uid, "insight:verify", "")
    return RedirectResponse("/knowledge?msg=已确认", status_code=303)


@app.post("/knowledge/confirm-all")
def knowledge_confirm_all():
    """批量确认待审条目。用于「先入库、后面统一再改」的节奏。"""
    row = db.one("SELECT COUNT(*) AS n FROM insights WHERE status='pending'")
    count = int(row["n"]) if row else 0
    if count:
        db.execute(
            "UPDATE insights SET status='verified', updated_at=? WHERE status='pending'",
            (db.now(),),
        )
        db.log_event("insights", "verify:bulk", f"{count} 条")
    return RedirectResponse(
        f"/knowledge?msg=已批量确认 {count} 条（可随时在「已确认」里继续修改）",
        status_code=303,
    )


# ---------------------------------------------------------------- 知识图谱


@app.get("/graph", response_class=HTMLResponse)
def graph_page(request: Request) -> HTMLResponse:
    counts = {
        "insights": db.one("SELECT COUNT(*) AS n FROM insights")["n"],
        "mechanisms": db.one("SELECT COUNT(*) AS n FROM mechanisms")["n"],
        "papers": db.one("SELECT COUNT(DISTINCT draft_uid) AS n FROM insights")["n"],
    }
    return render(request, "graph.html", active="graph", counts=counts)


@app.get("/graph/data")
def graph_data(min_refs: int = 1, include_papers: int = 1) -> dict[str, Any]:
    """返回三层结构的图数据：论文 — 创新点 — 机制。

    节点规模限制在几百个（100 条洞察 + 71 个机制 + 30 篇论文），
    力导向在前端跑完全够；不需要后端算布局。
    """
    mechanisms = db.query(
        "SELECT m.uid, m.name, m.name_en, m.verified, "
        "(SELECT COUNT(*) FROM insight_mechanisms im WHERE im.mechanism_uid = m.uid) AS refs "
        "FROM mechanisms m ORDER BY refs DESC"
    )
    keep = {m["uid"] for m in mechanisms if int(m["refs"]) >= max(1, min_refs)}

    insights = db.query(
        "SELECT i.uid, i.title, i.novelty_type, i.draft_uid, "
        "d.title AS paper_title FROM insights i "
        "LEFT JOIN drafts d ON d.uid = i.draft_uid WHERE i.status != 'rejected'"
    )
    links = db.query(
        "SELECT im.insight_uid, im.mechanism_uid FROM insight_mechanisms im "
        "JOIN mechanisms m ON m.uid = im.mechanism_uid"
    )

    nodes: list[dict[str, Any]] = []
    node_links: list[dict[str, str]] = []

    used_insights: set[str] = set()
    for link in links:
        if link["mechanism_uid"] not in keep:
            continue
        node_links.append(
            {"source": link["insight_uid"], "target": link["mechanism_uid"], "kind": "tag"}
        )
        used_insights.add(link["insight_uid"])

    for mech in mechanisms:
        if mech["uid"] not in keep:
            continue
        nodes.append(
            {
                "id": mech["uid"],
                "label": mech["name"],
                "label_en": mech["name_en"],
                "type": "mechanism",
                "size": 5 + min(14, int(mech["refs"]) * 1.1),
                "refs": int(mech["refs"]),
                "url": f"/mechanisms/{mech['uid']}",
            }
        )

    papers: dict[str, dict[str, Any]] = {}
    for insight in insights:
        if insight["uid"] not in used_insights:
            continue
        nodes.append(
            {
                "id": insight["uid"],
                "label": insight["title"],
                "type": "insight",
                "size": 4,
                "kind": insight["novelty_type"] or "",
                "url": f"/knowledge/{insight['uid']}",
            }
        )
        draft_uid = insight["draft_uid"] or ""
        if include_papers and draft_uid:
            paper = papers.setdefault(
                draft_uid,
                {
                    "id": draft_uid,
                    "label": (insight["paper_title"] or "未命名论文"),
                    "type": "paper",
                    "size": 9,
                    "count": 0,
                    "url": f"/draft/{draft_uid}",
                },
            )
            paper["count"] += 1
            node_links.append({"source": draft_uid, "target": insight["uid"], "kind": "belongs"})

    if include_papers:
        for paper in papers.values():
            paper["size"] = 8 + min(10, paper["count"] * 1.5)
            nodes.append(paper)

    return {
        "nodes": nodes,
        "links": node_links,
        "stats": {
            "mechanisms": len(keep),
            "insights": len(used_insights),
            "papers": len(papers),
        },
    }


# ---------------------------------------------------------------- 机制卡维护


@app.get("/mechanisms/{uid}", response_class=HTMLResponse)
def mechanism_detail_page(request: Request, uid: str, msg: str = "") -> Response:
    """单个机制的详情：它下面挂了哪些具体创新点、和哪些机制常一起出现。"""
    mech = db.one("SELECT * FROM mechanisms WHERE uid=?", (uid,))
    if mech is None:
        return RedirectResponse("/mechanisms?msg=机制不存在", status_code=303)

    rows = db.query(
        "SELECT i.*, d.title AS paper_title, p.filename AS paper_filename "
        "FROM insights i JOIN insight_mechanisms im ON im.insight_uid = i.uid "
        "LEFT JOIN drafts d ON d.uid = i.draft_uid "
        "LEFT JOIN papers p ON p.uid = i.paper_uid "
        "WHERE im.mechanism_uid = ? AND i.status != 'rejected' "
        "ORDER BY d.updated_at DESC, i.ordinal",
        (uid,),
    )
    for row in rows:
        row["evidence_ok"] = int(row.get("evidence_ok") or 0)

    # 共现机制：经常和它一起标在同一条创新点里的其他机制
    co = db.query(
        "SELECT m2.uid, m2.name, m2.name_en, COUNT(*) AS n FROM insight_mechanisms a "
        "JOIN insight_mechanisms b ON b.insight_uid = a.insight_uid "
        "AND b.mechanism_uid != a.mechanism_uid "
        "JOIN mechanisms m2 ON m2.uid = b.mechanism_uid "
        "WHERE a.mechanism_uid = ? GROUP BY m2.uid ORDER BY n DESC, m2.name LIMIT 12",
        (uid,),
    )

    return render(
        request,
        "mechanism_detail.html",
        active="knowledge",
        mech=mech,
        groups=group_by_paper(rows),
        total=len(rows),
        cooccurring=co,
        msg=msg,
    )


@app.get("/mechanisms", response_class=HTMLResponse)
def mechanisms_page(request: Request, msg: str = "") -> HTMLResponse:
    rows = db.query(
        "SELECT m.*, COUNT(im.insight_uid) AS n FROM mechanisms m "
        "LEFT JOIN insight_mechanisms im ON im.mechanism_uid = m.uid "
        "GROUP BY m.uid ORDER BY m.verified, n DESC, m.name"
    )
    return render(
        request,
        "mechanisms.html",
        active="knowledge",
        mechanisms=rows,
        pending=sum(1 for row in rows if not row["verified"]),
        orphan=sum(1 for row in rows if not row["n"]),
        msg=msg,
    )


@app.post("/knowledge/mechanisms/confirm-all")
def mechanism_confirm_all():
    row = db.one("SELECT COUNT(*) AS n FROM mechanisms WHERE verified=0")
    count = int(row["n"]) if row else 0
    if count:
        db.execute("UPDATE mechanisms SET verified=1 WHERE verified=0")
        db.log_event("mech", "confirm:bulk", f"{count} 个")
    return RedirectResponse(
        f"/mechanisms?msg=已把 {count} 个待确认机制转为正式", status_code=303
    )


@app.post("/knowledge/mechanisms/{uid}/confirm")
def mechanism_confirm(uid: str):
    db.execute(
        "UPDATE mechanisms SET verified=1 WHERE uid=?", (uid,)
    )
    db.log_event(uid, "mech:confirm", "")
    return RedirectResponse("/mechanisms?msg=已确认为正式机制", status_code=303)


@app.post("/knowledge/mechanisms/{uid}/rename")
def mechanism_rename(
    uid: str,
    name: str = Form(default=""),
    name_en: str = Form(default=""),
    definition: str = Form(default=""),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/mechanisms?msg=名称不能为空", status_code=303)
    clash = db.one("SELECT uid FROM mechanisms WHERE name=? AND uid!=?", (name, uid))
    if clash:
        return RedirectResponse(
            "/mechanisms?msg=已有同名机制，请改用「合并」", status_code=303
        )
    db.execute(
        "UPDATE mechanisms SET name=?, name_en=?, definition=? WHERE uid=?",
        (name, name_en.strip(), definition.strip(), uid),
    )
    return RedirectResponse("/mechanisms?msg=已保存", status_code=303)


@app.post("/knowledge/mechanisms/merge")
def mechanism_merge(source: str = Form(default=""), target: str = Form(default="")):
    """把 source 的所有引用并到 target，然后删掉 source。"""
    if not source or not target or source == target:
        return RedirectResponse(
            "/mechanisms?msg=请选择两个不同的机制", status_code=303
        )
    src = db.one("SELECT * FROM mechanisms WHERE uid=?", (source,))
    dst = db.one("SELECT * FROM mechanisms WHERE uid=?", (target,))
    if src is None or dst is None:
        return RedirectResponse("/mechanisms?msg=机制不存在", status_code=303)

    links = db.query(
        "SELECT insight_uid, variant_note FROM insight_mechanisms WHERE mechanism_uid=?",
        (source,),
    )
    for link in links:
        db.execute(
            "INSERT OR IGNORE INTO insight_mechanisms(insight_uid, mechanism_uid, "
            "variant_note) VALUES(?,?,?)",
            (link["insight_uid"], target, link["variant_note"] or ""),
        )
    db.execute("DELETE FROM insight_mechanisms WHERE mechanism_uid=?", (source,))
    db.execute("DELETE FROM mechanisms WHERE uid=?", (source,))
    db.log_event("mech", "merge", f"{src['name']} → {dst['name']}（{len(links)} 条引用）")
    return RedirectResponse(
        f"/mechanisms?msg=已把「{src['name']}」并入「{dst['name']}」，"
        f"迁移 {len(links)} 条引用",
        status_code=303,
    )


@app.post("/knowledge/mechanisms/{uid}/delete")
def mechanism_delete(uid: str):
    used = db.one(
        "SELECT COUNT(*) AS n FROM insight_mechanisms WHERE mechanism_uid=?", (uid,)
    )
    if used and used["n"]:
        return RedirectResponse(
            f"/mechanisms?msg=还有 {used['n']} 条引用，请先合并再删", status_code=303
        )
    db.execute("DELETE FROM mechanisms WHERE uid=?", (uid,))
    return RedirectResponse("/mechanisms?msg=已删除", status_code=303)


@app.post("/knowledge/{uid}/mechanisms/add")
def insight_mechanism_add(uid: str, name: str = Form(default="")):
    name = name.strip()
    if not name:
        return RedirectResponse(f"/knowledge/{uid}?msg=机制名不能为空", status_code=303)
    existing = db.one("SELECT uid FROM mechanisms WHERE name=?", (name,))
    if existing is None:
        mech_uid = db.new_uid("mech")
        db.execute(
            "INSERT INTO mechanisms(uid, name, definition, verified, created_at) "
            "VALUES(?,?,?,?,?)",
            (mech_uid, name, "", 0, db.now()),
        )
    else:
        mech_uid = existing["uid"]
    db.execute(
        "INSERT OR IGNORE INTO insight_mechanisms(insight_uid, mechanism_uid, variant_note) "
        "VALUES(?,?,?)",
        (uid, mech_uid, ""),
    )
    return RedirectResponse(f"/knowledge/{uid}?msg=已添加机制标签", status_code=303)


@app.post("/knowledge/{uid}/mechanisms/{mech_uid}/remove")
def insight_mechanism_remove(uid: str, mech_uid: str):
    db.execute(
        "DELETE FROM insight_mechanisms WHERE insight_uid=? AND mechanism_uid=?",
        (uid, mech_uid),
    )
    return RedirectResponse(f"/knowledge/{uid}?msg=已移除标签", status_code=303)


@app.post("/knowledge/{uid}/reject")
def knowledge_reject(uid: str):
    db.execute(
        "UPDATE insights SET status='rejected', updated_at=? WHERE uid=?", (db.now(), uid)
    )
    return RedirectResponse("/knowledge?msg=已丢弃", status_code=303)


@app.post("/knowledge/{uid}/save")
def knowledge_save(
    uid: str,
    title: str = Form(default=""),
    problem: str = Form(default=""),
    mechanism: str = Form(default=""),
    novelty_type: str = Form(default=""),
    applies_to: str = Form(default=""),
    limitation: str = Form(default=""),
    status: str = Form(default="pending"),
):
    db.execute(
        "UPDATE insights SET title=?, problem=?, mechanism=?, novelty_type=?, "
        "applies_to=?, limitation=?, status=?, updated_at=? WHERE uid=?",
        (
            title.strip(),
            problem.strip(),
            mechanism.strip(),
            novelty_type.strip(),
            applies_to.strip(),
            limitation.strip(),
            status if status in ("pending", "verified", "rejected") else "pending",
            db.now(),
            uid,
        ),
    )
    return RedirectResponse(f"/knowledge/{uid}?msg=已保存", status_code=303)


@app.post("/knowledge/draft/{draft_uid}/extract")
def knowledge_extract(draft_uid: str, back: str = Form(default="draft")):
    target = "/knowledge" if back == "knowledge" else f"/draft/{draft_uid}"
    try:
        job_uid = jobs.start_insights(draft_uid, force=True)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return RedirectResponse(f"{target}?err={message}", status_code=303)
    if job_uid is None:
        return RedirectResponse(f"{target}?err=已有洞察，未重复抽取", status_code=303)
    return RedirectResponse(f"{target}?msg=已排队抽取创新点", status_code=303)


@app.post("/knowledge/draft/{draft_uid}/skip")
def knowledge_skip(draft_uid: str, skip: int = Form(default=1)):
    """从待抽取队列里忽略 / 恢复这篇解读。"""
    db.execute(
        "UPDATE drafts SET extract_skip=?, updated_at=? WHERE uid=?",
        (1 if skip else 0, db.now(), draft_uid),
    )
    db.log_event(draft_uid, "insights:skip" if skip else "insights:unskip", "")
    action = "已忽略" if skip else "已恢复到待抽取"
    anchor = "#skipped" if skip else ""
    return RedirectResponse(f"/knowledge?msg={action}{anchor}", status_code=303)


# ---------------------------------------------------------------- 设置


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "") -> HTMLResponse:
    settings = db.get_settings()
    masked = ""
    key = settings.get("llm_api_key", "")
    if key:
        masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "已配置"
    return render(
        request, "settings.html", active="settings", settings=settings, masked=masked, msg=msg
    )


@app.post("/settings")
def save_settings_form(
    llm_base_url: str = Form(default=""),
    llm_api_key: str = Form(default=""),
    llm_model: str = Form(default=""),
    llm_temperature: str = Form(default="0.3"),
    llm_max_tokens: str = Form(default="8192"),
    max_paper_chars: str = Form(default="120000"),
    radar_keywords: str = Form(default=""),
    radar_per_keyword: str = Form(default="15"),
    radar_max_age_days: str = Form(default="1095"),
    github_token: str = Form(default=""),
) -> RedirectResponse:
    current = db.get_settings()
    values = {
        "llm_base_url": llm_base_url.strip(),
        "llm_model": llm_model.strip(),
        "llm_temperature": llm_temperature.strip() or "0.3",
        "llm_max_tokens": llm_max_tokens.strip() or "8192",
        "max_paper_chars": max_paper_chars.strip() or "120000",
        "radar_keywords": radar_keywords.strip() or current.get("radar_keywords", ""),
        "radar_per_keyword": radar_per_keyword.strip() or "15",
        "radar_max_age_days": radar_max_age_days.strip() or "1095",
    }
    # 留空表示不修改已保存的密钥
    if llm_api_key.strip():
        values["llm_api_key"] = llm_api_key.strip()
    else:
        values["llm_api_key"] = current.get("llm_api_key", "")
    if github_token.strip():
        values["github_token"] = github_token.strip()
    else:
        values["github_token"] = current.get("github_token", "")
    db.save_settings(values)
    db.log_event("settings", "save", values["llm_model"])
    return RedirectResponse("/settings?msg=已保存", status_code=303)


@app.post("/settings/probe", response_class=HTMLResponse)
def probe_settings() -> HTMLResponse:
    settings = db.get_settings()
    config = llm.config_from_settings(settings)
    try:
        reply = llm.chat_once(
            config,
            [
                {"role": "system", "content": "你是连通性测试助手。"},
                {"role": "user", "content": "只回复两个字：可用"},
            ],
        )
    except llm.LLMError as exc:
        return HTMLResponse(
            f"<div class='probe bad'>连接失败：{exc}</div>"
        )
    return HTMLResponse(
        f"<div class='probe ok'>连接正常（{config.model}）：{reply[:40]}</div>"
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "papers": db.one("SELECT COUNT(*) AS n FROM papers")["n"]}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
