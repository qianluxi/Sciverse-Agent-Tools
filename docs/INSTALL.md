# 安装与版本对照

本文是**唯一的安装真源**。其他文档里的安装命令如果和这里冲突，以本文为准。

## 0. 先搞清楚装的是哪条版本线

仓库里有三条不同的交付线，接口并不通用：

| 来源 | 版本 | 结构化检索工具名 | 现状 |
|---|---|---|---|
| **本仓库源码**（`packages/python`、`packages/mcp`） | 1.27.0 | `search_papers` | 唯一与 `openapi.yaml` 一致的实现，本文推荐 |
| PyPI `sciverse` / npm `sciverse-mcp-server` | 1.0.1 | `search_metadata` | 上游旧线，缺少 1.27 的字段、翻页与筛选能力 |
| 本仓库 GitHub Release | tag + `web-skill.tgz` | 随 skill bundle | **不含** SDK 包，也**不含**便携 exe |

由此得到两条硬规则：

- **不要 `pip install sciverse` / `npm install sciverse-mcp-server`。** 拉下来的是上游 1.0.1，工具名是 `search_metadata`，参数签名也不同，照本仓库文档写 `search_papers(...)` 会直接报错。
- 需要包管理器分发时，从本仓库源码安装（见下文 A / B），或使用 GitHub Release 里的 `web-skill.tgz`。

## A. 科研工作台（图形界面，不需要写代码）

要求：Python 3.10+（推荐 3.12），Windows 10/11 或 macOS / Linux。

```bash
git clone https://github.com/qianluxi/Sciverse-Agent-Tools.git
cd Sciverse-Agent-Tools

# 1) 先装本仓库的 SDK —— 顺序不能反，requirements 里不再从 PyPI 拉 sciverse
python -m pip install -e packages/python

# 2) 再装工作台依赖
python -m pip install -r research_app/requirements.txt

# 3) 启动
python -m streamlit run research_app/app.py
```

浏览器打开 <http://localhost:8501>，在左侧填入 [Sciverse 控制台](https://sciverse.space) 申请的 API Token。

Windows 用户也可以直接双击 `research_app/Start-Research-App.bat`，它会检查依赖并安装上面两步所需内容，然后启动应用。

验证 SDK 装对了：

```bash
python -c "import sciverse, pathlib; print(sciverse.__version__, pathlib.Path(sciverse.__file__).parent)"
# 期望：1.27.0 .../Sciverse-Agent-Tools/packages/python/src/sciverse
```

如果版本低于 `1.27.0`（例如 `1.0.1`），说明装的是 PyPI 旧包，先 `python -m pip uninstall -y sciverse` 再执行 A-1。

### 便携 exe

仓库**不随 Release 发布 exe 二进制**，需要自己在 Windows 上构建：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_desktop.ps1
```

产物为 `dist/SciverseResearchWorkbench.exe`（单文件，约 106 MB，首次启动需解压，本机实测约 7.6 秒）。构建前请先完成 A-1、A-2，`packages/python` 与 `research_app` 依赖都要在同一个 Python 环境里。

也可以在 GitHub Actions 里手动运行 **Build portable exe** 工作流（`workflow_dispatch`），下载 `SciverseResearchWorkbench-windows` artifact。该工作流只产出 artifact，不会自动挂 Release——便携包需要先在无 Python 的干净机器上验收。

## B. SDK（自己写 agent）

### Python

```bash
python -m pip install -e packages/python
```

```python
async with AgentToolsClient() as c:
    r = await c.search_papers(query="CRISPR", year_from=2020, page_size=10)
```

### TypeScript

```bash
cd packages/typescript
npm ci
npm run build
npm pack            # 产出 sciverse-1.27.0.tgz，可在别的项目 npm install <路径>
```

### MCP server

```bash
cd packages/mcp
npm ci
npm run build
```

在 agent 的 MCP 配置里指向本地构建产物，不要用 `npx -y sciverse-mcp-server`（那是 1.0.1 线）：

```json
{
  "mcpServers": {
    "sciverse": {
      "command": "node",
      "args": ["/绝对路径/Sciverse-Agent-Tools/packages/mcp/dist/cli.js"],
      "env": { "SCIVERSE_API_TOKEN": "${SCIVERSE_API_TOKEN}" }
    }
  }
}
```

## C. Skill / 插件渠道的实际情况

| 渠道 | 命令 | 装到的是什么 |
|---|---|---|
| well-known 自动发现 | 指向 `https://sciverse.space/.well-known/agent-skills/` | 线上 1.0.1 线的 skill bundle |
| Skills CLI | `npx skills add https://sciverse.space` | 同上 |
| Claude Code Marketplace | `claude /plugin marketplace add https://github.com/opendatalab/Sciverse-Agent-Tools` | **上游仓库**，不是本 fork |
| ClawHub | `clawhub install sciverse` | 上游已发布版本 |
| GitHub Release | 下载 `web-skill.tgz` | 本仓库当前版本（`tar xzf` 后取 `.well-known`） |

本 fork 尚未发布自己的 marketplace / ClawHub 包，上表后两行的内容与本文 1.27.0 的行为可能有差异，遇到工具名不一致时以本文第 0 节为准。

## D. 依赖与构建

| 用途 | 依赖 |
|---|---|
| 工作台运行 | `streamlit>=1.40`、`matplotlib>=3.8`，SDK 由 `packages/python` 提供 |
| 根目录生成器 | `pyyaml`、`jsonschema`、`datamodel-code-generator`（见根 `pyproject.toml`） |
| 重新派生 `dist/` 与各包 `types`/`tools` | `uv sync && bash scripts/build.sh`（Windows 用 Git Bash / WSL） |

`research_app/requirements.txt` 只列第三方依赖，**不包含 `sciverse`**：SDK 必须来自本仓库源码，避免装到 PyPI 旧线。

## E. 常见安装失败

| 现象 | 原因 / 处理 |
|---|---|
| `TypeError` / `ValueError: Unknown search_papers parameters` | 装到了 PyPI 1.0.1，按 A-1 重装本仓库 SDK |
| `ModuleNotFoundError: sciverse.credentials` | 同上，旧包没有该模块 |
| `pip install -e packages/python` 报缺 `hatchling` | 网络受限，先配置镜像或 `pip install hatchling` |
| 工作台启动后检索参数报错或提示未知参数 | 装到了旧线 SDK，按 A-1 重装，并确认 `python -c "import sciverse; print(sciverse.__version__)"` 为 1.27.0 |
| 双击 `Start-Research-App.bat` 装完仍报错 | 检查是否有多个 Python；用 `python -m pip -V` 确认装进的是启动时用的那个环境 |
