"""公众号排版主题。

设计依据：
- 基线取自 mdnice（github.com/mdnice/markdown-nice）的 `src/template/basic.js`：
  16px 正文、26px 行高、30/15px 标题间距、3px 左框引用、细边框表格。
  该文件里有一条实战经验——**纯黑 color 会被微信编辑器吞掉**，所以本文件所有
  文字色都避开 #000。
- 公众号编辑器只保留内联样式，且会过滤 position / 伪元素 / class，
  所以这里只描述「标签 → 内联样式」，不做任何布局技巧。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    description: str
    swatch: str
    styles: dict[str, str] = field(default_factory=dict)

    def style_for(self, tag: str) -> str:
        return self.styles.get(tag, "")


# 所有主题共用的骨架：字号体系、间距、图片、表格
_BASE: dict[str, str] = {
    "p": "margin:0 0 18px;font-size:16px;line-height:26px;color:#3f3f3f;letter-spacing:0.4px;",
    "h1": "margin:32px 0 16px;font-size:23px;line-height:1.45;font-weight:700;color:#1a1a1a;",
    "h2": "margin:32px 0 16px;font-size:19px;line-height:1.45;font-weight:700;color:#1a1a1a;",
    "h3": "margin:24px 0 12px;font-size:17px;line-height:1.5;font-weight:700;color:#1a1a1a;",
    "h4": "margin:20px 0 10px;font-size:16px;line-height:1.5;font-weight:700;color:#1a1a1a;",
    "strong": "font-weight:700;color:#1a1a1a;",
    "em": "font-style:italic;color:#555555;",
    "ul": "margin:0 0 18px;padding-left:24px;",
    "ol": "margin:0 0 18px;padding-left:24px;",
    "li": "margin:0 0 8px;font-size:16px;line-height:26px;color:#3f3f3f;",
    "img": "display:block;margin:14px auto;max-width:100%;",
    "pre": "margin:14px 0;padding:12px 14px;border-radius:6px;overflow-x:auto;"
    "font-size:13px;line-height:1.7;background:#f6f6f6;color:#333333;",
    "table": "width:100%;border-collapse:collapse;font-size:14px;margin:18px 0;",
    "th": "border:1px solid #dddddd;padding:6px 10px;background:#f0f0f0;text-align:left;"
    "font-weight:700;color:#1a1a1a;",
    "td": "border:1px solid #dddddd;padding:6px 10px;color:#3f3f3f;",
    "hr": "border:none;border-top:1px solid #e6e6e6;margin:28px 0;",
    "a": "color:#1e6bb8;text-decoration:none;font-weight:600;border-bottom:1px solid #1e6bb8;",
}


def _merge(overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(_BASE)
    merged.update(overrides)
    return merged


THEMES: tuple[Theme, ...] = (
    Theme(
        key="classic",
        name="经典（mdnice 基线）",
        description="黑灰正文、细边框表格、蓝色链接。最稳，什么号都能用。",
        swatch="#1e6bb8",
        styles=_merge(
            {
                "blockquote": "display:block;font-size:15px;overflow:auto;"
                "border-left:3px solid rgba(0,0,0,0.4);background:rgba(0,0,0,0.05);"
                "color:#6a737d;padding:10px 10px 10px 20px;margin:20px 0;",
                "code": "font-size:14px;padding:2px 4px;border-radius:4px;margin:0 2px;"
                "color:#1e6bb8;background:rgba(27,31,35,0.05);"
                "font-family:Consolas,Monaco,Menlo,monospace;",
            }
        ),
    ),
    Theme(
        key="green",
        name="微信绿",
        description="h2 左侧绿色竖条、绿色强调。最常见的公众号技术号风格。",
        swatch="#07c160",
        styles=_merge(
            {
                "h2": "margin:34px 0 16px;font-size:19px;line-height:1.45;font-weight:700;"
                "color:#1a1a1a;padding-left:10px;border-left:4px solid #07c160;",
                "strong": "font-weight:700;color:#0f8a49;",
                "blockquote": "margin:18px 0;padding:12px 16px;background:#f2faf5;"
                "border-left:3px solid #b7e4c9;color:#4a6b57;font-size:15px;line-height:1.8;",
                "code": "font-size:14px;padding:2px 5px;border-radius:3px;color:#0f8a49;"
                "background:#eef9f2;font-family:Consolas,Monaco,Menlo,monospace;",
                "th": "border:1px solid #d5e8dc;padding:6px 10px;background:#f2faf5;"
                "text-align:left;font-weight:700;color:#1a1a1a;",
                "td": "border:1px solid #d5e8dc;padding:6px 10px;color:#3f3f3f;",
                "a": "color:#0f8a49;text-decoration:none;font-weight:600;"
                "border-bottom:1px solid #0f8a49;",
            }
        ),
    ),
    Theme(
        key="tech",
        name="科技蓝",
        description="深蓝标题、浅蓝引用底、深色代码块。偏工程/算法号。",
        swatch="#2f5fa8",
        styles=_merge(
            {
                "h1": "margin:32px 0 16px;font-size:23px;line-height:1.45;font-weight:700;"
                "color:#13325c;",
                "h2": "margin:34px 0 16px;font-size:19px;line-height:1.45;font-weight:700;"
                "color:#13325c;padding-bottom:6px;border-bottom:2px solid #d6e2f5;",
                "h3": "margin:24px 0 12px;font-size:17px;line-height:1.5;font-weight:700;"
                "color:#2f5fa8;",
                "strong": "font-weight:700;color:#2f5fa8;",
                "blockquote": "margin:18px 0;padding:12px 16px;background:#f2f6fc;"
                "border-left:3px solid #9dbde8;color:#4a5b73;font-size:15px;line-height:1.8;",
                "code": "font-size:14px;padding:2px 5px;border-radius:3px;color:#2f5fa8;"
                "background:#eef3fb;font-family:Consolas,Monaco,Menlo,monospace;",
                "pre": "margin:14px 0;padding:12px 14px;border-radius:6px;overflow-x:auto;"
                "font-size:13px;line-height:1.7;background:#22303f;color:#e6edf3;",
                "th": "border:1px solid #d6e2f5;padding:6px 10px;background:#f2f6fc;"
                "text-align:left;font-weight:700;color:#13325c;",
                "td": "border:1px solid #d6e2f5;padding:6px 10px;color:#3f3f3f;",
                "a": "color:#2f5fa8;text-decoration:none;font-weight:600;"
                "border-bottom:1px solid #2f5fa8;",
            }
        ),
    ),
    Theme(
        key="paper",
        name="纸感（编辑台）",
        description="暖色纸底、衬线正文、砖红强调。像一本印刷刊物。",
        swatch="#9b3d2c",
        styles=_merge(
            {
                "p": "margin:0 0 18px;font-size:16px;line-height:28px;color:#3a352c;"
                "letter-spacing:0.5px;font-family:Georgia,'Songti SC','Noto Serif SC',serif;",
                "li": "margin:0 0 8px;font-size:16px;line-height:28px;color:#3a352c;"
                "font-family:Georgia,'Songti SC','Noto Serif SC',serif;",
                "h1": "margin:32px 0 18px;font-size:23px;line-height:1.5;font-weight:700;"
                "color:#2a241c;font-family:Georgia,'Songti SC',serif;",
                "h2": "margin:36px 0 16px;font-size:19px;line-height:1.5;font-weight:700;"
                "color:#2a241c;padding-left:12px;border-left:4px solid #9b3d2c;",
                "h3": "margin:24px 0 12px;font-size:17px;line-height:1.55;font-weight:700;"
                "color:#4a3f32;",
                "strong": "font-weight:700;color:#8c3626;",
                "blockquote": "margin:20px 0;padding:14px 18px;background:#f8f4ec;"
                "border-left:3px solid #d8cdb8;color:#6d675b;font-size:15px;line-height:1.9;",
                "hr": "border:none;border-top:1px solid #e2dcd0;margin:30px 0;",
                "th": "border:1px solid #ddd5c6;padding:6px 10px;background:#f7f2e8;"
                "text-align:left;font-weight:700;color:#2a241c;",
                "td": "border:1px solid #ddd5c6;padding:6px 10px;color:#3a352c;",
                "a": "color:#9b3d2c;text-decoration:none;font-weight:600;"
                "border-bottom:1px solid #d9b7ae;",
            }
        ),
    ),
    Theme(
        key="mono",
        name="极简黑白",
        description="全灰阶，无任何彩色强调。适合严肃技术长文，不抢内容。",
        swatch="#444444",
        styles=_merge(
            {
                "h2": "margin:34px 0 16px;font-size:19px;line-height:1.45;font-weight:700;"
                "color:#111111;letter-spacing:0.5px;",
                "strong": "font-weight:700;color:#111111;",
                "blockquote": "margin:18px 0;padding:12px 16px;background:#f7f7f7;"
                "border-left:3px solid #cccccc;color:#666666;font-size:15px;line-height:1.8;",
                "code": "font-size:14px;padding:2px 5px;border-radius:3px;color:#444444;"
                "background:#f2f2f2;font-family:Consolas,Monaco,Menlo,monospace;",
                "a": "color:#444444;text-decoration:none;font-weight:600;"
                "border-bottom:1px solid #999999;",
            }
        ),
    ),
)

THEME_MAP = {theme.key: theme for theme in THEMES}
DEFAULT_THEME = "classic"


def get_theme(key: str | None) -> Theme:
    return THEME_MAP.get((key or "").strip() or DEFAULT_THEME, THEME_MAP[DEFAULT_THEME])


def figure_style(theme: Theme) -> str:
    """图位提示块：用主题强调色描边，粘贴后一眼能看到该在哪插图。"""
    return (
        f"margin:18px 0;padding:12px 16px;background:#fafafa;"
        f"border:1px dashed {theme.swatch};color:#555555;font-size:14px;line-height:1.7;"
    )
