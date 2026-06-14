#!/usr/bin/env python3
"""Paper Pusher 订阅推送 —— 按 config.yaml 的 queries 定时搜索、摘要、推送。

用法::

    python subscribe.py --once             # 单次跑完即退
    python subscribe.py                    # 按 schedule.interval_minutes 周期跑（前台守护）
    python subscribe.py --stats            # 显示统计信息并退出
    python subscribe.py --config my.yaml   # 指定配置文件

临时查询、digest 列表卡等"对话式"功能在 ``query.py`` 里。
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import schedule

from paper_pusher.article import PaperArticle
from paper_pusher.log_utils import get_logger
from paper_pusher.notifier import Notifier
from paper_pusher.pipeline import load_config, process_and_push
from paper_pusher.searcher import Searcher
from paper_pusher.storage import Storage
from paper_pusher.summarizer import _is_content_valid

logger = get_logger("paper_pusher.subscribe")


# ----------------------------------------------------------------------
# 配额分配：按 query/label 轮询取，总数不超过 max_articles_per_run
# ----------------------------------------------------------------------

def _select_queries_round_robin(
    queries: list[dict],
    sample_quotas: dict,
    storage,
) -> list[dict]:
    """按 group 切桶，每组按持久化游标取连续 ``sample_quotas[组]`` 条 query。

    跨天**确定性轮转**：今天 cursor=0 取 [A,B] → 明天 cursor=2 取 [C,D]，
    走完一圈再回到 [A,B]。游标存在 ``storage.kv_store`` 的
    ``group_cursor:{group}`` 键里。

    - 组内 query 数 ≤ quota → 全跑（不动游标）
    - 组内 query 数 > quota → 按游标取 quota 条，更新游标
    - sample_quotas 未声明的组 → 全跑（不动游标）
    """
    by_group: dict[str, list[dict]] = defaultdict(list)
    for q in queries:
        g = (q.get('group') or '').strip() or '_default'
        by_group[g].append(q)

    selected: list[dict] = []
    declared = set(sample_quotas.keys())

    for group, quota in sample_quotas.items():
        bucket = by_group.get(group, [])
        if not bucket or quota <= 0:
            continue
        if quota >= len(bucket):
            selected.extend(bucket)
            continue

        cursor_key = f"group_cursor:{group}"
        try:
            cursor = int(storage.get_kv(cursor_key) or 0) % len(bucket)
        except (TypeError, ValueError):
            cursor = 0

        for i in range(quota):
            selected.append(bucket[(cursor + i) % len(bucket)])

        new_cursor = (cursor + quota) % len(bucket)
        storage.set_kv(cursor_key, str(new_cursor))

    for group, bucket in by_group.items():
        if group not in declared:
            selected.extend(bucket)
    return selected


def allocate_quota(articles: list, config: dict) -> list:
    """从同一个领域下的多维度查询结果里取出待处理列表。

    支持两种模式：

    * **分组配额**（``schedule.group_quotas`` 非空时）：
        1. 按 ``article.group`` 切桶；每个 group 有声明配额。
        2. 组内：先每条 query 保底 1 篇，再组内 query 间轮询补到组配额。
        3. 缺额（组内凑不齐配额）汇总后，从其他组剩余论文里按 query 轮询溢出补。
        4. 总数硬上限为 ``max_articles_per_run``。

    * **旧 round-robin**（无 ``group_quotas`` 或所有 article 无 group 时）：
        - 每个 ``feed_name`` 保底 1 篇 → 轮询填充至 ``max_articles_per_run``。
    """
    schedule_config = config.get('schedule', {}) or {}
    max_articles = schedule_config.get('max_articles_per_run', 12)
    if max_articles <= 0:
        return []

    group_quotas: dict = schedule_config.get('group_quotas', {}) or {}
    use_groups = bool(group_quotas) and any(
        getattr(a, 'group', None) for a in articles
    )

    if not use_groups:
        return _allocate_round_robin(articles, max_articles)

    return _allocate_by_group(articles, group_quotas, max_articles)


def _allocate_round_robin(articles: list, max_articles: int) -> list:
    """无分组配额时的旧逻辑：按 feed_name 轮询。"""
    by_feed = defaultdict(deque)
    for article in articles:
        by_feed[article.feed_name].append(article)

    feed_names = list(by_feed.keys())
    taken: list = []

    for feed in feed_names:
        if by_feed[feed]:
            taken.append(by_feed[feed].popleft())
            if len(taken) >= max_articles:
                return taken

    while len(taken) < max_articles:
        any_taken = False
        for feed in feed_names:
            if by_feed[feed]:
                taken.append(by_feed[feed].popleft())
                any_taken = True
                if len(taken) >= max_articles:
                    return taken
        if not any_taken:
            break

    return taken


def _allocate_by_group(
    articles: list,
    group_quotas: dict,
    max_articles: int,
) -> list:
    """分组配额分配。"""
    by_group: dict[str, list] = defaultdict(list)
    for a in articles:
        by_group[getattr(a, 'group', None) or '_default'].append(a)

    taken: list = []
    taken_ids: set[int] = set()

    for group, quota in group_quotas.items():
        if quota <= 0:
            continue
        group_articles = by_group.get(group, [])
        if not group_articles:
            logger.info(f"[配额] {group} 组当天无新论文，缺额 {quota} 待溢出补")
            continue

        by_feed = defaultdict(deque)
        for a in group_articles:
            by_feed[a.feed_name].append(a)
        feeds = list(by_feed.keys())

        group_taken_count = 0
        for f in feeds:
            if group_taken_count >= quota:
                break
            if by_feed[f]:
                article = by_feed[f].popleft()
                taken.append(article)
                taken_ids.add(id(article))
                group_taken_count += 1

        while group_taken_count < quota:
            any_taken = False
            for f in feeds:
                if group_taken_count >= quota:
                    break
                if by_feed[f]:
                    article = by_feed[f].popleft()
                    taken.append(article)
                    taken_ids.add(id(article))
                    group_taken_count += 1
                    any_taken = True
            if not any_taken:
                break

        if group_taken_count < quota:
            logger.info(
                f"[配额] {group} 组只凑到 {group_taken_count}/{quota} 篇，"
                f"缺 {quota - group_taken_count} 篇待其他组溢出补"
            )

    target_total = min(sum(max(q, 0) for q in group_quotas.values()), max_articles)
    if len(taken) < target_total:
        leftover_by_feed = defaultdict(deque)
        for group_articles in by_group.values():
            for a in group_articles:
                if id(a) not in taken_ids:
                    leftover_by_feed[a.feed_name].append(a)

        feeds = list(leftover_by_feed.keys())
        while len(taken) < target_total:
            any_taken = False
            for f in feeds:
                if len(taken) >= target_total:
                    break
                if leftover_by_feed[f]:
                    article = leftover_by_feed[f].popleft()
                    taken.append(article)
                    taken_ids.add(id(article))
                    any_taken = True
            if not any_taken:
                break

    return taken[:max_articles]


# ----------------------------------------------------------------------
# 单次运行
# ----------------------------------------------------------------------

def run_once(config: dict, storage: Storage):
    logger.info("")
    logger.info("=" * 50)
    logger.info("运行 Paper Pusher (订阅)")
    logger.info("=" * 50)

    field: str = (config.get('field') or '').strip()
    queries: list[dict] = config.get('queries', []) or []
    schedule_cfg: dict = config.get('schedule', {}) or {}
    max_articles = schedule_cfg.get('max_articles_per_run', 12)
    auto_fill = schedule_cfg.get('auto_fill_pending', True)
    max_age_days = schedule_cfg.get('max_age_days', 14)
    sample_queries = schedule_cfg.get('sample_queries', False)
    group_quotas: dict = schedule_cfg.get('group_quotas', {}) or {}
    sample_quotas: dict = schedule_cfg.get('queries_per_group', {}) or group_quotas

    if not queries:
        logger.warning("[警告] 没有配置任何 queries")
        return

    if field:
        logger.info(f"关注领域: {field}")

    if sample_queries and sample_quotas:
        queries_to_run = _select_queries_round_robin(queries, sample_quotas, storage)
        logger.info(
            f"[轮转] 配置 {len(queries)} 条 query，本次取 {len(queries_to_run)} 条："
            f"{[q.get('label') or q.get('query') for q in queries_to_run]}"
        )
    else:
        queries_to_run = queries
        logger.info(
            f"维度数: {len(queries)}（{[q.get('label') or q.get('query') for q in queries]}）"
        )

    searcher = Searcher(config)
    since = datetime.date.today() - datetime.timedelta(days=max_age_days)

    logger.info("")
    logger.info(f"[搜索] since={since.isoformat()}")
    raw_results: list[PaperArticle] = []
    for q_cfg in queries_to_run:
        q = (q_cfg.get('query') or '').strip()
        if not q:
            continue
        label = (q_cfg.get('label') or '').strip() or None
        category = (q_cfg.get('category') or field or '').strip()
        group = (q_cfg.get('group') or '').strip() or None
        articles = searcher.search_query(q, category, since=since, label=label, group=group)
        raw_results.extend(articles)

    new_articles = storage.filter_new_articles(raw_results)
    logger.info(f"新论文: {len(new_articles)} 篇（去重前 {len(raw_results)}）")

    articles_to_process = allocate_quota(new_articles, config)

    notifier = Notifier(config.get('notify', {}) or {})
    if not notifier.has_notifiers:
        logger.warning("[警告] 没有启用任何推送渠道，摘要将只保存到数据库")

    if articles_to_process:
        selected_set = {id(a) for a in articles_to_process}
        excess = [a for a in new_articles if id(a) not in selected_set]
        skipped = 0
        for article in excess:
            if not _is_content_valid(article.content):
                storage.mark_processed(article, None)
                skipped += 1
                continue
            storage.store_pending(article)
        kept = len(excess) - skipped
        if kept:
            logger.info(f"[待推送] 已存入 {kept} 篇待推送")
        if skipped:
            logger.info(f"[待推送] 跳过 {skipped} 篇内容过短/无效（已标记为已处理）")

    if not articles_to_process:
        pending = storage.get_pending_articles(max_articles)
        if pending:
            logger.info("")
            logger.info(f"[存稿] 从数据库恢复 {len(pending)} 篇存稿")
            process_and_push(
                pending, "📦 以下是之前存储的论文",
                config, storage, notifier, is_pending=True,
            )
        else:
            logger.info("")
            logger.info("没有新论文需要处理")
        return

    logger.info("")
    logger.info(f"本次处理 {len(articles_to_process)} 篇")

    total_pushed = process_and_push(
        articles_to_process, "📚 以下是最新论文",
        config, storage, notifier, is_pending=False,
    )

    if auto_fill and len(articles_to_process) < max_articles:
        remaining = max_articles - len(articles_to_process)
        pending_to_fill = storage.get_pending_articles(remaining)
        if pending_to_fill:
            logger.info("")
            logger.info(
                f"[补全] 新内容不足({len(articles_to_process)}篇)，"
                f"从存稿补全 {len(pending_to_fill)} 篇"
            )
            total_pushed += process_and_push(
                pending_to_fill,
                f"📦 从存稿补全 {len(pending_to_fill)} 篇",
                config, storage, notifier, is_pending=True,
            )

    logger.info("")
    logger.info("=" * 50)
    logger.info(f"完成! 成功推送 {total_pushed} 篇论文")
    stats = storage.get_stats()
    logger.info(f"数据库共记录 {stats['total_articles']} 篇论文")
    if stats.get('pending_articles', 0) > 0:
        logger.info(f"待推送存稿: {stats['pending_articles']} 篇")
    logger.info("=" * 50)


# ----------------------------------------------------------------------
# 调度
# ----------------------------------------------------------------------

def run_scheduler(config: dict, storage: Storage):
    interval = config.get('schedule', {}).get('interval_minutes', 360)
    logger.info(f"启动定时任务，每 {interval} 分钟执行一次")
    logger.info("按 Ctrl+C 停止")
    logger.info("")
    run_once(config, storage)
    schedule.every(interval).minutes.do(run_once, config=config, storage=storage)
    while True:
        schedule.run_pending()
        time.sleep(1)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Paper Pusher 订阅推送 - 按 config.yaml 定时搜索、摘要、推送',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python subscribe.py --once                 # 单次跑完即退
  python subscribe.py                        # 周期守护（前台 Ctrl+C 退出）
  python subscribe.py --stats                # 数据库统计
  python subscribe.py --config my.yaml       # 切换配置

临时查询 / digest 列表卡见 query.py
        """,
    )
    parser.add_argument('--config', '-c', default='config.yaml',
                        help='配置文件路径 (默认: config.yaml)')
    parser.add_argument('--once', action='store_true',
                        help='只执行一次，不启动定时任务')
    parser.add_argument('--stats', action='store_true',
                        help='显示统计信息并退出')
    parser.add_argument('--db', default='paper_pusher.db',
                        help='数据库文件路径 (默认: paper_pusher.db)')

    args = parser.parse_args()

    if not Path(args.config).exists():
        logger.error(f"[错误] 配置文件不存在: {args.config}")
        return 1

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"[错误] 加载配置文件失败: {e}")
        return 1

    storage = Storage(args.db)

    if args.stats:
        stats = storage.get_stats()
        logger.info("")
        logger.info("Paper Pusher 统计")
        logger.info("=" * 40)
        logger.info(f"总论文数: {stats['total_articles']}")
        if stats.get('pending_articles', 0) > 0:
            logger.info(f"待推送: {stats['pending_articles']} 篇")
        if stats.get('by_query'):
            logger.info("")
            logger.info("按维度统计:")
            for q, count in stats['by_query'].items():
                logger.info(f"  - {q[:60]}: {count}")
        return 0

    try:
        if args.once:
            run_once(config, storage)
        else:
            run_scheduler(config, storage)
    except KeyboardInterrupt:
        logger.info("")
        logger.info("已停止")
        return 0

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        logging.exception("程序异常退出")
        sys.exit(1)
