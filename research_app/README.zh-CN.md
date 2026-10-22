# 科研文献助手

一个基于 Sciverse Agent Tools 的本地网页应用。无需编程即可用自然语言检索学术文献、按作者和年份筛选、查看文献摘要与可访问的论文原文，并将结果下载为 CSV。

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

1. 选择“智能语义检索”，用一句话描述研究问题；或者选择“条件筛选”，填写关键词、作者和年份。
2. 点击检索按钮，在结果中浏览标题、作者、年份、期刊、摘要和相关度。
3. 点击“查看文献”，再点击“读取论文原文”查看账号有权访问的内容；按需继续阅读下一段。
4. 点击“下载文献清单（CSV）”，可用 Excel 或其他表格软件整理结果。

## 运行离线测试

从仓库根目录运行，测试使用本地 SDK 和 mock，不需要 Sciverse Token：

```bash
python -m pip install -r research_app/requirements.txt
python -m pip install -e packages/python pytest
python -m pytest research_app/tests/ -v
```

## Token 与隐私

Token 在网页侧边栏中输入，仅用于当前应用会话，不会由本应用写入文件。默认只在本机运行；不要将该应用公开到互联网或与他人共享 Token。

若已配置 `SCIVERSE_API_TOKEN` 环境变量，应用也可以直接使用该凭据，无需再次输入。

## 后续规划

开发路线和功能边界见 [ROADMAP.zh-CN.md](../ROADMAP.zh-CN.md)。

## 说明

- 论文全文的可读性取决于文献资源和 Sciverse 账号权限。
- “智能语义检索”支持平衡、更快和更深入三种检索方式。
- 遇到 `401` 时请确认 Token 有效；遇到 `429` 时请稍后再试。
