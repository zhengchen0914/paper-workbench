"""文章内嵌图片的收集与落盘改写。

为什么需要这个：正文里的图是 `/assets/{uid}/xxx.jpg` 这种**服务端绝对路径**，
只在浏览器里（经由 web 服务）能加载。一旦导出成 zip 或单独 HTML 存到磁盘，
再双击打开，这些路径就指向不存在的位置——图自然"消失"了。

所以导出时必须做两件事之一：
- 放进 zip 的：改写成包内相对路径 `图片/xxx.jpg`；
- 单独 HTML 的：直接把图片转成 data URL 内嵌进去。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import db

# 匹配 markdown/html 里的 /assets/<uid>/<文件名>
ASSET_REF = re.compile(r"/assets/([A-Za-z0-9_\-]+)/([^)\"\s'<>]+)")

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def collect(text: str) -> list[tuple[str, str, Path]]:
    """从正文里找出所有内嵌图片，返回 [(原始引用串, 文件名, 磁盘路径)]。

    只保留磁盘上真实存在的，避免把 404 带进包里。
    """
    seen: dict[str, tuple[str, str, Path]] = {}
    for match in ASSET_REF.finditer(text or ""):
        uid, name = match.group(1), match.group(2)
        path = db.ASSETS_DIR / uid / name
        if path.exists() and match.group(0) not in seen:
            seen[match.group(0)] = (match.group(0), name, path)
    return list(seen.values())


def to_relative(text: str) -> tuple[str, list[tuple[str, Path]]]:
    """把 /assets/... 改写成 图片/<文件名>，返回 (新文本, [(文件名, 路径)])。

    文件名保持不变——正文里已经是 gen_1.jpg 这种，重名风险可以接受。
    """
    items = collect(text)
    result = text
    files: list[tuple[str, Path]] = []
    for ref, name, path in items:
        result = result.replace(ref, f"图片/{name}")
        files.append((name, path))
    return result, files


def inline_as_data_urls(html: str, *, limit_mb: float = 12.0) -> tuple[str, int]:
    """把 HTML 里的 /assets/... 换成 data URL，得到单文件可携带的文档。

    超过 limit_mb 就放弃内嵌（避免生成一个几十 MB 的 HTML），返回未改动的内容。
    """
    items = collect(html)
    if not items:
        return html, 0

    total = sum(path.stat().st_size for _, _, path in items) / (1024 * 1024)
    if total > limit_mb:
        return html, 0

    result = html
    for ref, name, path in items:
        mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        result = result.replace(ref, f"data:{mime};base64,{encoded}")
    return result, len(items)
