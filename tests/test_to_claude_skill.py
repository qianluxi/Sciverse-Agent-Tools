"""单元测试：Claude Code 官方 Agent Skill 派生器。

与 test_to_clawhub_skill 平行，但断言点不同：
- frontmatter 严格只允许 `name` 和 `description`（不含 ClawHub 那些字段）
- description 须包含明确的 trigger 关键词
- marketplace.json 结构正确
"""
from pathlib import Path

from generators.to_claude_skill import (
    SKILL_NAME,
    generate_marketplace_json,
    generate_skill_md,
)

REAL_OPENAPI = Path(__file__).parent.parent / "openapi.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "minimal_openapi.yaml"


def _parse_frontmatter(md: str) -> dict:
    """提取 SKILL.md 顶部 YAML frontmatter。"""
    assert md.startswith("---\n"), "SKILL.md must start with frontmatter delimiter"
    end = md.index("\n---\n", 4)
    block = md[4:end]
    result = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def test_frontmatter_only_has_name_and_description():
    md = generate_skill_md(REAL_OPENAPI)
    fm = _parse_frontmatter(md)
    # Claude Code 官方 spec：只允许 name + description
    assert set(fm.keys()) == {"name", "description"}, (
        f"frontmatter must contain only name+description, got {list(fm.keys())}"
    )
    assert fm["name"] == SKILL_NAME


def test_frontmatter_excludes_clawhub_specific_fields():
    md = generate_skill_md(REAL_OPENAPI)
    # 这些字段是 ClawHub manifest 字段，不应出现在 Claude Code 官方 skill 的 frontmatter 里
    # （但有可能出现在正文 markdown 中，所以只检查 frontmatter 段）
    end = md.index("\n---\n", 4)
    frontmatter = md[:end + 5]
    for forbidden in ("version:", "license:", "homepage:", "runtime:", "manifest"):
        assert forbidden not in frontmatter, (
            f"frontmatter must not contain {forbidden!r}, got:\n{frontmatter}"
        )


def test_description_has_trigger_keywords():
    """description 必须包含让 Claude 能 trigger 的关键词。"""
    md = generate_skill_md(REAL_OPENAPI)
    fm = _parse_frontmatter(md)
    desc = fm["description"].lower()
    # 至少命中一个核心关键词
    assert "academic paper" in desc or "paper retrieval" in desc, (
        f"description must mention 'academic paper' or 'paper retrieval', got: {desc}"
    )


def test_skill_md_lists_all_three_tools():
    md = generate_skill_md(REAL_OPENAPI)
    for op_id in ("search_papers", "semantic_search", "read_content"):
        assert f"### {op_id}" in md, f"missing tool section {op_id}"


def test_skill_md_mentions_mcp_server_dep():
    md = generate_skill_md(REAL_OPENAPI)
    assert "sciverse-mcp-server" in md, (
        "SKILL.md should reference the MCP server package"
    )
    assert "SCIVERSE_API_TOKEN" in md


def test_skill_md_teaches_local_build_not_registry():
    """安装说明必须指向本地构建。

    npm registry 上的 `sciverse-mcp-server` 是上游 1.0.1 线（结构化检索叫
    `search_metadata`）。模板里一旦写回 `npm install -g` / `npx -y`，生成的
    SKILL.md 就会教用户装到旧版本——这正是对外交付的阻断项。
    """
    md = generate_skill_md(REAL_OPENAPI)
    assert "packages/mcp/dist/cli.js" in md, "安装说明应指向本地构建产物"
    assert "npm run build" in md, "安装说明应包含构建步骤"
    assert "npm install -g sciverse-mcp-server" not in md
    # 即使提到 npx，也只能出现在"不要用"的语境里
    for line in md.splitlines():
        if "npx" in line:
            assert "not" in line.lower(), f"npx 出现在非否定语境：{line!r}"


def test_skill_md_has_required_sections():
    md = generate_skill_md(REAL_OPENAPI)
    assert "## When to use" in md
    assert "## Prerequisites" in md
    assert "## Bootstrap: learn the schema first" in md
    assert "## Recipes" in md


def test_marketplace_json_structure():
    m = generate_marketplace_json(REAL_OPENAPI)
    assert m["name"] == SKILL_NAME
    assert "owner" in m and isinstance(m["owner"], dict)
    assert "plugins" in m and isinstance(m["plugins"], list)
    assert len(m["plugins"]) >= 1
    plugin = m["plugins"][0]
    for key in ("name", "description", "version", "source"):
        assert key in plugin, f"plugin entry missing key: {key}"
    assert plugin["source"].endswith("skill-claude-code")


def test_marketplace_version_pulled_from_openapi():
    m = generate_marketplace_json(REAL_OPENAPI)
    # openapi.yaml 当前是 0.1.2；只校验非空 + 与版本格式一致
    version = m["plugins"][0]["version"]
    assert version, "version must not be empty"
    assert version.count(".") >= 1, f"version should be semver-ish, got {version!r}"


def test_skill_md_works_with_minimal_fixture():
    """用最小 fixture 跑一遍，确保不依赖真实 openapi.yaml 字段。"""
    md = generate_skill_md(FIXTURE)
    fm = _parse_frontmatter(md)
    assert fm["name"] == SKILL_NAME
    # fixture 的 operation 是 do_foo
    assert "### do_foo" in md
