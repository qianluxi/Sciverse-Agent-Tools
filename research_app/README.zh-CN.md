# 科研文献助手

一个基于 Sciverse Agent Tools 的本地网页应用。无需编程即可检索学术文献、保存检索条件、浏览引用关系、阅读可访问的原文，并导出到 Zotero 或表格软件。

## 适合谁

- 希望通过网页界面查找研究论文的科研人员和学生
- 不想自己编写代码或直接使用命令行的 Sciverse 用户

## 首次启动

### Windows（推荐）

1. 安装 Python 3.10 或更新版本，并在安装时勾选“Add Python to PATH”。
2. 双击 `research_app/Start-Research-App.bat`。首次启动会自动安装所需组件，可能需要几分钟。
3. 浏览器打开应用后，在左侧输入从 [Sciverse 控制台](https://sciverse.space) 申请的 API Token。

如果双击启动器不成功，也可以在项目目录打开 PowerShell，运行：

```powershell
python -m pip install -r research_app/requirements.txt
python -m streamlit run research_app/app.py
```

浏览器地址通常是 <http://localhost:8501>。

### macOS / Linux

```bash
python3 -m pip install -r research_app/requirements.txt
python3 -m streamlit run research_app/app.py
```

随后在浏览器打开 <http://localhost:8501> 并输入 API Token。

## 使用方法

1. 选择“智能语义检索”，用一句话描述问题；或选择“条件筛选”，填写关键词、作者、年份、期刊/会议和学科。
2. 条件筛选默认只返回论文类文献（article/review/conference/preprint），会排除书籍章节、社论、勘误等；需要其他类型时选择“全部类型”。勾选“仅开放获取（OA）”只是按 OA 状态过滤，不代表平台有全文记录，某些主题下反而会减少 `doc_id` 覆盖。要只看当前页有全文记录的论文，请勾选“仅显示有全文记录（doc_id）”。语义检索不能可靠按类型过滤，但会自动按 `doc_id` 批量补全 `unique_id`；未匹配的结果会明确提示，仍可阅读但暂不能用于引用网络。
3. 结构化检索支持分页和年份排序；可从左侧加载字段目录，添加一个高级字段条件。
4. 勾选要整理的文献，可跨页选择后导出 CSV、RIS、BibTeX 或 Markdown。RIS/BibTeX 可导入 Zotero。
5. 点击“查看文献”后可读取有权限的原文，并按页浏览引用、参考文献和相关工作。
6. 可保存常用检索；最近检索和已保存检索都显示在左侧栏。
7. 左侧页面导航进入“系统综述筛选”：创建综述项目和纳入/排除标准，把主页当前检索结果加入候选队列，逐条标记纳入/排除/待定，并查看重复依据、审计记录和 PRISMA 统计。
8. 左侧“引用网络”：选择中心论文，加载引用/被引/相关工作的一层关系，按需继续展开节点；可补全年份、期刊等元数据，按条件过滤，并导出 GraphML 或边列表 CSV。
9. 左侧“趋势分析”：统计当前检索结果的年份、期刊、作者、学科、关键词、高被引和增长关键词；条件筛选可扩展到最多 5 页，页面显示样本量、请求次数、缺失率和截断风险，并导出统计 ZIP 或 PNG。主页发起新检索会清除旧的扩展样本，避免混用不同检索主题。
10. 左侧“图表证据板”：从最多 5 篇有全文记录的论文中提取图表（每篇最多 4 张），为每张图添加备注，并导出证据清单 CSV 或图片 ZIP。是否能读以实际读取结果为准；`is_content_accessible=false` 的论文也可以尝试，单个失败不会阻塞其他结果。
11. 左侧“AI 证据与提纲”：从最近使用的 Base URL 中选择或填写自定义 OpenAI 兼容服务，配置 API Key 和模型名；从最多 5 篇论文中提取研究问题、方法、样本、发现、局限和引文，自动比对引句，逐篇人工核对/备注后导出证据表 CSV/Markdown，再生成带来源标注的 Markdown 综述提纲。API Key 只在当前会话使用。

## 运行离线测试

从仓库根目录运行，测试使用本地 SDK 和 mock，不需要 Sciverse Token：

```bash
python -m pip install -r research_app/requirements.txt
python -m pip install -e packages/python pytest
python -m pytest research_app/tests/ -v
```

## Token 与隐私

网页中输入的 Token 仅用于当前会话，不写入本地数据库。检索条件、最近检索、综述项目、候选元数据、筛选决定、备注和审计记录保存在 `~/.sciverse/research-workbench.sqlite3`，最近检索最多保留 50 条；不会保存检索结果全文或附件。左侧“已保存检索的备份”只导出/恢复已保存的检索条件 JSON，不包含综述项目和审计记录。当前无自动备份，删除数据库文件会丢失本地记录。默认只在本机运行；不要将该应用公开到互联网或与他人共享 Token。

系统综述筛选中，候选会综合所有可用标识符（DOI、`unique_id`、`doc_id` 和 Unicode 规范化标题）进行跨字段匹配；例如一条记录有 DOI 和 `unique_id`，另一条只有相同 `unique_id`，也会识别为重复。若弱标识记录先入库、之后才出现带 DOI/稳定 ID 的同一论文，主候选会补全缺失元数据，但不会覆盖已有筛选决定、备注和审计历史。一个记录的多个强标识符指向不同候选时，会生成一个 `conflict:` 候选并在“重复记录”页标记为身份冲突，需人工核对。PRISMA 数量由候选和决定记录实时计算，导出的筛选 CSV 包含状态、排除原因、备注和更新时间。

引用网络的关系接口只返回标题和标识；年份、期刊、引用量需要点击“补全节点元数据”额外发起检索，页面会显示请求次数。未补全的节点标记为 `relation`，不会显示为已知元数据。关系接口只接受 Sciverse 的 `paper:...` unique_id；Semantic Scholar 数字 ID 或仅 DOI 的节点不能继续展开，页面会将其排除在“从哪个节点展开”之外。参考文献和被引关系保留方向；相关工作按无向虚线展示，并在 GraphML 中写入 `directed="false"`。

若已配置 `SCIVERSE_API_TOKEN` 环境变量，应用也可以直接使用该凭据，无需再次输入。

## AI 证据与提纲

“AI 证据与提纲”页面需要用户自备 OpenAI 兼容服务。默认 Base URL 为 OpenCode Go（`https://opencode.ai/zen/go/v1`）；填写 API Key 和模型名后，可对最多 5 篇论文提取结构化证据并生成 Markdown 提纲。也可选择“自定义 URL”连接其他服务。

- Base URL 和模型名会记录在本机 `~/.sciverse/ai-settings.json`，下次从下拉列表选择；选择历史 URL 会自动带出其上次使用的模型。
- API Key 只在当前会话使用，不写入本地数据库、配置文件或源码。
- 每条证据保留论文 ID、模型、提示词版本和生成时间；引句会自动与输入文本比对，未匹配项会明确提示。
- 证据字段、引句和核对备注可直接编辑；逐篇标记“已核对”后，可导出证据表 CSV 和 Markdown。CSV 每篇一行，最多 3 条引句分列，便于在表格软件中筛选。
- 编辑和核对只保留在当前浏览器会话，不覆盖原始 AI 输出，也不会自动保存为最终结论；提纲导出附带模型和提示词版本。
- 提纲直接生成 Markdown；若模型仍返回 JSON 包装或输出被截断，应用会自动提取其中的 Markdown 正文，避免下载到原始 JSON。
- AI 失败按论文隔离，不阻塞其他论文；响应因输出长度被截断时，客户端会在有界预算内重试更大输出。不会自动写入 Zotero 或生成最终论文。

OpenCode Go 推荐模型为 `glm-5.3-flash` 或其他走 `/chat/completions` 的非推理模型 ID（不要加 `opencode-go/` 前缀）。客户端会自动发送稳定的 `x-opencode-session` 会话头和专用 User-Agent。若反复出现 `finish_reason=length` 和空内容，说明推理过程耗尽了输出预算，应换用非推理模型。

## Windows 便携版 exe

需要构建便携版时，在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_desktop.ps1
```

构建结果位于 `dist/SciverseResearchWorkbench.exe`。双击后会启动本地服务并自动打开浏览器；不需要命令行。首次构建需要安装 PyInstaller，耗时和体积取决于当前 Python 环境。当前产物为 109,152,078 字节（约 104 MB）；本机最新构建首次启动实测约 6.7 秒，干净 Windows 机器可能更慢。onefile 首次启动需要解压，请等待浏览器自动打开。

AI 页面的 API Key 只在当前会话输入，不写入 exe 或本地数据库；Base URL 和模型名保存在本机 `~/.sciverse/ai-settings.json`。Sciverse Token 仍按原有方式从环境变量或凭据文件读取。

### 故障排查

- 浏览器没有自动打开：手动访问 `http://localhost:8501`；如果端口被占用，应用会依次尝试 `8501-8600`。
- 双击后长时间没有反应：先等待 30 秒；确认杀毒软件没有拦截 PyInstaller 单文件程序，可尝试把 exe 放到普通本地目录后重新运行。
- 提示缺少文件或启动即退出：重新运行 `scripts/build_desktop.ps1` 生成 exe；不要只复制 `dist` 中的临时文件。
- 页面能打开但检索失败：检查 Sciverse Token；AI 功能还需单独配置 API Key。
- 端口被其他程序占用：关闭其他 Streamlit/本地服务，或重启应用让它选择新端口。
- 数据异常：本地记录位于 `~/.sciverse/`（Windows 为 `C:\Users\<用户名>\.sciverse`）；删除前请先备份 `research-workbench.sqlite3`。

### 卸载

1. 关闭黑色命令行窗口或结束 `SciverseResearchWorkbench.exe` 进程。
2. 删除 `dist/SciverseResearchWorkbench.exe`。
3. 如需清除本地检索、筛选和 AI 服务地址记录，再删除 `~/.sciverse/`；这会丢失工作台本地数据，不会影响 Zotero。

### 干净 Windows 验收清单

1. 准备一台未安装 Python 的 Windows 10/11 机器，复制 exe 到本地目录。
2. 双击 exe，记录从双击到浏览器打开的时间。
3. 确认首页可打开，执行一次检索并导出 CSV。
4. 打开“AI 证据与提纲”，确认页面可编辑证据和导出。
5. 关闭应用，确认没有残留 `SciverseResearchWorkbench.exe` 进程。

## 后续规划

开发路线和功能边界见 [ROADMAP.zh-CN.md](../ROADMAP.zh-CN.md)。

## 说明

- 论文全文的可读性取决于文献资源和 Sciverse 账号权限。
- “智能语义检索”支持平衡、更快和更深入三种检索方式。
- 请求超时为 30 秒；`400/401` 不自动重试，`429` 遵循 `Retry-After` 且自动等待最多 5 秒，`5xx` 最多重试 2 次。
- `429` 建议等待时间超过 5 秒或网络超时时，应用会提示用户稍后手动重试。
- 若出现纯文本 `Bad Request`，通常是 Token 粘贴时混入了控制字符。请在页面左侧重新粘贴 Token；在 CMD 中登录时用鼠标右键或 `Shift+Insert` 粘贴，不要按 `Ctrl+V`。
