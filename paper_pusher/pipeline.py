"""共享流水线辅助：配置加载、摘要 + 推送 + 标记。

被 subscribe.py（定时订阅）和 query.py（--push-item 从列表卡指定推送）共用。
独立成模块是为了不让两个入口脚本互相 import。
"""

from __future__ import annotations

import os
import re
import time

import yaml

from .log_utils import get_logger
from .searcher import Searcher
from .summarizer import MIN_SUMMARY_LENGTH, Summarizer

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------------

_ENV_PATTERN = re.compile(r'\$\{(\w+)\}')


def load_config(config_path: str = "config.yaml") -> dict:
    """读取 YAML 并替换 ``${VAR}`` 风格的环境变量占位符。未设置的变量保持原样。"""
    with open(config_path, 'r', encoding='utf-8') as f:
        content = f.read()

    def replace_env(match):
        var_name = match.group(1)
        return os.environ.get(var_name) or match.group(0)

    content = _ENV_PATTERN.sub(replace_env, content)
    return yaml.safe_load(content)


# ----------------------------------------------------------------------
# 摘要 + 推送 + 标记
# ----------------------------------------------------------------------

def _rehydrate_pending(records: list) -> list:
    out: list = []
    for r in records:
        if isinstance(r, dict):
            out.append(Searcher.rehydrate(r))
        else:
            out.append(r)
    return out


def process_and_push(
    articles: list,
    sentinel: str,
    config: dict,
    storage,
    notifier,
    is_pending: bool = False,
) -> int:
    """统一处理：摘要 → 推送 → 标记。返回成功推送条数。

    ``is_pending=True`` 表示来源是 pending 队列（已 mark_processed 过），
    只需 ``mark_pushed``；否则首次见到，走 ``mark_processed``。
    """
    summarizer = Summarizer(config.get('llm', {}) or {})
    article_objs = _rehydrate_pending(articles)

    logger.info(
        f"并行生成 {len(article_objs)} 篇摘要 (workers={summarizer.max_workers})..."
    )
    results = summarizer.summarize_batch(article_objs)

    # 重试失败的摘要（最多 2 轮）
    for retry_round in range(2):
        failed = [
            (i, article) for i, (article, s) in enumerate(results)
            if not s or len(s.strip()) < MIN_SUMMARY_LENGTH
        ]
        if not failed:
            break
        logger.info(f"[重试] 第 {retry_round + 1} 轮，{len(failed)} 篇重试中...")
        for idx, article in failed:
            time.sleep(1)
            s = summarizer.summarize(article)
            if s and len(s.strip()) >= MIN_SUMMARY_LENGTH:
                results[idx] = (article, s)

    if notifier.has_notifiers:
        notifier.send_text(sentinel)

    success_count = 0
    for i, (article, summary) in enumerate(results, 1):
        logger.info("")
        logger.info(f"--- 论文 {i}/{len(results)} ---")
        logger.info(f"标题: {article.title[:60]}...")
        logger.info(f"来源: {article.feed_name}")

        if summary and len(summary.strip()) >= MIN_SUMMARY_LENGTH:
            logger.info(f"摘要: {summary[:100]}...")
            if notifier.has_notifiers:
                for channel, ok in notifier.notify(article, summary).items():
                    status = "OK" if ok else "FAIL"
                    logger.info(f"  [{status}] {channel}")
            if is_pending:
                storage.mark_pushed(article.url_hash)
            else:
                storage.mark_processed(article, summary, pushed=1)
            success_count += 1
        else:
            if not summary:
                logger.warning("[跳过] 摘要失败")
            else:
                logger.warning(
                    f"[跳过] 摘要内容不足 ({len(summary.strip())}字): {summary[:200]}"
                )
            if is_pending:
                storage.mark_pushed(article.url_hash)
            else:
                storage.mark_processed(article, None)

    return success_count
