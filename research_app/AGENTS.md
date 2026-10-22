# Research Workbench Instructions

These instructions apply only to code and documentation under `research_app/`. Read the root [`ROADMAP.zh-CN.md`](../ROADMAP.zh-CN.md) before making changes.

## Product Boundary

- The workbench is Sciverse's global discovery, screening, and evidence-organization layer.
- Do not rebuild Zotero's library management, PDF annotation, citation formatting, synchronization, or group-library features.
- Do not duplicate llm-for-zotero's generic library chat, paper summaries, note writing, or review drafting. Integrate with Zotero through interoperable exports instead.
- Keep AI features optional, evidence-grounded, and subject to human review.
- SQLite stores only workbench state (such as saved searches and screening decisions), not a duplicate Zotero library. Define migrations, retention, cache limits, and backup behavior before persistent storage is introduced.

## Roadmap Execution

- The current active stage and its acceptance evidence are recorded in `ROADMAP.zh-CN.md`.
- Before starting a task, identify its stage, scope, dependencies, non-goals, and testable acceptance criteria.
- If the task conflicts with the current stage or ordering, ask only when the request is materially ambiguous. If the user explicitly requests an early feature, explain the impact and update the roadmap before implementing it.
- Do not mark a stage complete from code completion alone. Record automated test results, manual acceptance evidence where needed, and unresolved items.
- Ordinary bug fixes do not change stage status unless they alter stage scope, dependencies, or acceptance.

## Implementation Guidance

- Keep Streamlit as the current UI; do not migrate frameworks without demonstrated limits.
- Keep API calls, response normalization, exports, storage, and UI concerns independently testable, but prefer small modules over speculative architecture.
- `openapi.yaml` is authoritative. Structured `search_papers` responses use `results`, `total_count`, `page`, and `page_size`; semantic-search chunks use `hits`.
- Do not store `AgentToolsClient` in Streamlit session state across `asyncio.run()` event loops. Make its lifecycle explicit if the API layer changes.
- Use mock API responses in offline tests. Never require a production token in CI.

## Validation

- Run `pytest research_app/tests/ -v` for the workbench smoke and unit tests.
- Run `python -m py_compile research_app/app.py` for a quick syntax check.
- Run the root generator and relevant SDK tests when changing their contracts or shared interfaces.
- Test 400, 401, 429, 5xx, timeout, empty-result, and permission-denied behavior with mocks as the service layer grows.
- Do not modify generated SDK or Skill artifacts by hand; coordinate interface changes through the root OpenAPI workflow.
