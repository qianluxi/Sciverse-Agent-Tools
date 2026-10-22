# Repository Development Instructions

These instructions apply to the whole repository. The research-workbench roadmap only governs work under `research_app/`; it does not constrain independent SDK, MCP, generator, packaging, or release tasks.

## Scope and Ownership

- For any task under `research_app/`, also read and follow [`research_app/AGENTS.md`](./research_app/AGENTS.md) and the root [`ROADMAP.zh-CN.md`](./ROADMAP.zh-CN.md).
- Keep module-specific changes within their owning package unless an interface change requires coordinated updates.
- Treat `openapi.yaml` as the canonical API contract. Update generated SDK schemas, tools, Skills, and examples through the documented generation workflow; do not hand-edit derived artifacts.
- Never include API tokens, credentials, local caches, or user research data in commits or logs.

## Validation

Use the repository CI workflow as the authoritative cross-package validation. Relevant local checks include:

| Area | Validation |
|---|---|
| Root generators | `uv run pytest tests/ -v` |
| Python SDK | From `packages/python`: `uv run pytest tests/ -v --ignore tests/test_contract.py` |
| TypeScript SDK | From `packages/typescript`: `npm run typecheck` and `npx vitest run --exclude 'tests/contract.test.ts'` |
| MCP server | From `packages/mcp`: `npm run build` and `npm test` |
| OpenAPI | `npx --yes @stoplight/spectral-cli@6 lint openapi.yaml` |
| Derived artifacts | `bash scripts/build.sh`, then verify the generated diff |
| Research app | Install `research_app/requirements.txt` and the local SDK, then run `pytest research_app/tests/ -v` |

Layer-2 contract tests require explicit test credentials and a test endpoint. Routine CI and offline tests must use mocks and must not depend on production credentials.

## Independent Security Work

Security hardening in `packages/mcp` (including HTTP request-body limits and timeouts) is a separate workstream. Do not mix it into research-workbench roadmap phases; scope and validate it independently when requested.

## Completion Standards

- Add or update tests for behavioral changes; report unavailable checks and their blockers.
- Update the owning package's documentation when usage changes.
- Keep changes focused and preserve unrelated worktree changes.
- Do not commit or push unless explicitly requested.
