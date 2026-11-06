from __future__ import annotations

import argparse
import getpass
import json

from research_app.storage import (
    admin_reset_reviewer_pin,
    collab_admin_configured,
    configure_collab_admin_key,
    database_path,
    initialize_legacy_pins,
    inspect_identifier_integrity,
    inspect_project_statistics,
    rebuild_project_statistics,
    repair_identifier_integrity,
    reviewers_without_pin,
    upgrade_database,
    verify_collab_admin_key,
)


def _prompt_pin(label: str) -> str:
    first = getpass.getpass(label)
    second = getpass.getpass("再次输入：")
    if first != second:
        raise SystemExit("两次输入不一致。")
    return first


def _admin_key() -> str:
    key = getpass.getpass("管理员恢复密钥：").strip()
    if not verify_collab_admin_key(key):
        raise SystemExit("管理员恢复密钥不正确。")
    return key


def _collab_admin_init() -> dict:
    if collab_admin_configured():
        raise SystemExit("管理员恢复密钥已存在，不能重复初始化。")
    first = getpass.getpass("设置管理员恢复密钥（8-128 字符）：")
    second = getpass.getpass("再次输入：")
    if first != second:
        raise SystemExit("两次输入不一致。")
    configure_collab_admin_key(first)
    return {"configured": True}


def _collab_init_legacy_pins() -> dict:
    admin_key = _admin_key()
    reviewers = reviewers_without_pin()
    if not reviewers:
        raise SystemExit("没有需要初始化的审核人。")
    pins = {}
    for reviewer in reviewers:
        pins[reviewer["id"]] = _prompt_pin(
            f"为 {reviewer['name']} 设置 PIN（4-32 字符）："
        )
    count = initialize_legacy_pins(
        pins,
        admin_key=admin_key,
        note="本机管理员旧库 PIN 初始化",
    )
    return {"initialized": count}


def _collab_reset_pin(reviewer_id: str, reason: str) -> dict:
    admin_key = _admin_key()
    pin = _prompt_pin("设置新的审核人 PIN（4-32 字符）：")
    admin_reset_reviewer_pin(
        reviewer_id,
        pin,
        admin_key=admin_key,
        note=reason,
    )
    return {"reviewer_id": reviewer_id, "reset": True, "reason": reason}


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
    subparsers.add_parser(
        "collab-admin-init",
        help="设置独立的本机管理员恢复密钥（仅首次）",
    )
    subparsers.add_parser(
        "collab-init-legacy-pins",
        help="为旧库中没有 PIN 的审核人批量初始化 PIN（需管理员密钥）",
    )
    collab_reset = subparsers.add_parser(
        "collab-reset-pin",
        help="由本机管理员重置某一审核人的 PIN（需管理员密钥）",
    )
    collab_reset.add_argument("--reviewer-id", required=True)
    collab_reset.add_argument("--reason", required=True)
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
    elif args.command == "collab-admin-init":
        result = _collab_admin_init()
    elif args.command == "collab-init-legacy-pins":
        result = _collab_init_legacy_pins()
    elif args.command == "collab-reset-pin":
        result = _collab_reset_pin(args.reviewer_id, args.reason)
    else:
        result = repair_identifier_integrity(backup=not args.no_backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
