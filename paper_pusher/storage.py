"""SQLite 存储模块 - 用于去重和待推送管理（论文版扩展）。

相对 my2 版的差异：
- ``processed_articles`` 额外存 ``doi`` 与 ``paper_json``，以便 pending 重水合。
- ``filter_new_articles`` 同时按 ``url_hash`` 和 ``doi`` 去重。
- ``get_stats()`` 多返回 ``by_query`` 字段（``feed_name`` 中 ``@`` 前的部分聚合）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .article import Article, PaperArticle


class Storage:
    """SQLite 存储管理（论文版）"""

    def __init__(self, db_path: str = "paper_pusher.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url_hash TEXT UNIQUE NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    feed_name TEXT,
                    category TEXT,
                    summary TEXT,
                    content TEXT,
                    doi TEXT,
                    paper_json TEXT,
                    group_name TEXT,
                    pushed INTEGER DEFAULT 0,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_url_hash
                ON processed_articles(url_hash)
            """)
            # ALTER 兜底，便于在旧库基础上加列
            for col_sql in (
                "ALTER TABLE processed_articles ADD COLUMN category TEXT",
                "ALTER TABLE processed_articles ADD COLUMN pushed INTEGER DEFAULT 0",
                "ALTER TABLE processed_articles ADD COLUMN content TEXT",
                "ALTER TABLE processed_articles ADD COLUMN doi TEXT",
                "ALTER TABLE processed_articles ADD COLUMN paper_json TEXT",
                "ALTER TABLE processed_articles ADD COLUMN group_name TEXT",
            ):
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doi
                ON processed_articles(doi)
            """)
            # 通用 kv 存储（用于 group 轮转游标、search digest session 元数据等）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # search digest session items（探索性查询的分页推送队列）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_session_items (
                    position INTEGER PRIMARY KEY,
                    paper_json TEXT NOT NULL,
                    title TEXT,
                    pushed_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_unpushed
                ON search_session_items(pushed_at, position)
            """)
            conn.commit()

    # ----- 通用 kv 存储 -----------------------------------------------------

    def get_kv(self, key: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set_kv(self, key: str, value: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    # ----- search digest session -------------------------------------------
    # 单 session 设计：kv_store['search_session:current'] 存元数据，
    # search_session_items 存按 position 排好序的待推送 paper_json。
    # 用户已确认"只保留最近一个 session"——start 时先 clear。

    _SESSION_META_KEY = "search_session:current"
    _SESSION_SCHEMA_VERSION = 1

    def start_search_session(
        self,
        query: str,
        sort: str,
        digest_size: int,
        articles: List[PaperArticle],
    ) -> None:
        """开新 session：清空旧 session + 写 metadata + 批量插入 items。单事务。"""
        meta = {
            "version": self._SESSION_SCHEMA_VERSION,
            "query": query,
            "sort": sort,
            "digest_size": int(digest_size),
            "total": len(articles),
            "created_at": datetime.now().isoformat(),
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM search_session_items")
            conn.execute(
                "DELETE FROM kv_store WHERE key = ?", (self._SESSION_META_KEY,)
            )
            conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?)",
                (self._SESSION_META_KEY, json.dumps(meta, ensure_ascii=False)),
            )
            rows = []
            for pos, a in enumerate(articles, start=1):
                pj = (json.dumps(a.paper_json, ensure_ascii=False)
                      if isinstance(a, PaperArticle) and a.paper_json else "{}")
                rows.append((pos, pj, a.title or ""))
            conn.executemany(
                "INSERT INTO search_session_items (position, paper_json, title) "
                "VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()

    def get_search_session_meta(self) -> Optional[dict]:
        """读 session 元数据；不存在或 version 不识别 → None。"""
        raw = self.get_kv(self._SESSION_META_KEY)
        if not raw:
            return None
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(meta, dict):
            return None
        if meta.get("version") != self._SESSION_SCHEMA_VERSION:
            return None
        return meta

    def get_search_session_remaining_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM search_session_items WHERE pushed_at IS NULL"
            ).fetchone()[0]

    def get_search_session_next_page(self, limit: int) -> List[dict]:
        """取下一页未推送 items。返回的 dict 含 ``paper_json`` 键，兼容
        ``Searcher.rehydrate``。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT position, paper_json, title
                FROM search_session_items
                WHERE pushed_at IS NULL
                ORDER BY position ASC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_search_session_items_by_positions(
        self, positions: List[int]
    ) -> List[dict]:
        """按 position 取出指定的 items（用户从列表卡上挑出的几条）。

        返回按 ``positions`` 输入顺序排列的 dict 列表，找不到的 position 跳过。
        不修改 ``pushed_at``——列表卡推送状态与单篇 AI 摘要推送状态独立。
        """
        if not positions:
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            qmarks = ",".join("?" for _ in positions)
            cursor = conn.execute(
                f"SELECT position, paper_json, title FROM search_session_items "
                f"WHERE position IN ({qmarks})",
                tuple(positions),
            )
            by_pos = {row['position']: dict(row) for row in cursor.fetchall()}
        return [by_pos[p] for p in positions if p in by_pos]

    def mark_search_session_pushed(self, positions: List[int]) -> None:
        if not positions:
            return
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            qmarks = ",".join("?" for _ in positions)
            conn.execute(
                f"UPDATE search_session_items SET pushed_at = ? "
                f"WHERE position IN ({qmarks})",
                (now, *positions),
            )
            conn.commit()

    def clear_search_session(self) -> None:
        """清掉 session（幂等）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM search_session_items")
            conn.execute(
                "DELETE FROM kv_store WHERE key = ?", (self._SESSION_META_KEY,)
            )
            conn.commit()

    # ----- 去重 ------------------------------------------------------------

    def is_processed(self, article: Article) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM processed_articles WHERE url_hash = ?",
                (article.url_hash,)
            )
            if cursor.fetchone() is not None:
                return True
            # 额外按 DOI 防交叉重复（同一 DOI 在不同库/不同 URL 出现）
            if isinstance(article, PaperArticle) and article.doi:
                cursor = conn.execute(
                    "SELECT 1 FROM processed_articles WHERE doi = ?",
                    (article.doi.strip().lower(),),
                )
                return cursor.fetchone() is not None
            return False

    def filter_new_articles(self, articles: List[Article]) -> List[Article]:
        """跨数据库去重：同一进程内若多个候选指向同一 url_hash/DOI，仅保留首个。"""
        seen_hashes: set[str] = set()
        seen_dois: set[str] = set()
        out: list[Article] = []
        for a in articles:
            if a.url_hash in seen_hashes:
                continue
            doi_key: Optional[str] = None
            if isinstance(a, PaperArticle) and a.doi:
                doi_key = a.doi.strip().lower()
                if doi_key in seen_dois:
                    continue
            if self.is_processed(a):
                continue
            seen_hashes.add(a.url_hash)
            if doi_key:
                seen_dois.add(doi_key)
            out.append(a)
        return out

    # ----- 标记 / 写入 -----------------------------------------------------

    def mark_processed(
        self,
        article: Article,
        summary: Optional[str] = None,
        pushed: int = 1,
    ):
        doi = (article.doi.strip().lower()
               if isinstance(article, PaperArticle) and article.doi else None)
        paper_json = (json.dumps(article.paper_json, ensure_ascii=False)
                      if isinstance(article, PaperArticle) and article.paper_json else None)
        group_name = (article.group
                      if isinstance(article, PaperArticle) else None)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_articles
                (url_hash, url, title, feed_name, category, summary, content,
                 doi, paper_json, group_name, pushed, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article.url_hash,
                article.url,
                article.title,
                article.feed_name,
                article.category,
                summary,
                article.content,
                doi,
                paper_json,
                group_name,
                pushed,
                datetime.now().isoformat(),
            ))
            conn.commit()

    def store_pending(self, article: Article):
        doi = (article.doi.strip().lower()
               if isinstance(article, PaperArticle) and article.doi else None)
        paper_json = (json.dumps(article.paper_json, ensure_ascii=False)
                      if isinstance(article, PaperArticle) and article.paper_json else None)
        group_name = (article.group
                      if isinstance(article, PaperArticle) else None)
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO processed_articles
                    (url_hash, url, title, feed_name, category, content,
                     doi, paper_json, group_name, summary, pushed, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                """, (
                    article.url_hash,
                    article.url,
                    article.title,
                    article.feed_name,
                    article.category,
                    article.content,
                    doi,
                    paper_json,
                    group_name,
                    datetime.now().isoformat(),
                ))
                conn.commit()
            except sqlite3.OperationalError:
                pass

    def get_pending_articles(self, limit: int = 12) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT id, url_hash, url, title, feed_name, category, content,
                       summary, doi, paper_json, group_name, pushed, processed_at
                FROM processed_articles
                WHERE pushed = 0 AND summary IS NULL
                ORDER BY processed_at ASC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def mark_pushed(self, url_hash: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE processed_articles
                SET pushed = 1
                WHERE url_hash = ?
            """, (url_hash,))
            conn.commit()

    # ----- 查询 ------------------------------------------------------------

    def get_recent_articles(self, limit: int = 50) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT url_hash, url, title, feed_name, category, summary,
                       doi, processed_at
                FROM processed_articles
                ORDER BY processed_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM processed_articles"
            ).fetchone()[0]

            by_feed = {
                row[0]: row[1]
                for row in conn.execute("""
                    SELECT feed_name, COUNT(*) as count
                    FROM processed_articles
                    GROUP BY feed_name
                    ORDER BY count DESC
                """).fetchall()
            }

            # by_query：feed_name = "{query}@{db}"，截 "@" 之前的部分聚合
            by_query: dict[str, int] = {}
            for fname, cnt in by_feed.items():
                q = (fname or "").split("@", 1)[0] or "(unknown)"
                by_query[q] = by_query.get(q, 0) + cnt

            pending = conn.execute(
                "SELECT COUNT(*) FROM processed_articles WHERE pushed = 0"
            ).fetchone()[0]

            return {
                'total_articles': total,
                'pending_articles': pending,
                'by_feed': by_feed,
                'by_query': by_query,
            }
