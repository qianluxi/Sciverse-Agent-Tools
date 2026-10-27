from __future__ import annotations

import argparse
import json

from research_app.storage import (
    database_path,
    inspect_identifier_integrity,
    inspect_project_statistics,
    rebuild_project_statistics,
    repair_identifier_integrity,
    upgrade_database,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sciverse 科研工作台数据库维护工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "inspect",
        help="只检查错误别名、歧义映射和孤立映射，不修改数据库",
    )
    repair = subparsers.add_parser(
        "repair",
        help="备份数据库后删除可确定的错误别名和孤立映射",
    )
    repair.add_argument(
        "--no-backup",
        action="store_true",
        help="不生成 SQLite 备份（不建议）",
    )
    subparsers.add_parser(
        "upgrade",
        help="备份数据库后升级到当前 Schema",
    )
    subparsers.add_parser(
        "stats",
        help="检查项目统计是否可由检索快照、来源和去重记录重建",
    )
    rebuild = subparsers.add_parser(
        "rebuild-stats",
        help="按当前来源和去重记录重建一个项目的统计（可选写入）",
    )
    rebuild.add_argument("--project-id", required=True)
    rebuild.add_argument(
        "--apply",
        action="store_true",
        help="写入重建结果；不加时只显示建议值",
    )
    args = parser.parse_args()

    print(f"database: {database_path()}", flush=True)
    if args.command == "inspect":
        result = inspect_identifier_integrity()
    elif args.command == "upgrade":
        result = upgrade_database()
    elif args.command == "stats":
        result = inspect_project_statistics()
    elif args.command == "rebuild-stats":
        result = rebuild_project_statistics(args.project_id, apply=args.apply)
    else:
        result = repair_identifier_integrity(backup=not args.no_backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
