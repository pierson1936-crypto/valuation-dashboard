from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitoring.assistant import RuleDraftAssistant
from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.notifications import build_notifier
from monitoring.presets import (
    DEFAULT_MOVE_PERCENT,
    MANAGED_RULE_NAMES,
    save_simple_setup,
    simple_rule_summary,
)
from monitoring.rules import (
    ALLOWED_DIRECTIONS,
    ALLOWED_METRICS,
    ALLOWED_OPERATORS,
    DIRECTION_LABELS,
    METRIC_LABELS,
    OPERATOR_LABELS,
    validate_rule_spec,
)
from monitoring.service import MonitorService


def _code(value: str) -> str:
    cleaned = str(value).strip()
    if not re.fullmatch(r"\d{6}", cleaned):
        raise argparse.ArgumentTypeError("代码必须是 6 位数字")
    return cleaned


def _config(db_override: str = "") -> MonitorConfig:
    config = MonitorConfig.from_env()
    if db_override:
        config = replace(config, db_path=Path(db_override).expanduser().resolve())
    return config


def _repository(config: MonitorConfig) -> MonitorRepository:
    repository = MonitorRepository(config.db_path)
    repository.initialize()
    return repository


def _print_watch_and_rules(repository: MonitorRepository) -> None:
    watches = repository.list_watch()
    rules = repository.list_rules()
    grouped: dict[str, list[dict]] = {}
    for rule in rules:
        grouped.setdefault(rule["code"], []).append(rule)
    if not watches:
        print("自选股为空。")
        return
    for watch in watches:
        position = ""
        if watch.get("quantity") is not None or watch.get("cost_price") is not None:
            position = " 持仓=%s 成本=%s" % (
                watch.get("quantity") if watch.get("quantity") is not None else "-",
                watch.get("cost_price") if watch.get("cost_price") is not None else "-",
            )
        print(
            "\n%s %s [%s]%s"
            % (
                watch["code"],
                watch["name"] or "(未命名)",
                "启用" if watch["enabled"] else "停用",
                position,
            )
        )
        for rule in grouped.get(watch["code"], []):
            print(
                "  #%s [%s] %s：%s %s %s，确认 %s 次，冷却 %s 分钟，回差 %s"
                % (
                    rule["id"],
                    "启用" if rule["enabled"] else "停用",
                    DIRECTION_LABELS[rule["direction"]],
                    METRIC_LABELS[rule["metric"]],
                    OPERATOR_LABELS[rule["operator"]],
                    rule["threshold"],
                    rule["confirm_count"],
                    int(rule["cooldown_seconds"] / 60),
                    rule["hysteresis"],
                )
            )


def _notification_channels(config: MonitorConfig) -> list[str]:
    channels = ["本地控制台"]
    if config.wecom_webhook_url:
        channels.append("企业微信群机器人")
    if config.serverchan_sendkey:
        channels.append("Server酱个人微信")
    return channels


def _format_price(value: float | None) -> str:
    return "-" if value is None else ("%g" % value)


def _print_simple_overview(repository: MonitorRepository) -> None:
    watches = repository.list_watch()
    if not watches:
        print("还没有盯盘标的。请先运行 quick-setup。")
        return
    grouped: dict[str, list[dict]] = {}
    for rule in repository.list_rules():
        grouped.setdefault(rule["code"], []).append(rule)
    for watch in watches:
        summary = simple_rule_summary(grouped.get(watch["code"], []))
        if not summary["configured"]:
            state = "尚未设置三线"
        else:
            move_text = (
                "±%s%%" % _format_price(summary["move_percent"])
                if summary["move_percent"] is not None
                else "未完整启用"
            )
            state = (
                "关注 %s｜风险 %s｜目标 %s｜异动 %s"
                % (
                    _format_price(summary["watch_price"]),
                    _format_price(summary["risk_price"]),
                    _format_price(summary["target_price"]),
                    move_text,
                )
            )
        print(
            "%s %s：%s"
            % (watch["code"], watch["name"] or "(未命名)", state)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自选股本地盯盘 Agent（确定性规则，分钟轮询不调用大模型）"
    )
    parser.add_argument(
        "--db",
        default="",
        help="覆盖数据库路径；默认 data/monitor.db",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化本地数据库")

    quick_setup = sub.add_parser(
        "quick-setup", help="用关注价、风险价和可选目标价快速设置盯盘"
    )
    quick_setup.add_argument("code", type=_code)
    quick_setup.add_argument("--name", default="")
    quick_setup.add_argument("--watch-price", type=float, required=True)
    quick_setup.add_argument("--risk-price", type=float, required=True)
    quick_setup.add_argument("--target-price", type=float)
    quick_setup.add_argument(
        "--move-pct",
        type=float,
        default=DEFAULT_MOVE_PERCENT,
        help=argparse.SUPPRESS,
    )

    add_stock = sub.add_parser("add-stock", help="新增或更新自选股")
    add_stock.add_argument("code", type=_code)
    add_stock.add_argument("--name", default="")
    add_stock.add_argument("--quantity", type=float)
    add_stock.add_argument("--cost", type=float)
    add_stock.add_argument("--notes", default="")

    remove_stock = sub.add_parser("remove-stock", help="删除自选股及其规则")
    remove_stock.add_argument("code", type=_code)

    add_rule = sub.add_parser("add-rule", help="新增一条确定性监控规则")
    add_rule.add_argument("code", type=_code)
    add_rule.add_argument("--name", required=True)
    add_rule.add_argument(
        "--direction", choices=sorted(ALLOWED_DIRECTIONS), required=True
    )
    add_rule.add_argument("--metric", choices=sorted(ALLOWED_METRICS), required=True)
    add_rule.add_argument(
        "--operator", choices=sorted(ALLOWED_OPERATORS), required=True
    )
    add_rule.add_argument("--threshold", type=float, required=True)
    add_rule.add_argument("--confirm", type=int, default=1)
    add_rule.add_argument("--cooldown-minutes", type=int, default=60)
    add_rule.add_argument("--hysteresis", type=float, default=0)

    for name, enabled, help_text in (
        ("enable-rule", True, "启用规则"),
        ("disable-rule", False, "停用规则"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("rule_id", type=int)
        command.set_defaults(target_enabled=enabled)

    delete_rule = sub.add_parser("delete-rule", help="删除规则")
    delete_rule.add_argument("rule_id", type=int)

    sub.add_parser("list", help="查看自选股和规则")
    sub.add_parser("overview", help="只查看三线价格和自动异动状态")
    sub.add_parser("status", help="查看数据库、保留策略和通知配置")

    once = sub.add_parser("run-once", help="执行一轮盯盘")
    once.add_argument(
        "--force", action="store_true", help="忽略交易时段限制，用于测试"
    )

    sub.add_parser("watch", help="持续盯盘，按 Ctrl+C 停止")

    events = sub.add_parser("events", help="查看最近触发事件")
    events.add_argument("--limit", type=int, default=30)

    digest = sub.add_parser("digest", help="生成本地事件摘要，不调用大模型")
    digest.add_argument("--days", type=int, default=1)

    sub.add_parser("cleanup", help="立即执行数据保留清理")

    notify = sub.add_parser("test-notify", help="向已配置渠道发送测试通知")
    notify.add_argument(
        "--yes",
        action="store_true",
        help="确认执行真实外部通知；没有此参数时只显示渠道",
    )

    suggest = sub.add_parser(
        "suggest-rules", help="按需调用 AI，把你的逻辑整理为规则草案"
    )
    suggest.add_argument("code", type=_code)
    suggest.add_argument("--logic", required=True)
    suggest.add_argument(
        "--force", action="store_true", help="忽略 7 天草案缓存，重新调用模型"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _config(args.db)
        repository = _repository(config)

        if args.command == "init":
            print("数据库已初始化：%s" % config.db_path)
        elif args.command == "quick-setup":
            summary = save_simple_setup(
                repository,
                args.code,
                args.name,
                args.watch_price,
                args.risk_price,
                args.target_price,
                args.move_pct,
            )
            print(
                "已设置 %s %s：关注 %s｜风险 %s｜目标 %s｜异动 ±%s%%"
                % (
                    summary["code"],
                    summary["name"],
                    _format_price(summary["watch_price"]),
                    _format_price(summary["risk_price"]),
                    _format_price(summary["target_price"]),
                    _format_price(summary["move_percent"]),
                )
            )
        elif args.command == "add-stock":
            repository.upsert_watch(
                args.code,
                args.name,
                args.quantity,
                args.cost,
                args.notes,
            )
            print("已保存自选股：%s %s" % (args.code, args.name))
        elif args.command == "remove-stock":
            print("已删除。" if repository.delete_watch(args.code) else "未找到该自选股。")
        elif args.command == "add-rule":
            if not any(item["code"] == args.code for item in repository.list_watch()):
                raise ValueError("请先用 add-stock 添加该代码")
            spec = validate_rule_spec(
                {
                    "name": args.name,
                    "direction": args.direction,
                    "metric": args.metric,
                    "operator": args.operator,
                    "threshold": args.threshold,
                    "confirm_count": args.confirm,
                    "cooldown_seconds": args.cooldown_minutes * 60,
                    "hysteresis": args.hysteresis,
                }
            )
            rule_id = repository.add_rule(args.code, **spec)
            print("规则已保存，ID=%d" % rule_id)
        elif args.command in {"enable-rule", "disable-rule"}:
            changed = repository.set_rule_enabled(args.rule_id, args.target_enabled)
            print("已更新。" if changed else "未找到该规则。")
        elif args.command == "delete-rule":
            print("已删除。" if repository.delete_rule(args.rule_id) else "未找到该规则。")
        elif args.command == "list":
            _print_watch_and_rules(repository)
        elif args.command == "overview":
            _print_simple_overview(repository)
        elif args.command == "status":
            print("数据库：%s" % config.db_path)
            print(
                "数据量：自选 %d，规则 %d，分钟快照 %d，每日快照 %d，事件 %d"
                % (
                    repository.count_rows("watchlist"),
                    repository.count_rows("rules"),
                    repository.count_rows("minute_quotes"),
                    repository.count_rows("daily_snapshots"),
                    repository.count_rows("events"),
                )
            )
            print(
                "保留：分钟数据 %d 天；事件 %d 天；每日快照长期保留；AI 草案缓存 %d 天"
                % (
                    config.minute_retention_days,
                    config.event_retention_days,
                    config.ai_draft_retention_days,
                )
            )
            print("轮询：%d 秒；完整分析刷新：%d 秒" % (
                config.poll_seconds,
                config.analysis_refresh_seconds,
            ))
            print("通知渠道：" + "、".join(_notification_channels(config)))
            print("分钟轮询 Token：0")
        elif args.command == "run-once":
            service = MonitorService(
                config, repository, notifier=build_notifier(config)
            )
            print(
                json.dumps(
                    service.run_once(force=args.force).to_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "watch":
            service = MonitorService(
                config, repository, notifier=build_notifier(config)
            )
            print(
                "开始盯盘：每 %d 秒检查一次；分钟循环不调用大模型。Ctrl+C 停止。"
                % config.poll_seconds
            )

            def report(summary):
                local = datetime.now().strftime("%H:%M:%S")
                print(
                    "[%s] 行情=%d 规则=%d 触发=%d%s"
                    % (
                        local,
                        summary.quote_count,
                        summary.evaluated_count,
                        summary.triggered_count,
                        " 跳过=" + summary.skipped_reason
                        if summary.skipped_reason
                        else "",
                    )
                )
                for error in summary.errors:
                    print("  注意：" + error)

            try:
                service.watch(report)
            except KeyboardInterrupt:
                print("\n盯盘已停止。")
        elif args.command == "events":
            rows = repository.list_events(args.limit)
            if not rows:
                print("暂无触发事件。")
            for event in rows:
                print(
                    "%s #%s %s %s=%s [%s]"
                    % (
                        event["occurred_at"],
                        event["id"],
                        event["code"],
                        event["metric"],
                        event["observed_value"],
                        event["notification_status"],
                    )
                )
                print("  " + event["message"].replace("\n", " | "))
        elif args.command == "digest":
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, args.days))
            rows = [
                event
                for event in repository.list_events(500)
                if datetime.fromisoformat(event["occurred_at"]) >= cutoff
            ]
            print("最近 %d 天共触发 %d 条规则。" % (max(1, args.days), len(rows)))
            for event in reversed(rows):
                print(
                    "- %s %s：%s=%s"
                    % (
                        event["code"],
                        event["direction"],
                        event["metric"],
                        event["observed_value"],
                    )
                )
        elif args.command == "cleanup":
            result = repository.cleanup(
                config.minute_retention_days, config.event_retention_days
            )
            print("清理完成：" + json.dumps(result, ensure_ascii=False))
        elif args.command == "test-notify":
            channels = _notification_channels(config)
            print("已配置渠道：" + "、".join(channels))
            if not args.yes:
                print("未发送。确认后运行：python -X utf8 monitor.py test-notify --yes")
            else:
                results = build_notifier(config).send(
                    "自选股盯盘测试",
                    "通知渠道连接测试。未触发任何买卖规则。",
                )
                for result in results:
                    print(
                        "%s：%s%s"
                        % (
                            result.channel,
                            "成功" if result.success else "失败",
                            ("（%s）" % result.error) if result.error else "",
                        )
                    )
        elif args.command == "suggest-rules":
            print(
                "AI 仅在本命令中调用一次；结果默认缓存 %d 天，不会加入分钟循环。"
                % config.ai_draft_retention_days
            )
            payload = RuleDraftAssistant(config, repository).suggest(
                args.code, args.logic, args.force
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print("草案未自动启用。确认价格后请使用 quick-setup 保存三线。")
        return 0
    except (ValueError, OSError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
