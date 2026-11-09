from __future__ import annotations

import streamlit as st

from research_app.storage import list_screening_projects


def project_selector(key: str, label: str = "当前研究项目") -> str | None:
    """渲染项目选择器，并把选中结果同步回 ``current_project_id``。

    selectbox 的 widget 状态独立于 ``current_project_id`` 存储：一旦某个 ``key``
    渲染过一次，后续 rerun 都会优先复用它记录的值，``index`` 参数不再生效。
    这里用 ``key::seen`` 记住上一次渲染出来的值，用来区分两种变化：

    - widget 值变了、当前项目没变 → 用户在界面上换了项目，以 widget 为准；
    - widget 值没变、当前项目变了 → 外部代码（如新建/删除项目）改了状态，
      必须把新值写回 widget，否则旧值会覆盖新选择，紧接着又被下面的 ``pop``
      删掉，表现为“新建项目后选择器仍为空、页面仍提示请选择项目”。
    """

    projects = list_screening_projects()
    options = [""] + [project["id"] for project in projects]
    current = st.session_state.get("current_project_id") or ""
    if current not in options:
        current = ""

    seen_key = f"{key}::seen"
    widget_value = st.session_state.get(key)
    if widget_value in options and widget_value != st.session_state.get(seen_key):
        # 用户刚在界面上改了选择，不要用旧的项目状态覆盖它。
        current = widget_value
    else:
        # 首次渲染，或外部改写了 current_project_id：把状态推进 widget。
        st.session_state[key] = current

    # 注意：这里不能再传 index —— widget 状态已经等于 current 时同时给默认值
    # 会触发 "created with a default value but also had its value set via the
    # Session State API" 警告。
    selected = st.selectbox(
        label,
        options,
        format_func=lambda value: (
            "不关联研究项目"
            if not value
            else next(project["name"] for project in projects if project["id"] == value)
        ),
        key=key,
    )
    st.session_state[seen_key] = selected
    if selected:
        st.session_state["current_project_id"] = selected
        return selected
    st.session_state.pop("current_project_id", None)
    return None
