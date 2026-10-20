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
import zipfile
from contextlib import asynccontextmanager
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
import jobs
import llm
import packaging
import pdfx
import prompts
import radar
import themes
import verify

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
    base = {"status_labels": STATUS_LABELS, "engine_labels": ENGINE_LABELS}
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
def drafts_list(request: Request, kind: str = "reading") -> HTMLResponse:
    rows = db.query(
        "SELECT d.*, p.filename FROM drafts d LEFT JOIN papers p ON p.uid = d.paper_uid "
        "WHERE d.kind=? AND d.status != 'empty' ORDER BY d.updated_at DESC",
        (kind,),
    )
    return render(request, "drafts.html", active="drafts", drafts=rows, kind=kind)


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
    disposition = (
        f'attachment; filename="package.zip"; filename*=UTF-8\'\'{quote(filename)}'
    )
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
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
        max_age = int(float(settings.get("radar_max_age_days") or 180))
    except (TypeError, ValueError):
        per_keyword, max_age = 15, 180

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
                "published, url, pdf_url, comment, categories, keyword, score, reasons, "
                "status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
    radar_max_age_days: str = Form(default="180"),
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
        "radar_max_age_days": radar_max_age_days.strip() or "180",
    }
    # 留空表示不修改已保存的密钥
    if llm_api_key.strip():
        values["llm_api_key"] = llm_api_key.strip()
    else:
        values["llm_api_key"] = current.get("llm_api_key", "")
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
