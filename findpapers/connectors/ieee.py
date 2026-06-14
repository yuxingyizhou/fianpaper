"""IEEE Xplore searcher implementation."""

from __future__ import annotations

import contextlib
import datetime
import logging
import re
from collections.abc import Callable
from typing import Any

import requests

from findpapers.connectors.doi_lookup_base import DOILookupConnectorBase
from findpapers.connectors.search_base import SearchConnectorBase
from findpapers.connectors.url_lookup_base import URLLookupConnectorBase
from findpapers.core.author import Author
from findpapers.core.paper import Database, Paper, PaperType
from findpapers.core.query import Query
from findpapers.core.source import Source, SourceType
from findpapers.exceptions import MissingApiKeyError
from findpapers.query.builder import QueryBuilder
from findpapers.query.builders.ieee import IEEEQueryBuilder

logger = logging.getLogger(__name__)

_BASE_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
_PAGE_SIZE = 200  # IEEE max per request
# 200 calls/day limit — use conservative interval
_MIN_REQUEST_INTERVAL = 0.5

# Regex that matches IEEE Xplore landing-page URLs and captures the article number.
# Handles:
#   https://ieeexplore.ieee.org/document/9413133
#   https://ieeexplore.ieee.org/abstract/document/9413133
_IEEE_URL_RE = re.compile(
    r"ieeexplore\.ieee\.org/(?:abstract/)?document/(\d+)",
    re.IGNORECASE,
)

# Mapping from IEEE content_type values to SourceType.
_IEEE_CONTENT_TYPE_MAP: dict[str, SourceType] = {
    "journals": SourceType.JOURNAL,
    "magazines": SourceType.JOURNAL,
    "conferences": SourceType.CONFERENCE,
    "books": SourceType.BOOK,
    "ebooks": SourceType.BOOK,
    "standards": SourceType.OTHER,
    "courses": SourceType.OTHER,
    "early access": SourceType.OTHER,
}

# Mapping from IEEE content_type (lowered) to PaperType.
_IEEE_PAPER_TYPE_MAP: dict[str, PaperType] = {
    "journals": PaperType.ARTICLE,
    "magazines": PaperType.ARTICLE,
    "conferences": PaperType.INPROCEEDINGS,
    "books": PaperType.INBOOK,
    "ebooks": PaperType.INBOOK,
    "standards": PaperType.TECHREPORT,
    "courses": PaperType.MISC,
    "early access": PaperType.ARTICLE,
}


class IEEEConnector(SearchConnectorBase, DOILookupConnectorBase, URLLookupConnectorBase):
    """Connector for the IEEE Xplore database.

    Requires an IEEE API key:
    https://developer.ieee.org/docs/read/Metadata_API_details

    Rate limit: up to 200 requests/day (conservative).
    """

    def __init__(
        self,
        query_builder: IEEEQueryBuilder | None = None,
        api_key: str | None = None,
    ) -> None:
        """Create an IEEE Xplore searcher.

        Parameters
        ----------
        query_builder : IEEEQueryBuilder | None
            Builder used to validate and convert queries.  When ``None`` a
            default :class:`IEEEQueryBuilder` is created automatically.
        api_key : str | None
            IEEE Xplore API key (required for production use).
        """
        super().__init__()
        self._query_builder: IEEEQueryBuilder = query_builder or IEEEQueryBuilder()
        if not api_key or not api_key.strip():
            raise MissingApiKeyError(
                "IEEEConnector requires an api_key. Obtain one at https://developer.ieee.org/"
            )
        self._api_key = api_key

    @property
    def name(self) -> str:
        """Return the database identifier.

        Returns
        -------
        str
            Database name.
        """
        return Database.IEEE.value

    @property
    def query_builder(self) -> QueryBuilder:
        """Return the IEEE query builder.

        Returns
        -------
        QueryBuilder
            The underlying builder instance.
        """
        return self._query_builder

    @property
    def min_request_interval(self) -> float:
        """Return the minimum seconds between HTTP requests.

        Returns
        -------
        float
            Interval in seconds.
        """
        return _MIN_REQUEST_INTERVAL

    def _prepare_params(self, params: dict) -> dict:
        """Inject the IEEE API key into query parameters when configured.

        Parameters
        ----------
        params : dict
            Raw query parameters.

        Returns
        -------
        dict
            Parameters with ``apikey`` added when a key is set.
        """
        if self._api_key:
            return {**params, "apikey": self._api_key}
        return params

    def _prepare_headers(self, headers: dict) -> dict:
        """Inject the IEEE API key header when configured.

        Parameters
        ----------
        headers : dict
            Raw HTTP headers.

        Returns
        -------
        dict
            Headers with ``X-API-Key`` added when a key is set.
        """
        updated = super()._prepare_headers(headers)
        if self._api_key:
            updated["X-API-Key"] = self._api_key
        return updated

    # ------------------------------------------------------------------
    # URL lookup
    # ------------------------------------------------------------------

    @property
    def url_pattern(self) -> re.Pattern[str]:
        """Return the regex matching IEEE Xplore landing-page URLs.

        Returns
        -------
        re.Pattern[str]
            Compiled regex whose first capture group is the article number.
        """
        return _IEEE_URL_RE

    def fetch_paper_by_id(self, paper_id: str) -> Paper | None:
        """Fetch a single IEEE paper by its article number.

        Parameters
        ----------
        paper_id : str
            IEEE Xplore article number (e.g. ``"9413133"``).

        Returns
        -------
        Paper | None
            A populated :class:`~findpapers.core.paper.Paper`, or ``None``
            when the article is not found or the response cannot be parsed.
        """
        params = self._prepare_params({"article_number": paper_id})
        try:
            response = self._get(_BASE_URL, params=params)
            data = response.json()
        except (requests.RequestException, ValueError):
            logger.debug("IEEE: failed to fetch article number %s.", paper_id)
            return None

        articles = data.get("articles") or []
        if not articles:
            logger.debug("IEEE: article number %s not found.", paper_id)
            return None

        return self._parse_paper(articles[0])

    # ------------------------------------------------------------------
    # DOI lookup
    # ------------------------------------------------------------------

    def fetch_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a single paper by its DOI from IEEE Xplore.

        Queries ``GET /api/v1/search/articles?doi={doi}&apikey={key}`` and
        converts the first result into a :class:`~findpapers.core.paper.Paper`.

        Parameters
        ----------
        doi : str
            Bare DOI identifier (e.g. ``"10.1109/5.771073"``).

        Returns
        -------
        Paper | None
            A populated :class:`~findpapers.core.paper.Paper`, or ``None``
            when no API key is configured, the DOI is not found in IEEE, or
            the response cannot be parsed.
        """
        # Note: max_records=1 is intentionally omitted - the IEEE Xplore API
        # returns total_records=1 but an empty articles list when max_records=1
        # is passed alongside a doi filter (probably an API bug).
        params = self._prepare_params({"doi": doi})
        try:
            response = self._get(_BASE_URL, params=params)
            data = response.json()
        except (requests.RequestException, ValueError):
            logger.debug("IEEE: failed to fetch DOI %s.", doi)
            return None

        articles = data.get("articles") or []
        if not articles:
            logger.debug("IEEE: DOI %s not found.", doi)
            return None

        return self._parse_paper(articles[0])

    def _parse_paper(self, item: dict[str, Any]) -> Paper | None:
        """Parse a single IEEE API result item.

        Parameters
        ----------
        item : dict
            Article metadata dictionary from IEEE JSON response.

        Returns
        -------
        Paper | None
            Parsed paper or ``None`` when required fields are missing.
        """
        title = (item.get("title") or "").strip()
        if not title:
            return None

        abstract = (item.get("abstract") or "").strip()

        # Authors
        authors: list[Author] = []
        for author_entry in item.get("authors", {}).get("authors", []):
            full_name = (author_entry.get("full_name") or "").strip()
            if full_name:
                affiliation = (author_entry.get("affiliation") or "").strip() or None
                authors.append(Author(name=full_name, affiliation=affiliation))

        # Publication date
        pub_date: datetime.date | None = None
        pub_year = item.get("publication_year")
        if pub_year:
            with contextlib.suppress(ValueError, TypeError):
                pub_date = datetime.date(int(pub_year), 1, 1)

        # DOI / URL
        doi: str | None = (item.get("doi") or "").strip() or None
        url: str | None = (item.get("html_url") or item.get("pdf_url") or "").strip() or None
        pdf_url: str | None = (item.get("pdf_url") or "").strip() or None

        # Keywords and subjects from index_terms
        # ieee_terms are INSPEC controlled vocabulary → subjects
        # author_terms and mesh_terms → keywords
        keywords: set[str] = set()
        subjects: set[str] = set()
        index_terms = item.get("index_terms") or {}
        for kw_el in index_terms.get("ieee_terms", {}).get("terms", []):
            term = kw_el.strip()
            if term:
                subjects.add(term)
        for kw_group in ["author_terms", "mesh_terms"]:
            for kw_el in index_terms.get(kw_group, {}).get("terms", []):
                kw = kw_el.strip()
                if kw:
                    keywords.add(kw)

        # Citations
        citations: int | None = None
        citation_count = item.get("citing_paper_count")
        if citation_count is not None:
            with contextlib.suppress(ValueError, TypeError):
                citations = int(citation_count)

        # Source
        source_title = (item.get("publication_title") or "").strip()
        raw_content_type = (item.get("content_type") or "").strip().lower()
        source: Source | None = None
        if source_title:
            issn = (item.get("issn") or "").strip() or None
            isbn = (item.get("isbn") or "").strip() or None
            publisher = (item.get("publisher") or "").strip() or None
            # Map content_type to SourceType.
            source_type = _IEEE_CONTENT_TYPE_MAP.get(raw_content_type)
            source = Source(
                title=source_title,
                issn=issn,
                isbn=isbn,
                publisher=publisher,
                source_type=source_type,
            )

        # Infer paper_type from content_type.
        paper_type = _IEEE_PAPER_TYPE_MAP.get(raw_content_type)

        # Pages
        start_page = item.get("start_page") or ""
        end_page = item.get("end_page") or ""
        pages: str | None = None
        if start_page and end_page:
            pages = f"{start_page}-{end_page}"
        elif start_page:
            pages = str(start_page)

        # Open access — "OPEN_ACCESS" → True, "LOCKED" → False, else None.
        raw_access_type = (item.get("access_type") or "").strip().upper()
        is_open_access: bool | None = None
        if raw_access_type == "OPEN_ACCESS":
            is_open_access = True
        elif raw_access_type == "LOCKED":
            is_open_access = False

        try:
            paper = Paper(
                title=title,
                abstract=abstract,
                authors=authors,
                source=source,
                publication_date=pub_date,
                url=url,
                pdf_url=pdf_url,
                doi=doi,
                citations=citations,
                keywords=keywords if keywords else None,
                subjects=subjects,
                page_range=pages,
                databases={self.name},
                paper_type=paper_type,
                is_open_access=is_open_access,
            )
        except ValueError:
            return None

        return paper

    def _fetch_papers(
        self,
        query: Query,
        max_papers: int | None,
        progress_callback: Callable[[int, int | None], None] | None,
        since: datetime.date | None = None,
        until: datetime.date | None = None,
    ) -> list[Paper]:
        """Fetch papers from IEEE Xplore with pagination.

        Parameters
        ----------
        query : Query
            Validated query object.
        max_papers : int | None
            Maximum papers to retrieve.
        progress_callback : Callable[[int, int | None], None] | None
            Progress callback.
        since : datetime.date | None
            Only return papers published on or after this date (year granularity).
        until : datetime.date | None
            Only return papers published on or before this date (year granularity).

        Returns
        -------
        list[Paper]
            Retrieved papers.
        """
        ieee_params = self._query_builder.convert_query(query)

        # IEEE Xplore supports year-level date filtering via start_year / end_year.
        if since is not None:
            ieee_params["start_year"] = str(since.year)
        if until is not None:
            ieee_params["end_year"] = str(until.year)
        papers: list[Paper] = []
        processed = 0
        offset = 1  # IEEE uses 1-based pagination
        total: int | None = None

        while True:
            remaining = (max_papers - len(papers)) if max_papers is not None else _PAGE_SIZE
            page_size = min(_PAGE_SIZE, remaining)

            # NOTE: The IEEE API only supports sort_field values:
            # article_number, article_title, publication_title.
            # None of these sort by date or relevance.  Omitting
            # sort_field/sort_order lets the API use its default
            # relevance-based ordering, which yields a better mix of
            # recent and older papers.  Additionally, sort_order
            # (asc/desc) is silently ignored by the API as of 2026.
            params = {
                **ieee_params,
                "start_record": offset,
                "max_records": page_size,
            }

            try:
                response = self._get(_BASE_URL, params)
            except requests.RequestException as exc:
                logger.warning("IEEE request failed (offset=%d): %s", offset, exc)
                logger.debug("IEEE request exception details:", exc_info=True)
                break

            data = response.json()
            total = data.get("total_records")

            articles = data.get("articles", [])
            if not articles:
                break

            for item in articles:
                paper = self._parse_paper(item)
                if paper is not None:
                    papers.append(paper)

            processed += len(articles)
            if progress_callback is not None:
                progress_callback(processed, total)

            if max_papers is not None and len(papers) >= max_papers:
                break

            if len(articles) < page_size:
                break

            offset += len(articles)

        # Ensure the progress bar is updated even when the loop exits early
        # (e.g. on the first request returning no articles or a request error),
        # so the bar never stays frozen at its initial 0-paper state.
        if progress_callback is not None:
            progress_callback(processed, total)

        return papers[:max_papers] if max_papers is not None else papers
