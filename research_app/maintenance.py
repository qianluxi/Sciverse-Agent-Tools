from __future__ import annotations

import argparse
import json

from research_app.storage import (
    database_path,
    inspect_identifier_integrity,
    repair_identifier_integrity,
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
    args = parser.parse_args()

    print(f"database: {database_path()}", flush=True)
    if args.command == "inspect":
        result = inspect_identifier_integrity()
    else:
        result = repair_identifier_integrity(backup=not args.no_backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
