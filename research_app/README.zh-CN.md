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
2. 结构化检索支持分页和年份排序；可从左侧加载字段目录，添加一个高级字段条件。
3. 勾选要整理的文献，可跨页选择后导出 CSV、RIS、BibTeX 或 Markdown。RIS/BibTeX 可导入 Zotero。
4. 点击“查看文献”后可读取有权限的原文，并按页浏览引用、参考文献和相关工作。
5. 可保存常用检索；最近检索和已保存检索都显示在左侧栏。

## 运行离线测试

从仓库根目录运行，测试使用本地 SDK 和 mock，不需要 Sciverse Token：

```bash
python -m pip install -r research_app/requirements.txt
python -m pip install -e packages/python pytest
python -m pytest research_app/tests/ -v
```

## Token 与隐私

网页中输入的 Token 仅用于当前会话，不写入本地数据库。保存的检索条件及最近检索记录保存在 `~/.sciverse/research-workbench.sqlite3`，最近检索最多保留 50 条；不会保存检索结果全文或附件。左侧“已保存检索的备份”可导出/恢复已保存的检索条件 JSON；最近检索不包含在备份中。当前无自动备份，删除数据库文件会丢失本地记录。默认只在本机运行；不要将该应用公开到互联网或与他人共享 Token。

若已配置 `SCIVERSE_API_TOKEN` 环境变量，应用也可以直接使用该凭据，无需再次输入。

## 后续规划

开发路线和功能边界见 [ROADMAP.zh-CN.md](../ROADMAP.zh-CN.md)。

## 说明

- 论文全文的可读性取决于文献资源和 Sciverse 账号权限。
- “智能语义检索”支持平衡、更快和更深入三种检索方式。
- 请求超时为 30 秒；`400/401` 不自动重试，`429` 遵循 `Retry-After` 且自动等待最多 5 秒，`5xx` 最多重试 2 次。
- `429` 建议等待时间超过 5 秒或网络超时时，应用会提示用户稍后手动重试。
- 若出现纯文本 `Bad Request`，通常是 Token 粘贴时混入了控制字符。请在页面左侧重新粘贴 Token；在 CMD 中登录时用鼠标右键或 `Shift+Insert` 粘贴，不要按 `Ctrl+V`。
