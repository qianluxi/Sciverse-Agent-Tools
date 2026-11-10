"""综述章节草稿：在页面内完成「生成 → 逐节改 → 补表 → 补线索 → 拼装导出」。

设计取向：普通用户不需要打开任何 md 文件或 Excel。模型只写有证据支撑的正文，
表格与「未纳入线索」由程序按数据拼装，人只负责改判断、补书目、调措辞。
"""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from research_app import storage
from research_app.ai import AIError, OpenAICompatibleClient
from research_app.ai_settings import default_profile, record_ai_profile
from research_app.project_ui import project_selector
from research_app.review_chapter import (
    CHAPTER_PROMPT_VERSION,
    DEFAULT_SECTION_PRESETS,
    EVIDENCE_SOURCE_LABELS,
    SECTION_GAPS,
    SECTION_LEADS,
    assemble_chapter,
    build_citation_table_rows,
    build_excluded_lead_text,
    build_gap_table_rows,
    build_number_table_rows,
    build_sources,
    check_citations,
    check_summary,
    citation_report_rows,
    draft_from_json,
    draft_to_json,
    evidence_level_label,
    excluded_lead_options,
    export_citation_report_csv,
    generate_review_chapter,
    new_section,
    prisma_digest,
    render_chapter_markdown,
    sections_from_markdown,
)


st.set_page_config(page_title="综述章节草稿 · Sciverse", page_icon="S", layout="wide")

st.title("综述章节草稿")
st.caption(
    "从项目内已纳入文献与证据矩阵起草综述章节，并在本页完成逐节修改、表格补全与引证核查。"
    "草稿只存在于当前会话与下载文件中，不写入数据库，也不等于最终论文。"
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
prisma = storage.prisma_counts(project_id)
_state_key = f"chapter_ws::{project_id}"
_previous = st.session_state.get(_state_key) or {}
# 已有草稿里的编号要沿用，否则新增一篇纳入文献就会把正文里的 [Cxxx] 全部错位。
_key_map = {
    item["candidate_key"]: item["key"]
    for item in (_previous.get("sources") or [])
    if item.get("candidate_key") and item.get("key")
}
sources = build_sources(
    matrix,
    candidates,
    papers=storage.project_papers(project_id),
    key_map=_key_map,
)
fulltext_sources = [item for item in sources if item["evidence_level"] == "fulltext"]
confirmed_sources = [item for item in fulltext_sources if item["evidence_confirmed"]]
pending_sources = [item for item in fulltext_sources if not item["evidence_confirmed"]]
abstract_sources = [item for item in sources if item["evidence_level"] != "fulltext"]

st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")

if not sources:
    st.warning(
        "当前项目没有题名/摘要纳入的文献。请先在「系统综述筛选」完成人工裁决，"
        "再到「项目证据矩阵」补齐全文证据。"
    )
    st.stop()

metrics = st.columns(5)
metrics[0].metric("题摘纳入", len(sources))
metrics[1].metric("全文已核验", len(confirmed_sources))
metrics[2].metric("全文待确认", len(pending_sources))
metrics[3].metric("仅题摘", len(abstract_sources))
metrics[4].metric("证据矩阵条目", len(matrix))

if not fulltext_sources:
    st.error(
        "当前项目没有任何全文证据（仅有题摘）。此时生成章节只会得到无法核查的空话，"
        "因此本页不提供生成按钮。请先在「项目证据矩阵」补齐全文证据，或使用「AI 证据与提纲」页生成提纲。"
    )
    st.stop()

if not confirmed_sources:
    st.warning(
        f"当前 {len(pending_sources)} 篇全文证据均为 AI 原始提取、未经人工确认。"
        "生成时会把它们标为「全文已提取（待确认）」，草稿中的方法与数值仍须逐条核对原文后才能写入论文。"
        "建议先在「项目证据矩阵」完成人工校正或最终采用。"
    )


def _default_tables(items: list[dict], prisma_data: dict) -> list[dict]:
    return [
        {
            "id": "cite",
            "title": "纳入文献的证据级别与可支持的陈述",
            "columns": ["编号", "文献", "证据级别", "可支持的陈述"],
            "rows": build_citation_table_rows(items),
            "anchor": "end",
            "fixed_rows": True,
        },
        {
            "id": "gaps",
            "title": "研究空白与补足路径",
            "columns": ["研究空白", "证据现状", "补足路径"],
            "rows": build_gap_table_rows(items, prisma_data),
            "anchor": "end",
            "fixed_rows": False,
        },
        {
            "id": "nums",
            "title": "本章数字口径与出处",
            "columns": ["指标", "数值", "出处"],
            "rows": build_number_table_rows(prisma_data, items),
            "anchor": "end",
            "fixed_rows": False,
        },
    ]


_state_key = f"chapter_ws::{project_id}"
if _state_key not in st.session_state:
    st.session_state[_state_key] = {
        "sources": sources,
        "sections": [],
        "tables": _default_tables(sources, prisma),
        "leads": [],
        "chapter_title": "第 2 章 文献综述",
        "question": str(project.get("question") or ""),
        "model": "",
        "generated_text": "",
    }
state = st.session_state[_state_key]
# 书目信息可能在别的页面补过，这里每次以最新构建结果为准，但保留用户手填的作者/期刊。
_latest = {item["key"]: item for item in sources}
for item in state["sources"]:
    fresh = _latest.get(item["key"])
    if fresh:
        for field in ("authors", "venue", "year"):
            if item.get(field):
                fresh[field] = item[field]
state["sources"] = list(_latest.values())

tab_src, tab_gen, tab_tbl, tab_lead, tab_out = st.tabs(
    ["① 来源与书目", "② 生成与逐节编辑", "③ 表格", "④ 未纳入线索", "⑤ 拼装与导出"]
)

# ---------------------------------------------------------------- ① 来源与书目
with tab_src:
    st.markdown("#### 来源清单")
    st.caption(
        "作者与期刊自动取自检索快照；检索时未返回的作者、期刊会显示「（待补）」，"
        "可直接在下表修改（例如照原文补全）。这里的修改会进入正文的文献指称与表 2-1。"
    )
    source_frame = pd.DataFrame(
        [
            {
                "编号": item["key"],
                "题名": item["title"],
                "作者": item.get("authors") or "",
                "期刊": item.get("venue") or "",
                "年份": str(item.get("year") or "").replace(".0", ""),
                "证据级别": evidence_level_label(item),
                "证据版本": EVIDENCE_SOURCE_LABELS.get(
                    item["evidence_source"], item["evidence_source"] or "未标注"
                ),
                "逐字引句": len(item.get("quotes") or []),
            }
            for item in state["sources"]
        ],
        columns=["编号", "题名", "作者", "期刊", "年份", "证据级别", "证据版本", "逐字引句"],
    )
    edited = st.data_editor(
        source_frame,
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        disabled=["编号", "题名", "证据级别", "证据版本", "逐字引句"],
        key=f"chapter_source_editor::{project_id}::{len(state['sources'])}",
    )
    by_key = {item["key"]: item for item in state["sources"]}
    for _, row in edited.iterrows():
        item = by_key.get(str(row["编号"]))
        if not item:
            continue
        item["authors"] = str(row["作者"] or "").strip()
        item["venue"] = str(row["期刊"] or "").strip()
        item["year"] = str(row["年份"] or "").strip()

    missing = [item["key"] for item in state["sources"] if not item.get("authors") or not item.get("venue")]
    if missing:
        st.info(
            f"{len(missing)} 篇缺少作者或期刊（{'、'.join(missing[:8])}{'…' if len(missing) > 8 else ''}）。"
            "不影响正文生成，但表 2-1 与参考文献著录需要补全。"
        )

# ------------------------------------------------------------ ② 生成与逐节编辑
with tab_gen:
    st.markdown("#### 生成设置")
    controls = st.columns([2, 1])
    chapter_title = controls[0].text_input(
        "章节标题",
        value=state.get("chapter_title") or "第 2 章 文献综述",
        key=f"chapter_title::{project_id}",
    )
    state["chapter_title"] = chapter_title
    question = st.text_area(
        "研究问题",
        value=state.get("question") or "",
        height=80,
        key=f"chapter_question::{project_id}",
    )
    state["question"] = question

    st.caption(f"将随提示词一起交给模型：{prisma_digest(prisma)}")

    disabled = not base_url or not ai_key or not model
    if disabled:
        st.caption("请先在左侧填写 Base URL、API Key 与模型名。")
    if state["sections"]:
        st.warning(
            "重新生成会覆盖下面所有小节编辑。需要保留时，先到「⑤ 拼装与导出」下载草稿 JSON。"
        )

    if st.button("生成章节草稿", type="primary", disabled=disabled, key="chapter_generate"):
        try:
            client = OpenAICompatibleClient(base_url=base_url, api_key=ai_key, model=model)
            record_ai_profile(base_url, model)
            with st.spinner("正在生成章节草稿…"):
                chapter, _ = generate_review_chapter(
                    client,
                    question,
                    state["sources"],
                    chapter_title=chapter_title,
                    prisma=prisma,
                )
            state["sections"] = sections_from_markdown(chapter)
            state["generated_text"] = chapter
            state["model"] = model
            st.success(f"已生成 {len(state['sections'])} 个小节，可在下方逐节修改。")
        except (AIError, ValueError) as error:
            st.error(str(error))
        except Exception as error:  # pragma: no cover - 兜底，避免整页崩溃
            st.error(f"生成失败：{type(error).__name__}: {error}")

    st.divider()
    st.markdown("#### 逐节编辑")
    if not state["sections"]:
        st.info("生成后可在此逐节修改标题与正文；也可以先手动添加小节，从零搭一章。")
    else:
        st.caption("修改即时生效，「⑤ 拼装与导出」按当前内容重新编号（2.1、2.2……）。")
        table_like = [
            item["heading"]
            for item in state["sections"]
            if re.search(r"(?m)^\s*\|.*\|", item.get("body") or "")
        ]
        if table_like:
            st.info(
                "这些小节正文里带 Markdown 表格：" + "、".join(table_like) + "。"
                "表格建议交给「③ 表格」插入，正文里的表可删除，避免出现两张重复的表。"
            )

    for index, section in enumerate(list(state["sections"])):
        sid = section["id"]
        with st.expander(f"2.{index + 1}　{section['heading']}", expanded=index == 0):
            heading = st.text_input(
                "小节标题",
                value=section["heading"],
                key=f"chapter_h_{sid}",
            )
            body = st.text_area(
                "小节正文（可直接修改；[Cxxx] 必须是来源清单里的编号）",
                value=section["body"],
                height=260,
                key=f"chapter_b_{sid}",
            )
            section["heading"] = heading.strip() or section["heading"]
            section["body"] = body
            actions = st.columns([1, 1, 1, 3])
            if actions[0].button("上移", key=f"up_{sid}", disabled=index == 0):
                state["sections"].insert(index - 1, state["sections"].pop(index))
                st.rerun()
            if actions[1].button(
                "下移", key=f"down_{sid}", disabled=index == len(state["sections"]) - 1
            ):
                state["sections"].insert(index + 1, state["sections"].pop(index))
                st.rerun()
            if actions[2].button("删除", key=f"del_{sid}"):
                state["sections"].pop(index)
                st.rerun()

    with st.form(key=f"chapter_add_section::{project_id}", clear_on_submit=True):
        st.markdown("**新增小节**")
        preset = st.selectbox(
            "预设标题",
            ["自定义"] + [name for name in DEFAULT_SECTION_PRESETS] + [SECTION_LEADS],
            key=f"chapter_add_preset::{project_id}",
        )
        custom = st.text_input("自定义标题（选「自定义」时使用）", key=f"chapter_add_title::{project_id}")
        new_body = st.text_area("初始正文（可留空）", height=120, key=f"chapter_add_body::{project_id}")
        if st.form_submit_button("添加小节"):
            title = custom.strip() if preset == "自定义" else preset
            if not title.strip():
                st.warning("请填写小节标题。")
            else:
                state["sections"].append(new_section(title.strip(), new_body.strip()))
                st.rerun()

# ---------------------------------------------------------------------- ③ 表格
with tab_tbl:
    st.markdown("#### 表格")
    st.caption(
        "三张表由数据自动生成初值，可逐格修改、增删行。表格不交给模型写，避免出现无出处的数字。"
        "放置位置决定它出现在哪一小节之后，编号按出现顺序自动排为表 2-1、2-2……"
    )
    anchor_options = [("end", "章末")] + [
        (section["id"], f"2.{index + 1} {section['heading']}")
        for index, section in enumerate(state["sections"])
    ]
    anchor_ids = [value for value, _ in anchor_options]
    anchor_lookup = dict(anchor_options)
    for table in state["tables"]:
        with st.expander(f"表　{table['title']}", expanded=False):
            table["title"] = st.text_input(
                "表题", value=table["title"], key=f"chapter_tbl_title_{table['id']}"
            )
            if table.get("anchor") not in anchor_ids:
                table["anchor"] = "end"
            select_key = f"chapter_tbl_anchor_{table['id']}"
            # 小节被删除后旧锚点会失效，先清掉控件状态再渲染，避免 selectbox 报错。
            if st.session_state.get(select_key) not in (None, *anchor_ids):
                st.session_state.pop(select_key, None)
            table["anchor"] = st.selectbox(
                "放置位置",
                anchor_ids,
                index=anchor_ids.index(table["anchor"]),
                format_func=lambda value: anchor_lookup.get(value, "章末"),
                key=select_key,
            )
            frame = pd.DataFrame(table["rows"], columns=table["columns"])
            edited_table = st.data_editor(
                frame,
                hide_index=True,
                use_container_width=True,
                num_rows="fixed" if table.get("fixed_rows") else "dynamic",
                key=f"chapter_tbl_editor_{table['id']}",
            )
            table["rows"] = [
                {column: row.get(column, "") for column in table["columns"]}
                for row in edited_table.to_dict("records")
            ]
            if st.button("按当前数据重置本表", key=f"chapter_tbl_reset_{table['id']}"):
                if table["id"] == "cite":
                    table["rows"] = build_citation_table_rows(state["sources"])
                elif table["id"] == "gaps":
                    table["rows"] = build_gap_table_rows(state["sources"], prisma)
                else:
                    table["rows"] = build_number_table_rows(prisma, state["sources"])
                st.rerun()

# -------------------------------------------------------------- ④ 未纳入线索
with tab_lead:
    st.markdown("#### 检索中发现但未纳入的线索")
    st.caption(
        "模型看不到被排除的文献，让它写这一节必然编造，所以这段由程序按你勾选的记录生成："
        "只陈述「检索到但依标准排除」，不带 [Cxxx] 引证、不给结论。"
    )
    options = excluded_lead_options(candidates)
    if not options:
        st.info("当前项目没有被排除的记录，这一节通常可以跳过。")
    else:
        reasons = sorted({item["reason"] for item in options})
        reason = st.selectbox("按排除原因筛选", ["全部"] + reasons, key=f"chapter_lead_reason::{project_id}")
        visible = [item for item in options if reason == "全部" or item["reason"] == reason]
        label = {item["key"]: f"{item['title']}（{item['year'] or '年份未知'}）· {item['reason']}" for item in visible}
        selected = st.multiselect(
            "选择要提及的记录",
            options=[item["key"] for item in visible],
            format_func=lambda key: label.get(key, key),
            key=f"chapter_lead_pick::{project_id}",
        )
        st.caption(f"当前筛选下共 {len(visible)} 条；全项目共排除 {len(options)} 条。")
        if st.button("生成／更新这一节", key="chapter_lead_build"):
            chosen = [item for item in options if item["key"] in set(selected)]
            if not chosen:
                st.warning("请至少选择一条记录。")
            else:
                text = build_excluded_lead_text(chosen, len(options))
                target = next(
                    (item for item in state["sections"] if item["heading"] == SECTION_LEADS),
                    None,
                )
                if target:
                    target["body"] = text
                else:
                    gaps_index = next(
                        (
                            position
                            for position, item in enumerate(state["sections"])
                            if item["heading"] == SECTION_GAPS
                        ),
                        len(state["sections"]),
                    )
                    state["sections"].insert(gaps_index, new_section(SECTION_LEADS, text))
                state["leads"] = list(selected)
                st.success("已写入小节；可在「② 生成与逐节编辑」继续润色。")

# -------------------------------------------------------------- ⑤ 拼装与导出
with tab_out:
    st.markdown("#### 拼装结果")
    assembled = assemble_chapter(state["sections"], state["tables"]) if state["sections"] else ""
    if not assembled.strip():
        st.info("还没有可拼装的内容：先在上面的分区生成或手动添加小节。表格会在有正文后一并拼入。")
    else:
        check = check_citations(assembled, state["sources"])
        stats = st.columns(4)
        stats[0].metric("小节数", len(state["sections"]))
        stats[1].metric("中文字数", len(re.findall(r"[\u4e00-\u9fff]", assembled)))
        stats[2].metric("引证次数", check["citation_count"])
        stats[3].metric("表格数", len(state["tables"]))

        st.markdown("##### 引证核查")
        for message in check_summary(check):
            if check["invalid"]:
                st.error(message)
            elif message.startswith("引证检查通过"):
                st.success(message)
            else:
                st.warning(message)

        with st.expander("逐篇引证核查表", expanded=False):
            st.dataframe(
                citation_report_rows(state["sources"], check),
                hide_index=True,
                use_container_width=True,
            )

        with st.expander("预览（Markdown 渲染）", expanded=False):
            st.markdown(assembled)

        edited_flag = bool(state.get("generated_text")) and assembled.strip() != state["generated_text"].strip()
        downloads = st.columns(3)
        with downloads[0]:
            st.download_button(
                "下载章节 Markdown",
                data=render_chapter_markdown(
                    assembled,
                    question=state.get("question", ""),
                    chapter_title=state.get("chapter_title", ""),
                    model=state.get("model", "") or "人工编辑",
                    project_id=project_id,
                    source_count=len(state["sources"]),
                    fulltext_count=len(fulltext_sources),
                    human_edited=edited_flag,
                ),
                file_name="sciverse-review-chapter.md",
                mime="text/markdown",
                key="download_chapter_markdown",
            )
        with downloads[1]:
            st.download_button(
                "下载引证核查 CSV",
                data=export_citation_report_csv(state["sources"], check),
                file_name="sciverse-chapter-citations.csv",
                mime="text/csv",
                key="download_chapter_citations",
            )
        with downloads[2]:
            st.download_button(
                "下载草稿 JSON（可再导入）",
                data=draft_to_json(state),
                file_name="sciverse-chapter-draft.json",
                mime="application/json",
                key="download_chapter_draft",
            )

        st.caption(
            "引证核查只能确认编号是否在来源清单内、是否有漏引，"
            "不能确认论断是否忠实于原文；表 2-1 的「可支持的陈述」也需按原文复核。"
        )

    st.divider()
    st.markdown("#### 恢复草稿")
    uploaded = st.file_uploader(
        "导入草稿 JSON（在本页继续编辑）",
        type=["json"],
        key=f"chapter_draft_upload::{project_id}",
    )
    if uploaded is not None and st.button("导入并覆盖当前草稿", key="chapter_draft_restore"):
        try:
            payload = draft_from_json(uploaded.getvalue())
        except ValueError as error:
            st.error(str(error))
        else:
            state["sections"] = payload["sections"]
            state["tables"] = payload["tables"] or state["tables"]
            state["leads"] = payload["leads"]
            state["chapter_title"] = payload["chapter_title"] or state["chapter_title"]
            state["question"] = payload["question"] or state["question"]
            for item in payload["sources"]:
                for source in state["sources"]:
                    if source["key"] == item.get("key"):
                        source["authors"] = item.get("authors") or source.get("authors", "")
                        source["venue"] = item.get("venue") or source.get("venue", "")
                        source["year"] = item.get("year") or source.get("year", "")
            st.success("已导入草稿。")
            st.rerun()

    if st.button("清空当前草稿", key="chapter_clear"):
        st.session_state.pop(_state_key, None)
        st.rerun()
