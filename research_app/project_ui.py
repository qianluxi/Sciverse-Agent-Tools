from __future__ import annotations

import streamlit as st

from research_app.storage import list_screening_projects


def project_selector(key: str, label: str = "当前研究项目") -> str | None:
    projects = list_screening_projects()
    options = [""] + [project["id"] for project in projects]
    current = st.session_state.get("current_project_id")
    index = options.index(current) if current in options else 0
    selected = st.selectbox(
        label,
        options,
        index=index,
        format_func=lambda value: (
            "不关联研究项目"
            if not value
            else next(project["name"] for project in projects if project["id"] == value)
        ),
        key=key,
    )
    if selected:
        st.session_state["current_project_id"] = selected
        return selected
    st.session_state.pop("current_project_id", None)
    return None
