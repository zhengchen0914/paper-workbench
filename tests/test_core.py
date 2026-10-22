"""核心纯函数测试：物料解析、合规检查、导出样式、截断对齐。

这些都不依赖模型接口，是每次改动都必须跑通的最低门槛。
"""

import export
import github_boards
import packaging
import pdfx
import prompts
import radar
import themes
import verify


# ---------------------------------------------------------------- 物料解析


def test_parse_package_strips_code_fence():
    raw = """```json
{"titles":[{"text":"标题甲","angle":"结果","score":5}],
 "summary":"摘要正文",
 "cover":{"main":"主","sub":"副"},
 "tags":["#A","B"]}
```"""
    meta = packaging.parse_package(raw)
    assert len(meta["titles"]) == 1
    assert meta["titles"][0]["score"] == 5
    assert meta["summary"] == "摘要正文"
    assert meta["cover"]["main"] == "主"
    # 没带 # 的标签会被补上
    assert meta["tags"] == ["#A", "#B"]


def test_parse_package_tolerates_noise_and_string_titles():
    raw = '好的，这是物料：{"titles":["标题一","标题二"],"tags":[]} 希望有帮助'
    meta = packaging.parse_package(raw)
    assert [t["text"] for t in meta["titles"]] == ["标题一", "标题二"]
    assert meta["titles"][0]["score"] == 0


def test_parse_package_returns_empty_on_garbage():
    for raw in ("", "不是 JSON", "{坏掉了}", "[1,2,3]"):
        meta = packaging.parse_package(raw)
        assert meta["titles"] == []
        assert meta["summary"] == ""


# ---------------------------------------------------------------- 合规检查


def test_check_prohibited_levels_and_counts():
    hits = packaging.check_prohibited("这是最强的方法，最强，100% 准确。")
    words = {hit["word"]: hit for hit in hits}
    assert words["最强"]["level"] == "危险"
    assert words["最强"]["count"] == 2
    assert words["100%"]["level"] == "危险"


def test_check_prohibited_danger_sorts_first():
    hits = packaging.check_prohibited("必看，然后加微信。")
    assert hits[0]["level"] == "危险"
    assert hits[0]["word"] == "加微信"


def test_check_prohibited_clean_text():
    hits = packaging.check_prohibited("本文介绍一种表面缺陷检测方法，在 MVTec LOCO 上 AUROC 为 96.1%。")
    assert hits == []
    assert packaging.summary_stats(hits) == {"danger": 0, "caution": 0}


# ---------------------------------------------------------------- 导出样式


def test_render_fragment_inlines_styles():
    html = export.render_fragment("## 小标题\n\n正文一段。\n\n- 列表项")
    assert "font-size:16px" in html
    assert "<h2" in html and "style=" in html


def test_render_fragment_applies_selected_theme():
    green = export.render_fragment("## 小标题", "green")
    assert "border-left:4px solid #07c160" in green
    tech = export.render_fragment("```\ncode\n```", "tech")
    assert "background:#22303f" in tech
    classic = export.render_fragment("## 小标题", "classic")
    assert "#07c160" not in classic


def test_all_themes_avoid_pure_black_text():
    """mdnice 的实战经验：纯黑 color 会被微信编辑器吞掉。"""
    for theme in themes.THEMES:
        for tag, style in theme.styles.items():
            assert "color:#000" not in style.replace(" ", ""), f"{theme.key}/{tag}"
            assert "color:black" not in style.replace(" ", ""), f"{theme.key}/{tag}"


def test_get_theme_falls_back_to_default():
    assert themes.get_theme("不存在").key == themes.DEFAULT_THEME
    assert themes.get_theme(None).key == themes.DEFAULT_THEME


def test_render_fragment_highlights_figure_placeholder():
    html = export.render_fragment("> 🖼️建议插入图：论文 Figure2 整体架构图", "green")
    assert "border:1px dashed #07c160" in html
    assert "🖼️" in html


def test_render_fragment_empty_input():
    assert export.render_fragment("") == ""
    assert export.render_fragment("   ") == ""


def test_standalone_page_echappes_title():
    page = export.standalone_page(title="<script>x</script>", body_html="<p>正文</p>")
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;" in page


# ---------------------------------------------------------------- 截断对齐


def test_truncate_keeps_marker_boundary():
    text = "===== 第 1 页 =====\n" + "甲" * 100 + "\n===== 第 2 页 =====\n" + "乙" * 100
    truncated, was_cut = pdfx.truncate(text, 150)
    assert was_cut is True
    assert len(truncated) <= 150
    assert "乙" not in truncated


def test_truncate_noop_under_limit():
    text = "短文本"
    assert pdfx.truncate(text, 100) == (text, False)
    assert pdfx.truncate(text, 0) == (text, False)


# ---------------------------------------------------------------- 配图缩放


def test_shrink_keeps_small_images_untouched():
    raw = b"x" * 1000
    data, ext = pdfx._shrink(raw, "png", max_width=1400, keep_png_bytes=200_000)
    assert data == raw and ext == "png"


def test_shrink_downscales_large_images_to_jpeg():
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (3000, 1500), (120, 30, 30))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()

    data, ext = pdfx._shrink(raw, "png", max_width=800, keep_png_bytes=1)
    assert ext == "jpg"
    assert len(data) < len(raw)
    with Image.open(BytesIO(data)) as result:
        assert result.format == "JPEG"
        assert result.width == 800


# ---------------------------------------------------------------- 数字溯源


def test_trace_numbers_exact_match():
    result = verify.trace_numbers("MVTec 上 AUROC 为 96.1。", "表 3：AUROC 96.1 (Ours)")
    assert result["total"] == 1
    assert result["unmatched"] == 0


def test_trace_numbers_tolerates_percent_sign():
    result = verify.trace_numbers("像素 AUROC 92.0%。", "结果达到 92.0")
    assert result["unmatched"] == 0


def test_trace_numbers_tolerates_rounding():
    result = verify.trace_numbers("AP 为 49.4。", "AP = 49.43")
    assert result["unmatched"] == 0


def test_trace_numbers_flags_invented_value():
    result = verify.trace_numbers("在 VisA 上达到 98.7%。", "VisA 上为 95.7")
    assert result["unmatched"] == 1
    assert result["details"][0]["number"] == "98.7%"
    assert "98.7" in result["details"][0]["context"]


def test_trace_numbers_empty_inputs():
    assert verify.trace_numbers("", "abc")["total"] == 0
    assert verify.trace_numbers("123", "")["total"] == 0


# ---------------------------------------------------------------- 选题雷达

ATOM_ENTRY = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2609.38476v1</id>
    <title>Surface Defect Detection with
      Multi-line Transformer</title>
    <updated>2026-09-29T20:06:52Z</updated>
    <published>2026-09-29T20:06:52Z</published>
    <link href="https://arxiv.org/abs/2609.38476v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2609.38476v1" rel="related" type="application/pdf" title="pdf"/>
    <summary>We study   surface defect detection.</summary>
    <author><name>Ada L</name></author>
    <author><name>Bo C</name></author>
    <category term="cs.CV"/>
    <arxiv:comment>Accepted to CVPR 2025. Code: github.com/x/y</arxiv:comment>
  </entry>
</feed>
"""


def _entry():
    import xml.etree.ElementTree as ET

    return ET.fromstring(ATOM_ENTRY).find("atom:entry", radar.NS)


def test_radar_parses_entry_fields():
    candidate = radar._to_candidate(_entry(), "surface defect detection")
    assert candidate["external_id"] == "2609.38476v1"
    assert candidate["title"] == "Surface Defect Detection with Multi-line Transformer"
    assert candidate["authors"] == "Ada L, Bo C"
    assert candidate["pdf_url"] == "https://arxiv.org/pdf/2609.38476v1"
    assert candidate["url"] == "https://arxiv.org/abs/2609.38476v1"
    assert candidate["published"] == "2026-09-29"
    assert candidate["categories"] == "cs.CV"


def test_radar_scores_venue_code_and_title_match():
    candidate = radar._to_candidate(_entry(), "surface defect detection")
    reasons = candidate["reasons"]
    assert any("CVPR" in reason for reason in reasons)
    assert "附开源代码" in reasons
    assert "标题直接命中关键词" in reasons
    assert candidate["score"] >= 7


def test_radar_score_recency_buckets():
    def score_for(published: str) -> int:
        candidate = {
            "title": "t",
            "comment": "",
            "abstract": "",
            "keyword": "x",
            "published": published,
        }
        return radar.score_candidate(candidate, today=__import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").timezone.utc))[0]

    assert score_for("2026-09-30") == 3
    assert score_for("2026-09-20") == 2
    assert score_for("2026-08-01") == 1
    assert score_for("2026-01-01") == 0


def test_radar_too_old_filters():
    assert radar._too_old("2026-01-01", 30) is True
    assert radar._too_old("2026-09-30", 30) is False
    assert radar._too_old("2026-01-01", 0) is False


# ---------------------------------------------------------------- 结构模板


def test_presets_all_have_structure_and_words():
    for key, preset in prompts.ARTICLE_PRESETS.items():
        assert preset["structure"].startswith("```"), key
        assert preset["structure"].endswith("```"), key
        assert "字" in preset["words"], key
        assert preset["name"], key


def test_build_article_messages_injects_preset_structure():
    reading = {"title": "T", "content": "正文"}
    system = prompts.build_article_messages(reading, None, preset="brief")[0]["content"]
    assert "{{STRUCTURE}}" not in system
    assert "## 01 一句话结论" in system
    assert "全文 900–1300 字" in system
    assert "## 01 导读" not in system


def test_build_article_messages_unknown_preset_falls_back():
    system = prompts.build_article_messages({"title": "T", "content": "x"}, None, preset="??")[0]["content"]
    assert "## 01 导读" in system


def test_build_article_messages_mentions_article_type_in_user_prompt():
    messages = prompts.build_article_messages({"title": "T", "content": "x"}, None, preset="compare")
    assert "文章类型：方法对比" in messages[1]["content"]


# ---------------------------------------------------------------- GitHub 榜单


def test_parse_item_normalizes_repo():
    raw = {
        "full_name": "cleanlab/cleanlab",
        "html_url": "https://github.com/cleanlab/cleanlab",
        "stargazers_count": 11715,
        "forks_count": 900,
        "language": "Python",
        "description": "  Standard data-centric AI library.  ",
        "topics": ["anomaly-detection", "machine-learning", "extra4", "extra5", "extra6", "extra7", "extra8"],
        "pushed_at": "2026-10-01T12:00:00Z",
        "owner": {"login": "cleanlab"},
    }
    item = github_boards.parse_item(raw)
    assert item["stars"] == 11715
    assert item["description"] == "Standard data-centric AI library."
    assert item["pushed_at"] == "2026-10-01"
    # topics 截断到 6 个
    assert len(item["topics"].split(", ")) == 6


def test_parse_item_tolerates_missing_fields():
    item = github_boards.parse_item({})
    assert item["stars"] == 0
    assert item["full_name"] == ""
    assert item["language"] == ""


def test_default_boards_avoid_readme_matching():
    """裸关键词 + in:readme 会把任意提过该词的仓库捞进来，预置榜必须避开。"""
    for board in github_boards.DEFAULT_BOARDS:
        query = board["query"]
        assert "in:readme" not in query, board["key"]
        assert "topic:" in query or "in:name" in query or "in:description" in query
        assert board["name"] and board["key"]


def test_fetch_board_rejects_empty_query():
    try:
        github_boards.fetch_board("   ")
    except github_boards.GitHubError as exc:
        assert "为空" in str(exc)
    else:
        raise AssertionError("应当拒绝空查询式")


def test_rate_limit_wait_hint():
    import time as _time

    err = github_boards.RateLimitError("限流", reset_epoch=int(_time.time()) + 42)
    assert "秒后恢复" in err.wait_hint()
    assert github_boards.RateLimitError("限流").wait_hint() == "稍后再试"


def test_roundup_messages_include_board_and_repos():
    repos = [
        {"full_name": "a/b", "stars": 1200, "language": "Python",
         "description": "第一个项目", "pushed_at": "2026-09-01"},
        {"full_name": "c/d", "stars": 300, "language": "Rust",
         "description": "", "pushed_at": ""},
    ]
    messages = prompts.build_roundup_messages(
        "工业异常检测", repos, board_query="topic:anomaly-detection", note="测试"
    )
    system, user = messages[0]["content"], messages[1]["content"]
    assert "开源项目榜单盘点" in system
    assert "榜单名称：工业异常检测" in user
    assert "1. a/b | ★1200 | Python" in user
    assert "2. c/d | ★300 | Rust" in user


# ---------------------------------------------------------------- 周资讯


def test_weekly_messages_include_gains_and_papers():
    github = [
        {"full_name": "a/b", "stars": 1000, "week_delta": 25,
         "language": "Python", "description": "涨得不错"},
    ]
    papers = [
        {"title": "Paper X", "published": "2026-10-20",
         "keyword": "anomaly detection", "abstract": "摘要正文"},
    ]
    messages = prompts.build_weekly_messages("AI 开源总榜", github, papers, board_query="topic:ai")
    system, user = messages[0]["content"], messages[1]["content"]
    assert "本周资讯周报" in system
    assert "榜单来源：AI 开源总榜" in user
    assert "本周 +25 star" in user
    assert "Paper X" in user
    assert "发布 2026-10-20" in user


def test_weekly_messages_mark_missing_gain():
    github = [{"full_name": "x/y", "stars": 10, "week_delta": 0, "language": "", "description": ""}]
    user = prompts.build_weekly_messages("B", github, [])[1]["content"]
    assert "本周涨幅未计" in user


def test_weekly_messages_handle_empty_material():
    user = prompts.build_weekly_messages("B", [], [])[1]["content"]
    assert "（本周没有可用的榜单数据）" in user
    assert "（本周没有新增论文候选）" in user


def test_weekly_preset_structure():
    key, preset = prompts.get_preset("weekly")
    assert key == "weekly"
    assert "## 01 本周概览" in preset["structure"]
    assert "## 02 开源动态：本周涨星" in preset["structure"]
    assert preset["min"] < preset["max"]
