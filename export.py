"""导出：Markdown → 可直接粘进公众号编辑器的富文本。

公众号编辑器会丢弃 <style> 与 class，只认元素上的内联样式，
所以这里把样式逐个写到标签上，并单独把「图位提示」做成醒目的占位块。
"""

from __future__ import annotations

from html import escape

import markdown as mdlib
from bs4 import BeautifulSoup
from markupsafe import Markup

import themes

FIGURE_MARK = "🖼️"


def render_fragment(markdown_text: str, theme: str | None = None) -> str:
    """Markdown 正文 → 内联样式的 HTML 片段（按主题着色）。"""
    if not (markdown_text or "").strip():
        return ""
    selected = themes.get_theme(theme)
    html = mdlib.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    soup = BeautifulSoup(html, "html.parser")

    for tag_name, style in selected.styles.items():
        for tag in soup.find_all(tag_name):
            tag["style"] = style

    # 图位占位块单独提亮，便于粘贴后按提示补图
    for blockquote in soup.find_all("blockquote"):
        if FIGURE_MARK in blockquote.get_text():
            blockquote["style"] = themes.figure_style(selected)

    # 代码块内部的 code 不要继承行内代码的底色
    for pre in soup.find_all("pre"):
        for code in pre.find_all("code"):
            code["style"] = (
                "background:none;padding:0;color:inherit;font-family:Consolas,Monaco,Menlo,monospace;"
            )

    return str(soup)


def standalone_page(
    *,
    title: str,
    body_html: str,
    meta: dict[str, str] | None = None,
) -> str:
    """自包含的粘贴页：带复制按钮，不依赖任何外部资源。"""
    meta = meta or {}
    title_line = escape(meta.get("title") or title or "未命名文章")
    info = " · ".join(
        part
        for part in [meta.get("venue", ""), meta.get("model", ""), meta.get("theme", "")]
        if part
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_line} · 待发布</title>
<style>
  body {{ margin:0; background:#eceae4; font-family:-apple-system,"Microsoft YaHei",sans-serif; }}
  .bar {{ position:sticky; top:0; z-index:9; display:flex; gap:10px; align-items:center;
          padding:12px 18px; background:#fffdf8; border-bottom:1px solid #ddd6c8; }}
  .bar b {{ font-size:14px; color:#211e19; }}
  .bar span {{ font-size:12px; color:#6e675b; }}
  .bar button {{ margin-left:auto; padding:8px 16px; border-radius:8px; border:1px solid #211e19;
                 background:#211e19; color:#fdfaf4; font-size:13px; cursor:pointer; }}
  .bar button:hover {{ background:#38312a; }}
  .ok {{ color:#2f6b4f; }}
  .page {{ max-width:720px; margin:24px auto 80px; background:#ffffff; padding:36px 34px;
           border-radius:4px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
  #article {{ font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
  #article img {{ max-width:100%; }}
  @media (max-width:760px) {{ .page {{ padding:22px 16px; margin:12px; }} }}
</style>
</head>
<body>
<div class="bar">
  <b>{title_line}</b>
  <span>{info}</span>
  <button onclick="copyRich()">复制正文（带格式）</button>
  <span id="state"></span>
</div>
<div class="page"><div id="article">{body_html}</div></div>
<script>
async function copyRich() {{
  const el = document.getElementById('article');
  const state = document.getElementById('state');
  const html = el.innerHTML;
  const text = el.innerText;
  try {{
    await navigator.clipboard.write([new ClipboardItem({{
      'text/html': new Blob([html], {{ type: 'text/html' }}),
      'text/plain': new Blob([text], {{ type: 'text/plain' }})
    }})]);
    state.textContent = '已复制，去公众号编辑器粘贴';
    state.className = 'ok';
  }} catch (err) {{
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    state.textContent = '已复制（兼容模式）';
    state.className = 'ok';
  }}
}}
</script>
</body>
</html>"""


def as_markup(html: str) -> Markup:
    return Markup(html)
