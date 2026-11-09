# Contributing

## 这个仓库的定位

本仓库是 [`opendatalab/Sciverse-Agent-Tools`](https://github.com/opendatalab/Sciverse-Agent-Tools)
的 **fork**，在它之上继续开发科研工作台与 1.27+ 的工具链：

| remote | 地址 | 说明 |
|---|---|---|
| `origin` | `https://github.com/qianluxi/Sciverse-Agent-Tools.git` | 本仓库，日常开发提交到这里 |
| `upstream` | `https://github.com/opendatalab/Sciverse-Agent-Tools.git` | 上游，只读参照；不要把 PR 提到这里 |

所以：

- **Issue / PR 请提到 `origin`**，不要提到上游仓库。
- 同步上游用 `git fetch upstream && git rebase upstream/main`，然后 push 到 `origin`。

## 包发布状态（重要）

当前 fork 没有 PyPI `sciverse`、npm `sciverse` / `sciverse-mcp-server`、ClawHub skill 的
所有者或 Maintainer 权限，上传会返回 403。因此 **合入 `main` 不会自动发布任何包**：

- `release.yml` 只做：算版本号 → 同步版本文件 → 重新派生 → 打 tag → 创建 GitHub Release
  → 上传 `web-skill.tgz`。它**没有** `publishCmd`。
- `republish.yml` 与 `scripts/publish.sh` 保留，供以后拿到包权限时手动补发。
- PyPI / npm 上现有的 `sciverse` 是 **1.0.1 上游旧线**，结构化检索叫 `search_metadata`；
  本仓库是 `search_papers`。写文档时不要引导用户 `pip install sciverse`，
  安装方式统一写在 [`docs/INSTALL.md`](./docs/INSTALL.md)。
- 便携 exe 不入库、不挂 Release，由 `scripts/build_desktop.ps1` 或
  **Build portable exe** 工作流构建。

## 报告问题

- Bug（请附版本：`python -c "import sciverse; print(sciverse.__version__)"`，或
  `git rev-parse --short HEAD`）
- 功能建议
- 工具描述 / `SKILL.md` 文案改进
- 文档错误与歧义

## 提交 PR

1. 从 `main` 拉分支，改动尽量限定在所属包内（`packages/*`、`research_app/`、`generators/`）。
2. commit message 遵循 [Conventional Commits](https://www.conventionalcommits.org/)，
   版本号由 `semantic-release` 从 commit 类型推导。
3. 开 PR。`test.yml` 会跑：生成器测试、Python SDK、TypeScript SDK、MCP server、
   科研工作台全量测试、spectral lint、以及**派生产物漂移检查**。
4. 维护者 review 后合入 `main`。

### 必须遵守的两条

- **不要手改派生产物**：`dist/*.json`、`packages/*/src/{types,tools}.{py,ts}`、
  `clawhub/SKILL.md`、`clawhub/manifest.json`、`skill-claude-code/SKILL.md`、
  `.claude-plugin/marketplace.json` 全部由 `openapi.yaml` 派生。改接口请改 `openapi.yaml`，
  然后跑 `bash scripts/build.sh` 并提交生成结果。
- **不要把 token、`.venv`、本地缓存、`~/.sciverse` 下的研究数据提交进来**。

## 本地验证

| 范围 | 命令 |
|---|---|
| 根生成器 | `uv run pytest tests/ -v` |
| Python SDK | `cd packages/python && uv run pytest tests/ -v --ignore tests/test_contract.py` |
| TypeScript SDK | `cd packages/typescript && npm run typecheck && npx vitest run --exclude 'tests/contract.test.ts'` |
| MCP server | `cd packages/mcp && npm run build && npm test` |
| OpenAPI | `npx --yes @stoplight/spectral-cli@6 lint openapi.yaml` |
| 派生产物 | `bash scripts/build.sh` 后确认 `git status` 无意外 diff |
| 科研工作台 | `python -m pip install -e packages/python && python -m pytest research_app/tests/ -v` |

Layer-2 契约测试需要测试 token 和测试端点，日常 CI 与离线测试一律用 mock，不得依赖生产凭据。

## 版本与 Release 流程

版本号由 `semantic-release` 从 commit message 推导：

| 类型 | 效果 |
|---|---|
| `feat: ...` | minor |
| `fix: ...` / `perf: ...` / `refactor: ...` / `docs: ...` | patch |
| `chore: ...` / `style: ...` / `test: ...` | 不出 release |
| `BREAKING CHANGE:` footer 或 `!` 后缀 | major |

push 到 `main` 后，`release.yml` 会：

1. 先跑测试门禁（生成器 + 工作台），失败则不打 tag
2. 分析上次 tag 以来的 commit，算 next version
3. 同步 `openapi.yaml` / Python / TypeScript / MCP / ClawHub manifest 的版本号
4. 重新生成派生产物
5. 打 `v${version}` tag，创建 GitHub Release，上传 `web-skill.tgz`
6. 用 `[skip ci]` 把版本改动提交回 `main`

**不会**发布到 PyPI / npm / ClawHub（原因见上文“包发布状态”）。

## License

[Apache-2.0](./LICENSE)
