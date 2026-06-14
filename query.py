#!/usr/bin/env python3
"""Paper Pusher 临时查询 —— agent / 对话式查询入口。

两种用法：

  1. **单次查询（digest 列表卡）**：搜索结果按引用量/日期排序后，做成一张
     飞书列表卡推送（不调 LLM）。剩余条目存 SQLite session，
     用 ``--search-more`` 续推下一页。

       python query.py --search "[TinyML]" --since 2022-01-01 --sort citations
       python query.py --search-more         # 续推下一页
       python query.py --search-clear        # 手动清掉 session

  2. **从列表卡指定推送**（用户从飞书列表卡里挑出几条，要 AI 摘要 + 单篇推送）：

       python query.py --push-item 3            # 推第 3 条
       python query.py --push-item 3,7,12       # 推第 3/7/12 条

     ``--push-item`` 读当前活跃 digest session 里对应 position 的论文，
     生成 LLM 摘要后用飞书单篇卡推送，并入主去重表
     （避免之后定时订阅又推一次）。

定时订阅推送在 ``subscribe.py`` 里。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path

from paper_pusher.article import PaperArticle
from paper_pusher.log_utils import get_logger
from paper_pusher.notifier import Notifier
from paper_pusher.pipeline import load_config, process_and_push
from paper_pusher.searcher import Searcher
from paper_pusher.storage import Storage

logger = get_logger("paper_pusher.query")


# ----------------------------------------------------------------------
# 搜索 + 排序（digest 用）
# ----------------------------------------------------------------------

def _search_and_sort(
    config: dict,
    query: str,
    since_str: str | None,
    limit: int,
    sort: str,
) -> tuple[list[PaperArticle] | None, datetime.date | None]:
    """跑一次临时查询并返回 (按要求排序的去重列表, since 日期)。

    日期解析失败时返回 ``(None, None)``，调用方应当作错误处理（return 1）。
    """
    schedule_cfg = config.setdefault('schedule', {}) or {}
    schedule_cfg['max_papers_per_query_db'] = limit

    if since_str:
        try:
            since = datetime.date.fromisoformat(since_str)
        except ValueError:
            logger.error(f"[错误] --since 日期格式应为 YYYY-MM-DD: {since_str}")
            return None, None
    else:
        since = datetime.date.today() - datetime.timedelta(days=365)

    field = (config.get('field') or '').strip() or 'search'

    searcher = Searcher(config)
    articles = searcher.search_query(query, field, since=since, label='cli-search')

    seen: set[str] = set()
    unique: list[PaperArticle] = []
    for a in articles:
        h = a.url_hash
        if h in seen:
            continue
        seen.add(h)
        unique.append(a)

    unique.sort(key=lambda a: a.publication_date_str or '', reverse=True)
    if sort == 'citations':
        unique.sort(
            key=lambda a: a.citation_count if a.citation_count is not None else -1,
            reverse=True,
        )
    return unique, since


# ----------------------------------------------------------------------
# --search：digest 列表卡 + 分页（session 存 SQLite）
# ----------------------------------------------------------------------

# 飞书卡片元素与 payload 上限的工程经验值——digest_size 超过 25 时强制 clip
_DIGEST_SIZE_HARD_CAP = 25
_DIGEST_SIZE_DEFAULT = 15


def _resolve_digest_size(cli_value: int | None, schedule_cfg: dict) -> int:
    if cli_value is not None:
        size = int(cli_value)
    else:
        size = int(schedule_cfg.get('search_digest_size', _DIGEST_SIZE_DEFAULT)
                   or _DIGEST_SIZE_DEFAULT)
    if size > _DIGEST_SIZE_HARD_CAP:
        logger.warning(
            f"[digest] digest_size={size} 超出硬上限 {_DIGEST_SIZE_HARD_CAP}，"
            f"clip 到 {_DIGEST_SIZE_HARD_CAP}"
        )
        size = _DIGEST_SIZE_HARD_CAP
    if size < 1:
        size = 1
    return size


def _push_digest_page(
    storage: Storage,
    notifier: Notifier,
    meta: dict,
    digest_size: int,
    offset: int,
) -> int:
    """取一页 → 推 → 成功则 mark + (末页) clear。返回 0/1 退出码。"""
    page_rows = storage.get_search_session_next_page(digest_size)
    if not page_rows:
        logger.info("[digest] session 已无剩余，清理脏 session")
        storage.clear_search_session()
        return 0

    items = [Searcher.rehydrate(row) for row in page_rows]
    total = int(meta.get('total', 0))
    is_last = (offset + len(items)) >= total

    results = notifier.send_digest(meta, items, offset=offset, is_last_page=is_last)
    pushed_ok = (any(results.values()) if notifier.has_notifiers else True)

    if not pushed_ok:
        logger.error("[digest] 推送失败，session 保留，可用 --search-more 重试")
        return 1

    storage.mark_search_session_pushed([row['position'] for row in page_rows])
    new_remaining = max(0, total - offset - len(items))
    if is_last:
        storage.clear_search_session()
        logger.info(
            f"[digest] 末页推送完成 ({offset + len(items)}/{total})，session 已清理"
        )
    else:
        logger.info(
            f"[digest] 推送成功 {len(items)} 条 (offset={offset})，"
            f"剩余 {new_remaining}，--search-more 继续"
        )
    return 0


def _run_search_digest(
    config: dict,
    query: str,
    limit: int,
    since_str: str | None,
    sort: str,
    digest_size: int | None,
    db_path: str,
) -> int:
    schedule_cfg = config.get('schedule', {}) or {}
    size = _resolve_digest_size(digest_size, schedule_cfg)

    unique, since = _search_and_sort(config, query, since_str, limit, sort)
    if unique is None:
        return 1

    if not unique:
        logger.info(f"[digest] 查询无结果：{query}（since={since.isoformat()}）")
        return 0

    storage = Storage(db_path)
    old_meta = storage.get_search_session_meta()
    if old_meta:
        old_remaining = storage.get_search_session_remaining_count()
        logger.info(
            f"[digest] 替换上一会话: {old_meta.get('query')!r} "
            f"({old_remaining} 剩余) → {query!r}"
        )

    storage.start_search_session(query, sort, size, unique)
    logger.info(
        f"[digest] 新会话: {len(unique)} 条 / 每页 {size} / 排序 {sort} "
        f"/ since={since.isoformat()}"
    )

    meta = storage.get_search_session_meta()
    if not meta:
        logger.error("[digest] 写入 session 后无法读回 meta，异常")
        return 1

    notifier = Notifier(config.get('notify', {}) or {})
    if not notifier.has_notifiers:
        logger.warning("[digest] 没有启用任何推送渠道，将以 stdout 形式输出")

    return _push_digest_page(storage, notifier, meta, size, offset=0)


def run_search_more(config: dict, db_path: str) -> int:
    storage = Storage(db_path)
    meta = storage.get_search_session_meta()
    if not meta:
        logger.info("[digest] 无活跃 digest 会话；先用 --search ... 启动")
        return 0

    remaining = storage.get_search_session_remaining_count()
    if remaining <= 0:
        logger.info("[digest] session 已无剩余，清理脏 session")
        storage.clear_search_session()
        return 0

    digest_size = int(meta.get('digest_size', _DIGEST_SIZE_DEFAULT)
                      or _DIGEST_SIZE_DEFAULT)
    total = int(meta.get('total', 0))
    offset = total - remaining

    notifier = Notifier(config.get('notify', {}) or {})
    if not notifier.has_notifiers:
        logger.warning("[digest] 没有启用任何推送渠道，将以 stdout 形式输出")

    return _push_digest_page(storage, notifier, meta, digest_size, offset=offset)


# ----------------------------------------------------------------------
# --push-item：用户从 digest 列表卡挑出指定 position，做 AI 摘要 + 单篇推送
# ----------------------------------------------------------------------

def _parse_positions(raw: str) -> list[int]:
    """把 ``"3,7,12"`` 解析成 ``[3, 7, 12]``；负数/0 报错。

    保留输入顺序与重复（让用户能多次推同一篇——尽管不太可能）。
    """
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    if not parts:
        raise ValueError("--push-item 需要至少一个 position（如 --push-item 3）")
    out: list[int] = []
    for p in parts:
        try:
            n = int(p)
        except ValueError as e:
            raise ValueError(f"--push-item 无法解析: {p!r}") from e
        if n < 1:
            raise ValueError(f"--push-item position 必须 ≥ 1，收到 {n}")
        out.append(n)
    return out


def run_push_item(config: dict, raw_positions: str, db_path: str) -> int:
    """从活跃 digest session 按 position 取出几篇 → LLM 摘要 → 飞书单篇推送。

    入主去重表（``mark_processed``），避免之后定时订阅又推一次。
    不修改 session 的 ``pushed_at``——列表卡推送与单篇 AI 摘要推送独立。
    """
    try:
        positions = _parse_positions(raw_positions)
    except ValueError as e:
        logger.error(f"[push-item] {e}")
        return 2

    storage = Storage(db_path)
    meta = storage.get_search_session_meta()
    if not meta:
        logger.info(
            "[push-item] 无活跃 digest 会话；先用 --search ... 启动一次查询"
        )
        return 0

    rows = storage.get_search_session_items_by_positions(positions)
    if not rows:
        logger.error(
            f"[push-item] 在当前 session 中没找到任何指定 position: {positions}"
        )
        return 1

    found_positions = {row['position'] for row in rows}
    missing = [p for p in positions if p not in found_positions]
    if missing:
        logger.warning(f"[push-item] 这些 position 不在 session 中，已跳过: {missing}")

    query_label = meta.get('query') or 'session'
    articles = [Searcher.rehydrate(row) for row in rows]
    header = f"⭐ 指定推送: {query_label}"
    for a in articles:
        a.header_override = header

    logger.info(
        f"[push-item] 取出 {len(articles)} 篇 (positions={sorted(found_positions)})，"
        f"开始摘要 + 推送..."
    )

    notifier = Notifier(config.get('notify', {}) or {})
    if not notifier.has_notifiers:
        logger.warning("[push-item] 没有启用任何推送渠道，摘要将只写库")

    sentinel = f"⭐ 从列表卡指定推送: {query_label}"
    pushed = process_and_push(
        articles, sentinel, config, storage, notifier, is_pending=False,
    )

    logger.info(f"[push-item] 完成，成功推送 {pushed}/{len(articles)} 篇")
    return 0


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Paper Pusher 临时查询 - 单次搜索 (digest 列表卡) / 指定推送',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # —— 单次查询（digest 列表卡，不调 LLM）——
  python query.py --search "[TinyML]" --since 2022-01-01 --sort citations
  python query.py --search "au[Yann LeCun]" --limit 30
  python query.py --search "[RAG] AND [evaluation]" --digest-size 10
  python query.py --search-more                                # 推下一页
  python query.py --search-clear                               # 手动清掉 session

  # —— 从列表卡指定推送（生成 AI 摘要 + 飞书单篇卡）——
  python query.py --push-item 3                                # 推第 3 条
  python query.py --push-item 3,7,12                           # 推第 3/7/12 条

定时订阅推送（守护 / --once / --stats）见 subscribe.py
        """,
    )
    parser.add_argument('--config', '-c', default='config.yaml',
                        help='配置文件路径 (默认: config.yaml)')
    parser.add_argument('--db', default='paper_pusher.db',
                        help='数据库文件路径 (默认: paper_pusher.db)')
    parser.add_argument('--search', metavar='QUERY',
                        help='单次查询（digest 列表卡）：搜索结果按 --sort 排序后切页'
                             '推送，每页 N 条由 --digest-size 决定，剩余条目存 SQLite '
                             'session，下次用 --search-more 续推。查询语法见 README。')
    parser.add_argument('--limit', type=int, default=20,
                        help='每个数据库最多返回多少篇 (默认: 20；挖经典时建议 50)')
    parser.add_argument('--since', metavar='YYYY-MM-DD',
                        help='发表日期下限；不指定时默认 1 年窗口'
                             '（独立于 config 的 max_age_days——后者只管定时推送）')
    parser.add_argument('--sort', choices=['date', 'citations'], default='date',
                        help='排序方式：date=发表日期降序（默认），'
                             'citations=引用量降序（适合挖经典/高影响力工作）')
    parser.add_argument('--digest-size', type=int, default=None,
                        help='单次查询每页条数 (默认: config 的 '
                             'search_digest_size 或 15；硬上限 25)')
    parser.add_argument('--search-more', action='store_true',
                        help='续推上一 digest session 的下一页；末页推送成功后自动清理 session')
    parser.add_argument('--search-clear', action='store_true',
                        help='手动清掉当前 digest session（items + metadata）')
    parser.add_argument('--push-item', metavar='N[,N,...]', default=None,
                        help='从活跃 digest session 按 position 取指定论文（用户从列表卡'
                             '看到的 [N] 编号），生成 AI 摘要后单篇推送到飞书并入主去重表。'
                             '示例: --push-item 3   或   --push-item 3,7,12')

    args = parser.parse_args()

    if not Path(args.config).exists():
        logger.error(f"[错误] 配置文件不存在: {args.config}")
        return 1

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"[错误] 加载配置文件失败: {e}")
        return 1

    # 调度优先级：--search-clear > --search-more > --push-item > --search
    if args.search_clear:
        Storage(args.db).clear_search_session()
        logger.info("[digest] session 已清理")
        return 0

    if args.search_more:
        try:
            return run_search_more(config, args.db)
        except KeyboardInterrupt:
            logger.info("")
            logger.info("已中断")
            return 0

    if args.push_item:
        try:
            return run_push_item(config, args.push_item, args.db)
        except KeyboardInterrupt:
            logger.info("")
            logger.info("已中断")
            return 0

    if not args.search:
        parser.print_help()
        logger.error("[错误] 必须指定 --search QUERY / --search-more / "
                     "--search-clear / --push-item")
        return 2

    try:
        return _run_search_digest(
            config,
            query=args.search,
            limit=args.limit,
            since_str=args.since,
            sort=args.sort,
            digest_size=args.digest_size,
            db_path=args.db,
        )
    except KeyboardInterrupt:
        logger.info("")
        logger.info("已中断")
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        logging.exception("程序异常退出")
        sys.exit(1)
