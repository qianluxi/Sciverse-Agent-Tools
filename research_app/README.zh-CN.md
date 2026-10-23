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
2. 条件筛选默认只返回论文类文献（article/review/conference/preprint），会排除书籍章节、社论、勘误等；需要其他类型时选择“全部类型”。语义检索不能可靠按类型过滤，但会自动按 `doc_id` 批量补全 `unique_id`；未匹配的结果会明确提示，仍可阅读但暂不能用于引用网络。
3. 结构化检索支持分页和年份排序；可从左侧加载字段目录，添加一个高级字段条件。
4. 勾选要整理的文献，可跨页选择后导出 CSV、RIS、BibTeX 或 Markdown。RIS/BibTeX 可导入 Zotero。
5. 点击“查看文献”后可读取有权限的原文，并按页浏览引用、参考文献和相关工作。
6. 可保存常用检索；最近检索和已保存检索都显示在左侧栏。
7. 左侧页面导航进入“系统综述筛选”：创建综述项目和纳入/排除标准，把主页当前检索结果加入候选队列，逐条标记纳入/排除/待定，并查看重复依据、审计记录和 PRISMA 统计。
8. 左侧“引用网络”：选择中心论文，加载引用/被引/相关工作的一层关系，按需继续展开节点；可补全年份、期刊等元数据，按条件过滤，并导出 GraphML 或边列表 CSV。
9. 左侧“趋势分析”：统计当前检索结果的年份、期刊、作者、学科、关键词、高被引和增长关键词；条件筛选可扩展到最多 5 页，页面显示样本量、请求次数、缺失率和截断风险，并导出统计 ZIP 或 PNG。

## 运行离线测试

从仓库根目录运行，测试使用本地 SDK 和 mock，不需要 Sciverse Token：

```bash
python -m pip install -r research_app/requirements.txt
python -m pip install -e packages/python pytest
python -m pytest research_app/tests/ -v
```

## Token 与隐私

网页中输入的 Token 仅用于当前会话，不写入本地数据库。检索条件、最近检索、综述项目、候选元数据、筛选决定、备注和审计记录保存在 `~/.sciverse/research-workbench.sqlite3`，最近检索最多保留 50 条；不会保存检索结果全文或附件。左侧“已保存检索的备份”只导出/恢复已保存的检索条件 JSON，不包含综述项目和审计记录。当前无自动备份，删除数据库文件会丢失本地记录。默认只在本机运行；不要将该应用公开到互联网或与他人共享 Token。

系统综述筛选中，候选按 DOI、`unique_id`、`doc_id` 或规范化标题去重；重复候选单独记录依据。PRISMA 数量由候选和决定记录实时计算，导出的筛选 CSV 包含状态、排除原因、备注和更新时间。

引用网络的关系接口只返回标题和标识；年份、期刊、引用量需要点击“补全节点元数据”额外发起检索，页面会显示请求次数。未补全的节点标记为 `relation`，不会显示为已知元数据。关系接口只接受 Sciverse 的 `paper:...` unique_id；Semantic Scholar 数字 ID 或仅 DOI 的节点不能继续展开，页面会将其排除在“从哪个节点展开”之外。

若已配置 `SCIVERSE_API_TOKEN` 环境变量，应用也可以直接使用该凭据，无需再次输入。

## 后续规划

开发路线和功能边界见 [ROADMAP.zh-CN.md](../ROADMAP.zh-CN.md)。

## 说明

- 论文全文的可读性取决于文献资源和 Sciverse 账号权限。
- “智能语义检索”支持平衡、更快和更深入三种检索方式。
- 请求超时为 30 秒；`400/401` 不自动重试，`429` 遵循 `Retry-After` 且自动等待最多 5 秒，`5xx` 最多重试 2 次。
- `429` 建议等待时间超过 5 秒或网络超时时，应用会提示用户稍后手动重试。
- 若出现纯文本 `Bad Request`，通常是 Token 粘贴时混入了控制字符。请在页面左侧重新粘贴 Token；在 CMD 中登录时用鼠标右键或 `Shift+Insert` 粘贴，不要按 `Ctrl+V`。
