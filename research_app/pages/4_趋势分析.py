from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from sciverse.credentials import resolve_token

from research_app.service import call_api
from research_app.trends import analyze_papers, export_trends_png, export_trends_zip


st.set_page_config(page_title="趋势分析 · Sciverse", page_icon="S", layout="wide")

st.title("趋势分析")
st.caption("基于当前检索样本统计年份、期刊、作者、学科和关键词；所有图表都附带样本范围与缺失率。")

with st.sidebar:
    entered_token = st.text_input("API Token", type="password", placeholder="sci_...")
    token = entered_token.strip() or resolve_token()
    if not token:
        st.info("扩展采样需要 Sciverse API Token；分析当前结果不需要。")


def _show_error(error: Exception) -> None:
    st.error(str(error))


current_papers = st.session_state.get("papers", [])
if not current_papers:
    st.info("主页还没有检索结果。请先完成一次检索，再回到此页做趋势分析。")
    st.stop()

search_kind = st.session_state.get("search_kind")
search_params = st.session_state.get("search_params", {})
st.markdown("### 样本范围")
st.caption(
    f"检索方式：{'条件筛选' if search_kind == 'structured' else '语义检索'} · "
    f"检索条件：`{search_params}`"
)

sample = st.session_state.get("trend_sample")
if sample:
    source_label = st.session_state.get("trend_source", "当前检索结果 + 扩展采样")
    requests_used = st.session_state.get("trend_requests", 0)
    truncated = st.session_state.get("trend_truncated", False)
else:
    sample = current_papers
    source_label = "当前检索结果"
    requests_used = 1
    truncated = False

sample_cols = st.columns([1, 1, 2])
with sample_cols[0]:
    if search_kind == "structured":
        pages = st.number_input("扩展页数（每页 50）", min_value=1, max_value=5, value=1)
    else:
        pages = 1
        st.caption("语义检索不支持按页扩展样本。")
with sample_cols[1]:
    extend_clicked = st.button(
        "扩展采集",
        disabled=search_kind != "structured" or not token,
    )
with sample_cols[2]:
    if sample is not current_papers and st.button("重置为当前结果"):
        st.session_state.pop("trend_sample", None)
        st.session_state.pop("trend_source", None)
        st.session_state.pop("trend_requests", None)
        st.session_state.pop("trend_truncated", None)
        st.rerun()

if extend_clicked and token and search_kind == "structured":
    try:
        all_papers = list(current_papers)
        requests_made = 0
        truncated = False
        with st.spinner("正在扩展样本…"):
            for page in range(1, int(pages) + 1):
                params = {**search_params, "page": page, "page_size": 50}
                result = call_api(token, "search_papers", **params)
                requests_made += 1
                results = result.get("results", [])
                all_papers.extend(results)
                if len(results) < 50:
                    truncated = False
                    break
            else:
                truncated = True
        st.session_state["trend_sample"] = all_papers
        st.session_state["trend_source"] = f"当前检索结果 + 扩展 {requests_made} 页"
        st.session_state["trend_requests"] = requests_made
        st.session_state["trend_truncated"] = truncated
        st.rerun()
    except Exception as error:
        _show_error(error)

analysis = analyze_papers(sample)
scope = {
    "source": source_label,
    "search_kind": search_kind,
    "search_params": str(search_params),
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "sample_size": analysis["sample_size"],
    "duplicates_removed": analysis["duplicates_removed"],
    "identifier_conflicts": analysis["identifier_conflicts"],
    "requests": requests_used,
    "truncated": truncated,
    "year_range": analysis["year_range"],
}

metric_cols = st.columns(4)
metric_cols[0].metric("去重后样本", analysis["sample_size"])
metric_cols[1].metric("重复记录", analysis["duplicates_removed"])
metric_cols[2].metric(
    "年份范围",
    f"{analysis['year_range'][0]}–{analysis['year_range'][1]}"
    if analysis["year_range"]
    else "未知",
)
metric_cols[3].metric("请求次数", requests_used)
if truncated:
    st.warning("扩展采样达到页数上限，结果只是部分样本，不能代表全体文献。")
if analysis["identifier_conflicts"]:
    st.warning(
        f"检测到 {analysis['identifier_conflicts']} 项强标识符冲突关系，"
        "冲突记录已保留为独立样本，请人工核对后再用于统计。"
    )
st.caption("统计口径：仅使用本次样本，不代表 Sciverse 全库；缺失字段不会被猜测填充。")

with st.expander("查看样本与缺失率", expanded=False):
    st.json(scope)
    st.dataframe(
        [
            {
                "字段": row["label"],
                "缺失数": row["missing"],
                "总数": row["total"],
                "缺失率": f"{row['missing_rate'] * 100:.1f}%",
            }
            for row in analysis["missing"]
        ],
        hide_index=True,
        use_container_width=True,
    )

chart_tabs = st.tabs(["年份", "期刊", "作者", "学科", "关键词", "高被引", "增长关键词"])
with chart_tabs[0]:
    if analysis["year_counts"]:
        st.bar_chart(pd.DataFrame(analysis["year_counts"]).set_index("year"))
    else:
        st.info("样本中没有可用年份。")
with chart_tabs[1]:
    if analysis["venue_counts"]:
        st.bar_chart(pd.DataFrame(analysis["venue_counts"][:15]).set_index("venue"))
    else:
        st.info("样本中没有期刊信息。")
with chart_tabs[2]:
    if analysis["author_counts"]:
        st.bar_chart(pd.DataFrame(analysis["author_counts"][:15]).set_index("author"))
    else:
        st.info("样本中没有作者信息。")
with chart_tabs[3]:
    if analysis["subject_counts"]:
        st.bar_chart(pd.DataFrame(analysis["subject_counts"][:15]).set_index("subject"))
    else:
        st.info("样本中没有学科信息。")
with chart_tabs[4]:
    if analysis["keyword_counts"]:
        st.bar_chart(pd.DataFrame(analysis["keyword_counts"][:15]).set_index("keyword"))
    else:
        st.info("样本中没有关键词信息。")
with chart_tabs[5]:
    st.dataframe(analysis["top_cited"], hide_index=True, use_container_width=True)
with chart_tabs[6]:
    if analysis["growing_keywords"]:
        st.dataframe(analysis["growing_keywords"], hide_index=True, use_container_width=True)
    else:
        st.info("样本不足以计算关键词增长。")

st.markdown("### 导出")
export_cols = st.columns(2)
with export_cols[0]:
    st.download_button(
        "下载统计 ZIP（CSV）",
        data=export_trends_zip(analysis, scope),
        file_name="sciverse-trends.zip",
        mime="application/zip",
        key="download_trends_zip",
    )
with export_cols[1]:
    try:
        png = export_trends_png(analysis)
        st.download_button(
            "下载图表 PNG",
            data=png,
            file_name="sciverse-trends.png",
            mime="image/png",
            key="download_trends_png",
        )
    except ImportError:
        st.caption("未安装 matplotlib，暂不能导出 PNG；统计 ZIP 不受影响。")
