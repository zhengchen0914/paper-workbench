"""PDF 文本提取。

只做一件事：把 PDF 变成带页码标记的纯文本，供模型精读。
扫描件（无文本层）会被识别并明确报告，而不是静默返回空字符串。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pymupdf


class PDFError(RuntimeError):
    """PDF 无法解析或没有可用文本层。"""


def extract(path: str | Path) -> dict[str, Any]:
    """提取全文，返回 {pages, char_count, text, has_text_layer, title_guess}。"""
    file_path = Path(path)
    if not file_path.exists():
        raise PDFError(f"文件不存在：{file_path}")

    try:
        doc = pymupdf.open(file_path)
    except Exception as exc:  # pymupdf 的异常类型不稳定，统一兜住
        raise PDFError(f"无法打开 PDF：{exc}") from exc

    try:
        page_texts: list[str] = []
        for index, page in enumerate(doc, start=1):
            body = page.get_text("text").strip()
            page_texts.append(f"\n===== 第 {index} 页 =====\n{body}")
    finally:
        doc.close()

    text = "\n".join(page_texts).strip()
    char_count = len(text)
    # 粗略判断有无文本层：正文有效字符太少说明是扫描件
    cjk_and_latin = len(re.findall(r"[\u4e00-\u9fffA-Za-z]", text))
    has_text_layer = cjk_and_latin > 200

    return {
        "pages": len(page_texts),
        "char_count": char_count,
        "text": text,
        "has_text_layer": has_text_layer,
        "title_guess": _guess_title(text),
    }


def _guess_title(text: str) -> str:
    """从首页正文猜标题：取第一组较长的连续非空行。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = [line for line in lines if "=====" not in line and len(line) > 12]
    if not candidates:
        return ""
    head = candidates[:6]
    title = " ".join(head)
    return title[:180]


# 同一份 PDF 会被反复用于溯源，按 (路径, mtime, 大小) 缓存全文
_TEXT_CACHE: dict[tuple[str, float, int], str] = {}
_TEXT_CACHE_LIMIT = 8


def source_text(path: str | Path) -> str:
    """取全文用于数字溯源，带简单缓存。"""
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError:
        raise PDFError(f"文件不存在：{file_path}")
    key = (str(file_path), stat.st_mtime, stat.st_size)
    cached = _TEXT_CACHE.get(key)
    if cached is not None:
        return cached

    text = extract(file_path)["text"]
    if len(_TEXT_CACHE) >= _TEXT_CACHE_LIMIT:
        _TEXT_CACHE.clear()
    _TEXT_CACHE[key] = text
    return text


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字符上限截断，返回 (文本, 是否被截断)。

    截断点尽量对齐到页码标记，避免把一段话切在中间；
    但如果退到标记处会丢掉大部分内容（不足 60% 上限），宁可保留半页。
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    window = text[:limit]
    marker = window.rfind("\n===== 第 ")
    if marker > limit * 0.6:
        window = window[:marker]
    return window, True


def extract_images(
    path: str | Path,
    out_dir: str | Path,
    *,
    min_side: int = 80,
    max_images: int = 40,
    max_width: int = 1400,
    keep_png_bytes: int = 200_000,
) -> list[dict[str, Any]]:
    """把 PDF 内嵌位图导出来当配图候选。

    - 只取两侧都 >= min_side 的图（过滤图标、装饰线），跨页重复的同一张只留一次。
    - 大图统一缩到 max_width 并转 JPEG，否则一篇论文的图能撑到几十 MB；
      小于 keep_png_bytes 的图保留原格式（多为线框图，JPEG 会糊）。
    - 矢量图与整页排版图不会出现在这里，那类图需要按页渲染裁切，留到后续版本。
    """
    file_path = Path(path)
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if not file_path.exists():
        raise PDFError(f"文件不存在：{file_path}")

    saved: list[dict[str, Any]] = []
    seen: set[int] = set()

    try:
        doc = pymupdf.open(file_path)
    except Exception as exc:
        raise PDFError(f"无法打开 PDF：{exc}") from exc

    try:
        for page_index, page in enumerate(doc, start=1):
            for image_index, info in enumerate(page.get_images(full=True), start=1):
                xref = int(info[0])
                if xref in seen:
                    continue
                try:
                    payload = doc.extract_image(xref)
                except Exception:
                    continue
                width = int(payload.get("width") or 0)
                height = int(payload.get("height") or 0)
                if width < min_side or height < min_side:
                    continue
                seen.add(xref)

                raw = payload["image"]
                ext = str(payload.get("ext") or "png")
                data, ext = _shrink(raw, ext, max_width=max_width, keep_png_bytes=keep_png_bytes)
                filename = f"p{page_index:02d}_{image_index:02d}.{ext}"
                out_path = target_dir / filename
                out_path.write_bytes(data)
                saved.append(
                    {
                        "page": page_index,
                        "width": width,
                        "height": height,
                        "bytes": len(data),
                        "path": str(out_path),
                        "filename": filename,
                        "caption": f"第 {page_index} 页 · 图 {image_index}（{width}×{height}）",
                    }
                )
                if len(saved) >= max_images:
                    return saved
    finally:
        doc.close()
    return saved


def _shrink(
    raw: bytes,
    ext: str,
    *,
    max_width: int,
    keep_png_bytes: int,
) -> tuple[bytes, str]:
    """把大图缩到 max_width 并转 JPEG；小图原样保留。"""
    if len(raw) <= keep_png_bytes:
        return raw, ext
    try:
        from PIL import Image
    except ImportError:  # 没装 Pillow 就原样返回，不阻断主流程
        return raw, ext

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            if image.width > max_width:
                ratio = max_width / float(image.width)
                image = image.resize(
                    (max_width, max(1, int(image.height * ratio))),
                    Image.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85, optimize=True)
            return buffer.getvalue(), "jpg"
    except Exception:
        return raw, ext
