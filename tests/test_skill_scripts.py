"""守卫：脚本式 skill 必须自己翻译便捷参数、自己补分页默认值。

踩过的两个坑：

1. ClawHub 的 `search_papers.mjs` 把入参原样透传，`authors` / `year_from` /
   `sort_advanced` / `filters_advanced` 这些便利字段后端不认、静默丢弃——
   看起来检索成功，实际条件没生效。
2. `read_content.mjs` 不传 offset 时服务端返回整篇全文并忽略 limit，
   一次调用就可能把几十万码点塞进模型上下文。

两份 skill bundle（clawhub / web）各自独立分发，无法共享文件，所以两处都要有。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIRS = (
    ROOT / "clawhub" / "scripts",
    ROOT / "generators" / "web_skill_assets" / "scripts",
)


def test_search_papers_script_translates_convenience_params():
    for scripts in SCRIPT_DIRS:
        common = (scripts / "_common.mjs").read_text(encoding="utf-8")
        assert "export function toMetaSearchPayload" in common, (
            f"{scripts.parent.name}/_common.mjs 缺少 toMetaSearchPayload"
        )
        for key in ("authors", "year_from", "year_to", "journals", "subjects"):
            assert key in common, f"{scripts.parent.name} 未翻译便捷字段 {key}"

        script = (scripts / "search_papers.mjs").read_text(encoding="utf-8")
        assert "toMetaSearchPayload(" in script, (
            f"{scripts.parent.name}/search_papers.mjs 未调用 toMetaSearchPayload——"
            " 便捷参数会被后端静默丢弃"
        )


def test_read_content_script_sets_pagination_defaults():
    for scripts in SCRIPT_DIRS:
        script = (scripts / "read_content.mjs").read_text(encoding="utf-8")
        assert "offset: args.offset ??" in script, (
            f"{scripts.parent.name}/read_content.mjs 未补默认 offset——服务端会返回整篇全文"
        )
        assert "limit: args.limit ??" in script, (
            f"{scripts.parent.name}/read_content.mjs 未补默认 limit"
        )
