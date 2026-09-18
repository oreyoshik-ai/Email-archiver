from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from mail_archiver import __version__
from mail_archiver.config import imap_credentials_ready, init_local_config, load_config
from mail_archiver.pipeline import run_imap, run_local_paths, summarize
from mail_archiver.util import resolve_timezone, setup_logging

LOG = logging.getLogger("mail_archiver")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mail_archiver",
        description="邮件归档管理系统。默认打开本地网页，填邮箱后点按钮即可。",
    )
    parser.add_argument("--web", action="store_true", help="打开网页（默认，不需要加这个）")
    parser.add_argument("--cli", action="store_true", help="使用命令行，不打开页面")
    parser.add_argument("--port", type=int, default=8765, help="网页端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动网页但不自动打开浏览器")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--from-dir", help="从目录导入 .eml / .mbox")
    parser.add_argument("--from-file", help="导入单个 .eml 或 .mbox")
    parser.add_argument("--date", help="归档日期 YYYY-MM-DD，命令行模式使用")
    parser.add_argument("--init-config", action="store_true", help="生成 config.local.json")
    parser.add_argument("--self-test", action="store_true", help="运行内置自检")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"mail-archiver {__version__}")
    return parser


def parse_day(value: str | None, tz_name: str) -> date:
    tz = resolve_timezone(tz_name)
    if not value:
        return datetime.now(tz).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"日期格式错误，应为 YYYY-MM-DD: {value}") from exc


def run_cli_archive(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    use_local = bool(args.from_dir or args.from_file)
    use_imap = imap_credentials_ready(cfg) and not use_local
    if not use_imap and not use_local:
        print("未配置邮箱。双击「打开页面.bat」用网页填写，或加上 --from-dir。")
        return 2

    day = parse_day(args.date, cfg.timezone)
    LOG.info("归档根目录: %s", cfg.archive_root)
    try:
        if args.from_file:
            result = run_local_paths(cfg, [Path(args.from_file)])
        elif args.from_dir:
            result = run_local_paths(cfg, [Path(args.from_dir)])
        else:
            LOG.info("IMAP 筛选日期: %s", day.isoformat())
            result = run_imap(cfg, day)
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    LOG.info("%s", summarize(result))
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    from mail_archiver.util import configure_stdio

    configure_stdio()
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.init_config:
        path = init_local_config()
        print(f"已生成配置文件: {path}")
        return 0

    if args.self_test:
        from mail_archiver.selftest import run_self_test

        return run_self_test(verbose=args.verbose)

    cli_requested = args.cli or args.from_dir or args.from_file
    if cli_requested:
        return run_cli_archive(args)

    from mail_archiver.webapp import run_web

    return run_web(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
