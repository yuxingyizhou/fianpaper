"""Article 数据结构 —— 本地定义，避免依赖 my2。

Article 是普通文章的最小载体（兼容 my2 字段语义），PaperArticle 是论文版扩展，
带 DOI/作者/期刊/PDF/引用等字段，并用 DOI（或标题+首作者）做去重哈希。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Article:
    """通用文章载体（沿用 my2 的字段语义）。

    Attributes
    ----------
    title : str
        文章标题。
    url : str
        原文链接。
    content : str
        正文 / 摘要（用于 LLM 输入）。
    published : datetime | None
        发布时间。
    feed_name : str
        来源标识。论文场景为 ``"{query}@{db}"``。
    category : str
        分类（来自 fixed_queries 配置或随机查询池的后缀）。
    """

    title: str
    url: str
    content: str
    published: Optional[datetime]
    feed_name: str
    category: str

    @property
    def url_hash(self) -> str:
        """以 URL 为基础的 16 位哈希，用于 SQLite 主键去重。"""
        return hashlib.sha256((self.url or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class PaperArticle(Article):
    """论文版 Article。DOI 优先做去重，缺 DOI 时退化为标题+首作者签名。

    Attributes
    ----------
    doi : str | None
        DOI 字符串（大小写不敏感，存储时转 lower）。
    authors : list[str]
        作者列表（保持论文给出的顺序）。
    journal : str | None
        来源期刊 / 会议 / 出版物名称（对应 ``Paper.source.name``）。
    pdf_url : str | None
        PDF 直链（用于飞书"下载 PDF"按钮）。
    citation_count : int | None
        引用数（来自 enrichment）。
    publication_date_str : str | None
        ISO 格式发表日期字符串（便于卡片直接渲染）。
    paper_json : dict | None
        ``Paper.to_dict()`` 的完整 dict，存到 SQLite 后可用
        ``Paper.from_dict()`` 重新水合。
    group : str | None
        配额分组键（如 ``"tinyml"`` / ``"algorithm"``）。持久化到 SQLite
        ``processed_articles.group_name``，pending 重水合时一并恢复，飞书
        卡片头部会显示该值。
    header_override : str | None
        卡片头部完整覆盖文本（已含 emoji 前缀，如 ``"🔍 [LLM] AND [agent]"``）。
        设置后，notifier 不再添加 ``📚`` 前缀。仅运行时使用，**不持久化**——
        ``--search`` 模式专用，用来在卡片头部直接展示用户键入的查询。
    """

    doi: Optional[str] = None
    authors: list[str] = field(default_factory=list)
    journal: Optional[str] = None
    pdf_url: Optional[str] = None
    citation_count: Optional[int] = None
    publication_date_str: Optional[str] = None
    paper_json: Optional[dict] = None
    group: Optional[str] = None
    header_override: Optional[str] = None

    @property
    def url_hash(self) -> str:
        """DOI（lowercase）优先；否则用 normalize(title) + 首作者 lastname。"""
        if self.doi:
            key = self.doi.strip().lower()
        else:
            normalized_title = re.sub(r"\W+", " ", (self.title or "")).strip().lower()
            if self.authors:
                first_author_full = self.authors[0].strip()
                parts = first_author_full.split()
                first_author_last = parts[-1].lower() if parts else ""
            else:
                first_author_last = ""
            key = f"{normalized_title}|{first_author_last}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
