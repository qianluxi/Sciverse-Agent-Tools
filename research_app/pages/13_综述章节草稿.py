from __future__ import annotations

import streamlit as st

from research_app import storage
from research_app.ai import AIError, OpenAICompatibleClient
from research_app.ai_settings import default_profile, record_ai_profile
from research_app.project_ui import project_selector
from research_app.review_chapter import (
    CHAPTER_PROMPT_VERSION,
    build_sources,
    check_summary,
    citation_report_rows,
    export_citation_report_csv,
    generate_review_chapter,
    render_chapter_markdown,
)


st.set_page_config(page_title="综述章节草稿 · Sciverse", page_icon="S", layout="wide")

st.title("综述章节草稿")
st.caption(
    "基于项目内已纳入文献与证据矩阵，生成一篇论文的文献综述章节草稿。"
    "生成时会校验引证范围：越界编号、漏引的全文证据、仅题摘引用、无引证段落都会标出。"
    "草稿只在本页会话与下载文件中存在，不写入数据库，也不等于最终论文。"
)

_profile = default_profile()

with st.sidebar:
    st.markdown("### 项目")
    project_id = project_selector("chapter_project_selector")
    st.divider()
    st.markdown("### AI 服务（OpenAI 兼容）")
    base_url = st.text_input(
        "Base URL",
        value=st.session_state.get("ai_base_url") or _profile["base_url"],
        key="chapter_base_url",
    ).strip()
    ai_key = st.text_input(
        "API Key",
        type="password",
        value=st.session_state.get("ai_key_session", ""),
        key="chapter_api_key",
        help="只在当前浏览器会话使用，不写入本地数据库。",
    )
    st.session_state["ai_key_session"] = ai_key
    model = st.text_input(
        "模型名",
        value=st.session_state.get("ai_model") or _profile["model"],
        key="chapter_model",
    ).strip()
    st.caption(f"章节提示词版本：{CHAPTER_PROMPT_VERSION}")

if not project_id:
    st.info("请先在「研究项目」页创建或选择一个项目。")
    st.stop()

project = storage.get_screening_project(project_id)
matrix = storage.load_evidence_matrix(project_id)
candidates = storage.list_all_screening_candidates(project_id)
sources = build_sources(matrix, candidates)
fulltext_sources = [item for item in sources if item["evidence_level"] == "fulltext"]
abstract_sources = [item for item in sources if item["evidence_level"] != "fulltext"]

st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")

metrics = st.columns(4)
metrics[0].metric("题摘纳入", len(sources))
metrics[1].metric("全文已核验", len(fulltext_sources))
metrics[2].metric("仅题摘", len(abstract_sources))
metrics[3].metric("证据矩阵条目", len(matrix))

if not sources:
    st.warning(
        "当前项目没有题名/摘要纳入的文献。请先在「系统综述筛选」完成人工裁决，"
        "再到「项目证据矩阵」补齐全文证据。"
    )
    st.stop()

st.markdown("#### 可用来源")
st.dataframe(
    [
        {
            "编号": item["key"],
            "题名": item["title"],
            "年份": str(item["year"]).replace(".0", ""),
            "证据级别": "全文已核验" if item["evidence_level"] == "fulltext" else "仅题摘",
            "逐字引句": len(item["quotes"]),
        }
        for item in sources
    ],
    hide_index=True,
    use_container_width=True,
)

if not fulltext_sources:
    st.error(
        "当前项目没有完成全文核验的证据。此时生成章节只会得到无法核查的空话，"
        "因此本页不提供生成按钮。请先在「项目证据矩阵」补齐全文证据，或使用「AI 证据与提纲」页生成提纲。"
    )
    st.stop()

st.markdown("#### 生成设置")
chapter_title = st.text_input("章节标题", value="第 2 章 文献综述", key="chapter_title")
question = st.text_area(
    "研究问题",
    value=str(project.get("question") or ""),
    height=80,
    key="chapter_question",
)

disabled = not base_url or not ai_key or not model
if disabled:
    st.caption("请先在左侧填写 Base URL、API Key 与模型名。")

if st.button("生成章节草稿", type="primary", disabled=disabled, key="chapter_generate"):
    try:
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=ai_key,
            model=model,
        )
        record_ai_profile(base_url, model)
        with st.spinner("正在生成章节草稿并校验引证…"):
            chapter, check = generate_review_chapter(
                client,
                question,
                sources,
                chapter_title=chapter_title,
            )
        st.session_state["chapter_text"] = chapter
        st.session_state["chapter_check"] = check
        st.session_state["chapter_meta"] = {
            "model": model,
            "project_id": project_id,
            "chapter_title": chapter_title,
            "question": question,
        }
    except (AIError, ValueError) as error:
        st.error(str(error))
    except Exception as error:  # pragma: no cover - 兜底，避免整页崩溃
        st.error(f"生成失败：{type(error).__name__}: {error}")

chapter = st.session_state.get("chapter_text", "")
check = st.session_state.get("chapter_check")
meta = st.session_state.get("chapter_meta") or {}

if chapter and check:
    st.markdown("### 引证核查")
    for message in check_summary(check):
        if check["invalid"]:
            st.error(message)
        elif message.startswith("引证检查通过"):
            st.success(message)
        else:
            st.warning(message)

    st.dataframe(
        citation_report_rows(sources, check),
        hide_index=True,
        use_container_width=True,
    )

    st.markdown("### 章节草稿")
    st.markdown(chapter)

    downloads = st.columns(2)
    with downloads[0]:
        st.download_button(
            "下载章节 Markdown",
            data=render_chapter_markdown(
                chapter,
                question=meta.get("question", question),
                chapter_title=meta.get("chapter_title", chapter_title),
                model=meta.get("model", model),
                project_id=meta.get("project_id", project_id),
                source_count=len(sources),
                fulltext_count=len(fulltext_sources),
            ),
            file_name="sciverse-review-chapter.md",
            mime="text/markdown",
            key="download_chapter_markdown",
        )
    with downloads[1]:
        st.download_button(
            "下载引证核查 CSV",
            data=export_citation_report_csv(sources, check),
            file_name="sciverse-chapter-citations.csv",
            mime="text/csv",
            key="download_chapter_citations",
        )
    st.caption(
        "草稿为 AI 辅助产物：引证校验只能确认编号是否在来源清单内，"
        "不能确认论断是否忠实于原文。请逐条核对后再进入论文。"
    )

    if st.button("清空章节草稿", key="chapter_clear"):
        st.session_state.pop("chapter_text", None)
        st.session_state.pop("chapter_check", None)
        st.session_state.pop("chapter_meta", None)
        st.rerun()
