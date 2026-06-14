"""推送模块 - 飞书/Email（论文版增强）。

相对 my2：
- 飞书卡片头部用 "📚 {category}"（论文场景）；
- PaperArticle 额外渲染"作者/期刊/DOI/引用/发表日期"备注；
- 有 pdf_url 时多一个"📥 下载PDF"按钮。
"""

from __future__ import annotations

import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict

import requests

from .article import Article, PaperArticle
from .log_utils import get_logger

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 推送（带主动节流 + 11232 限频重试）"""

    DEFAULT_MIN_INTERVAL_SEC = 1.2
    DEFAULT_MAX_RATE_LIMIT_RETRIES = 3

    def __init__(
        self,
        webhook_url: str,
        min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
        max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
    ):
        self.webhook_url = webhook_url
        self.min_interval_sec = float(min_interval_sec)
        self.max_rate_limit_retries = int(max_rate_limit_retries)
        self._last_send_at = 0.0

    def _wait_for_slot(self):
        elapsed = time.monotonic() - self._last_send_at
        if 0 < elapsed < self.min_interval_sec:
            time.sleep(self.min_interval_sec - elapsed)

    def _post_with_retry(self, payload: dict, label: str = "") -> bool:
        suffix = f" {label}" if label else ""
        for attempt in range(self.max_rate_limit_retries + 1):
            self._wait_for_slot()
            try:
                response = requests.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
            except Exception as e:
                logger.error(f"[飞书] 发送异常{suffix}: {e}")
                return False
            self._last_send_at = time.monotonic()

            if response.status_code != 200:
                logger.error(f"[飞书] HTTP 错误{suffix}: {response.status_code}")
                return False

            try:
                result = response.json()
            except ValueError:
                logger.error(f"[飞书] 非 JSON 响应{suffix}: {response.text[:200]}")
                return False

            if result.get('code') == 0 or result.get('StatusCode') == 0:
                return True

            if result.get('code') == 11232 and attempt < self.max_rate_limit_retries:
                backoff = 2 ** attempt + 1   # 2, 3, 5
                logger.warning(
                    f"[飞书] 限频 (11232){suffix}，{backoff}s 后重试 "
                    f"({attempt + 1}/{self.max_rate_limit_retries})"
                )
                time.sleep(backoff)
                continue

            logger.error(f"[飞书] 发送失败{suffix}: {result}")
            return False
        return False

    def send(self, article: Article, summary: str) -> bool:
        is_paper = isinstance(article, PaperArticle)
        override = (article.header_override
                    if is_paper and article.header_override else None)
        if override:
            # 完整覆盖（如 --search 的 "🔍 [LLM] AND [agent]"），不加 emoji 前缀
            header_prefix = ""
            header_text = override
        else:
            header_prefix = "📚" if is_paper else "📰"
            # 优先用 group（如 "tinyml"），其次 category，最后 feed_name
            header_text = (
                (article.group if is_paper and article.group else None)
                or article.category
                or article.feed_name
            )
        # category 可能为空，feed_name 可能很长 —— 截断保证可读
        if len(header_text) > 50:
            header_text = header_text[:50] + "…"
        full_header = (
            f"{header_prefix} {header_text}" if header_prefix else header_text
        )

        elements: list[dict] = [
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": f"原标题: {article.title}"}
            ]},
        ]

        if is_paper:
            meta_parts: list[str] = []
            if article.authors:
                head = ", ".join(article.authors[:3])
                if len(article.authors) > 3:
                    head += " et al."
                meta_parts.append(f"作者: {head}")
            if article.journal:
                meta_parts.append(f"期刊: {article.journal}")
            if article.doi:
                meta_parts.append(f"DOI: {article.doi}")
            if article.citation_count is not None:
                meta_parts.append(f"引用: {article.citation_count}")
            if article.publication_date_str:
                meta_parts.append(f"发表: {article.publication_date_str}")
            if meta_parts:
                elements.append({"tag": "note", "elements": [
                    {"tag": "plain_text", "content": " · ".join(meta_parts)}
                ]})

        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": summary},
            {"tag": "hr"},
        ])

        actions = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔗 阅读原文"},
            "type": "primary",
            "url": article.url,
        }]
        if is_paper and article.pdf_url:
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📥 下载 PDF"},
                "type": "default",
                "url": article.pdf_url,
            })
        elements.append({"tag": "action", "actions": actions})

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": full_header,
                    },
                    "template": "blue",
                },
                "elements": elements,
            },
        }
        return self._post_with_retry(payload, label=article.title[:20])

    def send_text(self, text: str) -> bool:
        payload = {"msg_type": "text", "content": {"text": text}}
        return self._post_with_retry(payload, label="text")

    # ---- digest (search list card) ---------------------------------------

    @staticmethod
    def _format_digest_item_markdown(article: PaperArticle, rank: int) -> str:
        title = (article.title or "(no title)").replace("[", "［").replace("]", "］")
        url = article.url or ""
        line1 = f"**[{rank}] [{title}]({url})**" if url else f"**[{rank}] {title}**"

        meta_bits: list[str] = []
        if article.authors:
            head = ", ".join(article.authors[:3])
            if len(article.authors) > 3:
                head += " et al."
            meta_bits.append(head)
        if article.journal:
            meta_bits.append(article.journal)
        if article.publication_date_str:
            meta_bits.append(article.publication_date_str)
        if article.citation_count is not None:
            meta_bits.append(f"引用 {article.citation_count}")
        line2 = " · ".join(meta_bits) if meta_bits else ""

        line3_bits: list[str] = []
        if article.doi:
            line3_bits.append(f"DOI: {article.doi}")
        if article.pdf_url:
            line3_bits.append(f"[📥 PDF]({article.pdf_url})")
        line3 = " · ".join(line3_bits)

        return "\n".join(s for s in (line1, line2, line3) if s)

    def send_digest(
        self,
        meta: dict,
        items: list,
        offset: int,
        is_last_page: bool,
    ) -> bool:
        """发送 digest 列表卡。``items`` 为 PaperArticle 列表，按 position 排序。

        ``offset`` 是本批第一条在整 session 中的 0-based 位置（用于显示编号）。
        """
        query = meta.get("query", "?")
        sort = meta.get("sort", "?")
        total = int(meta.get("total", 0))
        digest_size = int(meta.get("digest_size", len(items)))

        start_rank = offset + 1
        end_rank = offset + len(items)
        remaining = max(0, total - end_rank)

        header_text = f"🔍 {query}"
        if len(header_text) > 50:
            header_text = header_text[:50] + "…"

        top_note = (
            f"本批 {start_rank}-{end_rank} / 共 {total} · "
            f"排序: {sort} · 剩余 {remaining}"
        )
        if is_last_page:
            bottom_note = f"✅ 已推送完毕 {total}/{total}"
        else:
            bottom_note = (
                f"继续推送下一批（每批 {digest_size}）: "
                f"python main.py --search-more"
            )

        elements: list[dict] = [
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": top_note}
            ]},
            {"tag": "hr"},
        ]
        for i, article in enumerate(items):
            if i > 0:
                elements.append({"tag": "hr"})
            elements.append({
                "tag": "markdown",
                "content": self._format_digest_item_markdown(
                    article, rank=offset + 1 + i
                ),
            })
        elements.extend([
            {"tag": "hr"},
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": bottom_note}
            ]},
        ])

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": header_text},
                    "template": "blue",
                },
                "elements": elements,
            },
        }
        page_no = offset // max(1, digest_size) + 1
        return self._post_with_retry(payload, label=f"digest p{page_no}")


class EmailNotifier:
    """Email SMTP 推送（保持 my2 行为，论文 metadata 追加到正文）"""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        to_address: str,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.to_address = to_address

    def send(self, article: Article, summary: str) -> bool:
        meta_html = ""
        is_paper = isinstance(article, PaperArticle)
        override = (article.header_override
                    if is_paper and article.header_override else None)
        if override:
            header_text = override
            header_emoji = ""
        else:
            header_text = (
                (article.group if is_paper and article.group else None)
                or article.category
                or article.feed_name
            )
            header_emoji = "📚 "
        if is_paper:
            bits: list[str] = []
            if article.authors:
                bits.append(f"作者: {', '.join(article.authors[:5])}")
            if article.journal:
                bits.append(f"期刊: {article.journal}")
            if article.doi:
                bits.append(f"DOI: {article.doi}")
            if article.citation_count is not None:
                bits.append(f"引用: {article.citation_count}")
            if article.publication_date_str:
                bits.append(f"发表: {article.publication_date_str}")
            if bits:
                meta_html = (
                    '<p style="color: #555; font-size: 13px;">'
                    + " · ".join(bits) + "</p>"
                )

        pdf_button = ""
        if isinstance(article, PaperArticle) and article.pdf_url:
            pdf_button = (
                f'&nbsp;<a href="{article.pdf_url}" '
                'style="display: inline-block; background: #444; color: white; '
                'padding: 10px 20px; text-decoration: none; border-radius: 4px;">'
                '📥 下载 PDF</a>'
            )

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px;">
                <p style="color: #666; margin: 0;">{header_emoji}{header_text}</p>
                <h2 style="margin: 10px 0;">{article.title}</h2>
                {meta_html}
                <p style="line-height: 1.6; white-space: pre-wrap;">{summary}</p>
                <a href="{article.url}"
                   style="display: inline-block; background: #1a73e8; color: white;
                          padding: 10px 20px; text-decoration: none; border-radius: 4px;">
                    🔗 阅读原文
                </a>{pdf_button}
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Paper] {article.title}"
        msg['From'] = self.username
        msg['To'] = self.to_address
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"[Email] 发送失败: {e}")
            return False

    def send_text(self, text: str) -> bool:
        msg = MIMEText(text, 'plain', 'utf-8')
        msg['Subject'] = "[Paper Pusher] 通知"
        msg['From'] = self.username
        msg['To'] = self.to_address
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"[Email] 发送失败: {e}")
            return False


class Notifier:
    """统一的通知管理器（保持 my2 行为）"""

    def __init__(self, config: dict):
        self.notifiers = []

        feishu_config = config.get('feishu', {}) or {}
        if feishu_config.get('enabled') and feishu_config.get('webhook_url'):
            self.notifiers.append(
                ('飞书', FeishuNotifier(
                    feishu_config['webhook_url'],
                    min_interval_sec=feishu_config.get(
                        'min_interval_sec', FeishuNotifier.DEFAULT_MIN_INTERVAL_SEC
                    ),
                    max_rate_limit_retries=feishu_config.get(
                        'max_rate_limit_retries',
                        FeishuNotifier.DEFAULT_MAX_RATE_LIMIT_RETRIES,
                    ),
                ))
            )

        email_config = config.get('email', {}) or {}
        if email_config.get('enabled') and email_config.get('username'):
            self.notifiers.append(
                ('Email', EmailNotifier(
                    email_config['smtp_host'],
                    email_config['smtp_port'],
                    email_config['username'],
                    email_config['password'],
                    email_config['to'],
                ))
            )

    def notify(self, article: Article, summary: str) -> Dict[str, bool]:
        results = {}
        for name, notifier in self.notifiers:
            logger.info(f"[推送] {name}: {article.title[:30]}...")
            results[name] = notifier.send(article, summary)
        return results

    def send_text(self, text: str) -> Dict[str, bool]:
        results = {}
        for name, notifier in self.notifiers:
            if hasattr(notifier, 'send_text'):
                results[name] = notifier.send_text(text)
            else:
                results[name] = False
        return results

    def send_digest(
        self,
        meta: dict,
        items: list,
        offset: int,
        is_last_page: bool,
    ) -> Dict[str, bool]:
        """digest 列表卡：仅飞书；其它渠道日志跳过。"""
        results: Dict[str, bool] = {}
        any_channel = False
        for name, notifier in self.notifiers:
            if isinstance(notifier, FeishuNotifier):
                any_channel = True
                logger.info(
                    f"[推送] digest 飞书: {len(items)} 条 (offset={offset}, "
                    f"last={is_last_page})"
                )
                results[name] = notifier.send_digest(
                    meta, items, offset, is_last_page
                )
            else:
                logger.warning(
                    f"[推送] digest 暂不支持 {name} 渠道，跳过 "
                    f"({len(items)} 条)"
                )
                results[name] = False
        if not any_channel:
            # stdout fallback：没有飞书时打印列表，调用方不应 mark pushed
            print(f"\n=== Digest (offset={offset}, last={is_last_page}) ===")
            print(f"query={meta.get('query')!r}  sort={meta.get('sort')}  "
                  f"total={meta.get('total')}")
            for i, a in enumerate(items):
                rank = offset + 1 + i
                print(f"  [{rank}] {getattr(a, 'title', '?')}  "
                      f"<{getattr(a, 'url', '')}>")
            print("=== /digest ===\n")
        return results

    @property
    def has_notifiers(self) -> bool:
        return len(self.notifiers) > 0
