"""自动配图：调 agnes-image-2.5-flash 生成插图，插进公众号文章。

设计取舍：
- 提示词从「小节标题」推导，不再多调一次模型写提示词——省一次调用，
  也避免模型把配图需求写成空话；
- 风格写死成和工作台一致的编辑插画（米白纸面 / 砖红 / 深蓝），
  这样多张图放一起不打架；
- 生成的是 PNG，统一裁成 16:9 再入库，公众号里排版更稳。
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import httpx

import db

MODEL = "agnes-image-2.5-flash"
ENDPOINT = "/images/generations"

STYLE = (
    "编辑插画风格：温暖米白纸面底色（#f7f4ec），砖红（#9b3d2c）与深蓝（#1f3a5f）为主色，"
    "细线条勾边加平涂色块，轻微纸质颗粒质感，留白充足、构图简洁克制。"
    "不要出现任何文字、字母、数字、水印、logo；不要写实照片质感；不要霓虹高饱和。"
)

HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)
IMAGE_LINE = re.compile(r"^!\[[^\]]*\]\(/assets/", re.M)


class IllustrationError(RuntimeError):
    """配图失败，携带可读原因。"""


def build_prompt(heading: str, title: str) -> str:
    """小节标题 → 图像提示词。"""
    topic = heading.strip().strip("#").strip()
    return (
        f"为一篇技术文章生成一张配图。\n"
        f"文章主题：{title or '技术解析'}。\n"
        f"这张图对应文章的「{topic}」这一节，请用**具体的视觉隐喻**表现它的核心概念，"
        f"而不是堆砌抽象的几何图形。\n"
        f"{STYLE}"
    )


def pick_sections(markdown: str, count: int) -> list[tuple[str, int]]:
    """挑出要配图的小节，返回 [(标题, 标题所在行的行号)]。

    跳过「引言/结语/总结」这类不需要插图的收尾小节；如果标题不够，就按顺序补。
    """
    if count <= 0:
        return []
    lines = markdown.splitlines()
    found: list[tuple[str, int]] = []
    skip_words = ("引言", "结语", "总结", "写在最后", "参考", "延伸阅读", "尾巴")

    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        title = match.group(1).strip()
        if any(word in title for word in skip_words):
            continue
        found.append((title, index))

    if not found:
        return []
    # 均匀取样，避免几张图全挤在开头
    if len(found) <= count:
        return found
    step = len(found) / count
    return [found[min(len(found) - 1, int(i * step))] for i in range(count)]


def insert_image(markdown: str, line_index: int, alt: str, src: str) -> str:
    """在第 line_index 行的小节标题后、第一段正文之后再插入图片。"""
    lines = markdown.splitlines()
    cursor = min(line_index + 1, len(lines))
    # 找标题后的第一段结束位置：连续非空行之后遇到空行
    seen_paragraph = False
    while cursor < len(lines):
        if lines[cursor].strip():
            seen_paragraph = True
        elif seen_paragraph:
            break
        cursor += 1
    image_block = ["", f"![{alt}]({src})", ""]
    lines[cursor:cursor] = image_block
    return "\n".join(lines)


def _credentials() -> tuple[str, str]:
    settings = db.get_settings()
    base = (settings.get("llm_base_url") or "").rstrip("/")
    key = settings.get("llm_api_key") or ""
    if not base or not key:
        raise IllustrationError("还没配置模型接口地址或 key")
    return base, key


def generate(prompt: str, *, timeout: float = 180.0) -> str:
    """调图像接口，返回远端 URL。"""
    base, key = _credentials()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base}{ENDPOINT}",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": MODEL, "prompt": prompt, "n": 1},
            )
    except httpx.HTTPError as exc:
        raise IllustrationError(f"图像接口网络失败：{exc}") from exc

    if response.status_code >= 400:
        raise IllustrationError(
            f"图像接口返回 {response.status_code}：{response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise IllustrationError("图像接口返回的不是 JSON") from exc

    items = payload.get("data") or []
    url = (items[0] or {}).get("url") if items else ""
    if not url:
        raise IllustrationError("图像接口没有返回图片地址")
    return str(url)


def download(url: str, target: Path, *, timeout: float = 120.0) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            target.write_bytes(response.content)
    except httpx.HTTPError as exc:
        raise IllustrationError(f"下载图片失败：{exc}") from exc
    return target


def crop_16x9(path: Path) -> bool:
    """裁成 16:9。Pillow 不可用时静默跳过，不阻塞配图。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            width, height = image.size
            target_h = round(width * 9 / 16)
            if target_h <= height:
                top = (height - target_h) // 2
                image = image.crop((0, top, width, top + target_h))
            else:
                target_w = round(height * 16 / 9)
                left = (width - target_w) // 2
                image = image.crop((left, 0, left + target_w, height))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=92, optimize=True)
        path.with_suffix(".jpg").write_bytes(buffer.getvalue())
        if path.suffix.lower() != ".jpg":
            path.unlink(missing_ok=True)
        return True
    except Exception:  # 裁剪失败不该让整篇配图失败
        return False


def already_illustrated(markdown: str) -> bool:
    return bool(IMAGE_LINE.search(markdown))
