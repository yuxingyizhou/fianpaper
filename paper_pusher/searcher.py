"""Searcher：封装 findpapers.Engine，把 Paper 转成 PaperArticle。"""

from __future__ import annotations

import datetime
from typing import List, Optional

from findpapers import Engine
from findpapers.core.paper import Paper

from .article import PaperArticle
from .log_utils import get_logger

logger = get_logger("paper_pusher.searcher")


# config.search_databases 的 key → Engine 构造参数名
_SEARCH_KEY_PARAM_MAP = {
    "ieee": "ieee_api_key",
    "scopus": "scopus_api_key",
    "pubmed": "pubmed_api_key",
    "openalex": "openalex_api_key",
    "semantic_scholar": "semantic_scholar_api_key",
    "wos": "wos_api_key",
}

# 内置的"需要 API key 才可用"的搜索库（arxiv 不需要）
_KEY_REQUIRED_FOR_SEARCH = {"ieee", "scopus", "wos"}


class Searcher:
    """封装 findpapers.Engine 的多查询循环。"""

    def __init__(self, config: dict):
        self._config = config
        search_dbs_cfg: dict = config.get("search_databases", {}) or {}
        enrichment_dbs_cfg: dict = config.get("enrichment_databases", {}) or {}
        schedule_cfg: dict = config.get("schedule", {}) or {}

        # 1) 收集 Engine 构造参数
        engine_kwargs: dict = {}
        for db_name, db_cfg in search_dbs_cfg.items():
            db_cfg = db_cfg or {}
            if not db_cfg.get("enabled"):
                continue
            param = _SEARCH_KEY_PARAM_MAP.get(db_name)
            api_key = (db_cfg.get("api_key") or "").strip() if db_cfg.get("api_key") else None
            if param and api_key:
                engine_kwargs[param] = api_key
            email = (db_cfg.get("email") or "").strip() if db_cfg.get("email") else None
            if email and "email" not in engine_kwargs:
                engine_kwargs["email"] = email
        # 全局 email（如果配置在 top-level）
        if config.get("email") and "email" not in engine_kwargs:
            engine_kwargs["email"] = config["email"]
        if config.get("proxy"):
            engine_kwargs["proxy"] = config["proxy"]
        if "ssl_verify" in config:
            engine_kwargs["ssl_verify"] = bool(config["ssl_verify"])

        self._engine = Engine(**engine_kwargs)

        # 2) 预过滤启用的搜索库；对于 _KEY_REQUIRED_FOR_SEARCH 中的库，
        #    若缺 key 则跳过（避免 Engine 内部一堆 "Skipping..." 日志）
        self._enabled_search_dbs: list[str] = []
        for db_name, db_cfg in search_dbs_cfg.items():
            db_cfg = db_cfg or {}
            if not db_cfg.get("enabled"):
                continue
            if db_name in _KEY_REQUIRED_FOR_SEARCH:
                api_key = (db_cfg.get("api_key") or "").strip() if db_cfg.get("api_key") else None
                if not api_key:
                    logger.info("跳过 %s（需要 API key 但未提供）", db_name)
                    continue
            self._enabled_search_dbs.append(db_name)

        # 3) 启用的 enrichment 库
        self._enabled_enrichment_dbs: list[str] = [
            name for name, cfg in enrichment_dbs_cfg.items()
            if (cfg or {}).get("enabled")
        ]

        self._max_per_db: Optional[int] = schedule_cfg.get("max_papers_per_query_db")
        self._verbose: bool = bool(schedule_cfg.get("verbose", False))

        logger.info(
            "Searcher 初始化：搜索库=%s, enrichment=%s, max_per_db=%s",
            self._enabled_search_dbs, self._enabled_enrichment_dbs, self._max_per_db,
        )

    @property
    def enabled_search_databases(self) -> List[str]:
        return list(self._enabled_search_dbs)

    def search_query(
        self,
        query: str,
        category: str,
        since: Optional[datetime.date] = None,
        until: Optional[datetime.date] = None,
        label: Optional[str] = None,
        group: Optional[str] = None,
    ) -> List[PaperArticle]:
        """执行单个查询，返回 PaperArticle 列表。

        Parameters
        ----------
        query : str
            findpapers DSL 查询。
        category : str
            分类标签（透传到 Article.category，通常等于 config.field）。
        since : datetime.date | None
            发表日期下限。
        until : datetime.date | None
            发表日期上限。
        label : str | None
            该查询的用途标签（如 "keyword: agent" / "author: Yann LeCun"），
            会写入 ``feed_name`` 以便日志/统计区分维度。
        group : str | None
            配额分组键，透传到 ``PaperArticle.group``，供 ``allocate_quota`` 按组分配。
        """
        if not self._enabled_search_dbs:
            logger.warning("没有可用的搜索数据库，跳过查询：%s", query)
            return []
        try:
            result = self._engine.search(
                query,
                databases=self._enabled_search_dbs,
                max_papers_per_database=self._max_per_db,
                since=since,
                until=until,
                enrichment_databases=self._enabled_enrichment_dbs or None,
                show_progress=False,
                verbose=self._verbose,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("搜索失败 query=%r：%s", query, exc)
            return []

        articles = [
            self._paper_to_article(p, query, category, label, group) for p in result.papers
        ]
        logger.info(
            "查询 %s → %d 篇", (label or query), len(articles),
        )
        return articles

    # ------------------------------------------------------------------
    # 内部转换
    # ------------------------------------------------------------------

    @staticmethod
    def _paper_to_article(
        paper: Paper,
        query: str,
        category: str,
        label: Optional[str] = None,
        group: Optional[str] = None,
    ) -> PaperArticle:
        authors = [a.name for a in (paper.authors or []) if a and getattr(a, "name", None)]
        journal = paper.source.title if paper.source is not None else None
        # 去重哈希要稳定，feed_name 不参与去重；这里给一个可读字符串。
        # label 存在则用 label，否则回退到 query。
        databases = sorted(paper.databases) if paper.databases else []
        head = label or query
        feed_name = f"{head}@{','.join(databases) or 'unknown'}"

        url = paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else "")
        abstract = paper.abstract or ""
        # 与 my2 一致：截断超长内容，省 LLM token
        content = abstract.strip()
        if len(content) > 3000:
            content = content[:3000].rstrip() + "..."

        published: Optional[datetime.datetime] = None
        publication_date_str: Optional[str] = None
        if paper.publication_date is not None:
            publication_date_str = paper.publication_date.isoformat()
            published = datetime.datetime.combine(
                paper.publication_date, datetime.time.min
            )

        return PaperArticle(
            title=paper.title,
            url=url,
            content=content,
            published=published,
            feed_name=feed_name,
            category=category or "",
            doi=paper.doi,
            authors=authors,
            journal=journal,
            pdf_url=paper.pdf_url,
            citation_count=paper.citations,
            publication_date_str=publication_date_str,
            paper_json=paper.to_dict(),
            group=group,
        )

    @staticmethod
    def rehydrate(record: dict) -> PaperArticle:
        """从 storage.get_pending_articles 返回的 dict 重建 PaperArticle。

        如果 ``paper_json`` 存在，用它还原全部字段；否则仅根据基础字段回填。
        """
        import json

        paper_dict: Optional[dict] = None
        raw_pj = record.get("paper_json")
        if raw_pj:
            try:
                paper_dict = json.loads(raw_pj)
            except (json.JSONDecodeError, TypeError):
                paper_dict = None

        if paper_dict:
            paper = Paper.from_dict(paper_dict)
            return Searcher._paper_to_article(
                paper,
                query=(record.get("feed_name") or "").split("@", 1)[0],
                category=record.get("category") or "",
                group=record.get("group_name"),
            )

        # 退化：仅靠 SQLite 行字段构造
        return PaperArticle(
            title=record.get("title") or "",
            url=record.get("url") or "",
            content=record.get("content") or "",
            published=None,
            feed_name=record.get("feed_name") or "",
            category=record.get("category") or "",
            doi=record.get("doi"),
            group=record.get("group_name"),
        )
