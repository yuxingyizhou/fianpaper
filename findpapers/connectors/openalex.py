"""OpenAlex searcher implementation."""

from __future__ import annotations

import contextlib
import datetime
import logging
import re
from collections.abc import Callable
from typing import Any

import requests

from findpapers.connectors.citation_base import CitationConnectorBase
from findpapers.connectors.doi_lookup_base import DOILookupConnectorBase
from findpapers.connectors.search_base import SearchConnectorBase
from findpapers.connectors.url_lookup_base import URLLookupConnectorBase
from findpapers.core.author import Author
from findpapers.core.paper import Database, Paper, PaperType
from findpapers.core.query import Query
from findpapers.core.source import Source, SourceType
from findpapers.query.builder import QueryBuilder
from findpapers.query.builders.openalex import OpenAlexQueryBuilder
from findpapers.utils.normalization import normalize_doi, normalize_language

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openalex.org/works"
_PAGE_SIZE = 200  # OpenAlex max per_page
# Polite pool: ~10 req/s with email in User-Agent → use 0.1s interval
_MIN_REQUEST_INTERVAL = 0.15

# Regex that matches OpenAlex work landing-page URLs and captures the work ID.
# Handles:
#   https://openalex.org/W2741809807
#   https://openalex.org/works/W2741809807
_OPENALEX_URL_RE = re.compile(
    r"openalex\.org/(?:works/)?(W\d+)",
    re.IGNORECASE,
)

# Mapping from OpenAlex source.type values to SourceType.
_OPENALEX_SOURCE_TYPE_MAP: dict[str, SourceType] = {
    "journal": SourceType.JOURNAL,
    "conference": SourceType.CONFERENCE,
    "repository": SourceType.REPOSITORY,
    "book series": SourceType.BOOK,
    "ebook platform": SourceType.BOOK,
    "metadata": SourceType.OTHER,
    "other": SourceType.OTHER,
}


# Maximum number of OpenAlex IDs to fetch in a single filter request
# (the API supports pipe-separated ID filters).
_REFERENCES_BATCH_SIZE = 50

# Mapping from OpenAlex work.type (lowered) to PaperType.
_OPENALEX_PAPER_TYPE_MAP: dict[str, PaperType] = {
    "article": PaperType.ARTICLE,
    "review": PaperType.ARTICLE,
    "letter": PaperType.ARTICLE,
    "editorial": PaperType.ARTICLE,
    "erratum": PaperType.ARTICLE,
    "book-chapter": PaperType.INBOOK,
    "book": PaperType.BOOK,
    "dissertation": PaperType.PHDTHESIS,
    "preprint": PaperType.UNPUBLISHED,
    "report": PaperType.TECHREPORT,
    "standard": PaperType.TECHREPORT,
    "peer-review": PaperType.MISC,
    "other": PaperType.MISC,
    "paratext": PaperType.MISC,
    "reference-entry": PaperType.INCOLLECTION,
    "dataset": PaperType.MISC,
    "component": PaperType.MISC,
    "grant": PaperType.MISC,
    "supplementary-materials": PaperType.MISC,
    "libguides": PaperType.MISC,
}


class OpenAlexConnector(
    SearchConnectorBase, CitationConnectorBase, DOILookupConnectorBase, URLLookupConnectorBase
):
    """Connector for the OpenAlex open catalog of academic works.

    https://docs.openalex.org/how-to-use-the-api

    Rate limit: max 100 req/s for all users.
    Without an API key the daily budget is $0.01/day (~10 requests,
    recommended for testing and demos only).  With a free API key the budget
    is $10/day (~10,000 requests/day). Singleton requests are free.
    """

    def __init__(
        self,
        query_builder: OpenAlexQueryBuilder | None = None,
        api_key: str | None = None,
        email: str | None = None,
    ) -> None:
        """Create an OpenAlex searcher.

        Parameters
        ----------
        query_builder : OpenAlexQueryBuilder | None
            Builder used to validate and convert queries.  When ``None`` a
            default :class:`OpenAlexQueryBuilder` is created automatically.
        api_key : str | None
            OpenAlex API key (optional but highly recommended; free keys
            available at https://openalex.org/settings/api).  Without a key
            the daily budget is $0.01/day, suitable for testing only.
        email : str | None
            Contact email for the polite pool (recommended by OpenAlex).
        """
        super().__init__()
        self._query_builder: OpenAlexQueryBuilder = query_builder or OpenAlexQueryBuilder()
        self._api_key = api_key
        self._email = email

        if not api_key:
            logger.warning(
                "No API key provided for OpenAlex. "
                "The daily budget without a key is $0.01/day (~10 requests), "
                "suitable for testing only. Get a free key at "
                "https://openalex.org/settings/api to increase your quota."
            )

    @property
    def name(self) -> str:
        """Return the database identifier.

        Returns
        -------
        str
            Database name.
        """
        return Database.OPENALEX.value

    @property
    def query_builder(self) -> QueryBuilder:
        """Return the OpenAlex query builder.

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
        """Inject the OpenAlex API key into query parameters when configured.

        Parameters
        ----------
        params : dict
            Raw query parameters.

        Returns
        -------
        dict
            Parameters with ``api_key`` added when a key is set.
        """
        if self._api_key:
            return {**params, "api_key": self._api_key}
        return params

    # ------------------------------------------------------------------
    # URL lookup
    # ------------------------------------------------------------------

    @property
    def url_pattern(self) -> re.Pattern[str]:
        """Return the regex matching OpenAlex work landing-page URLs.

        Returns
        -------
        re.Pattern[str]
            Compiled regex whose first capture group is the OpenAlex work ID.
        """
        return _OPENALEX_URL_RE

    def fetch_paper_by_id(self, paper_id: str) -> Paper | None:
        """Fetch a single paper by its OpenAlex work ID.

        Parameters
        ----------
        paper_id : str
            OpenAlex work ID (e.g. ``"W2741809807"``).

        Returns
        -------
        Paper | None
            A populated :class:`~findpapers.core.paper.Paper`, or ``None``
            when the work is not found or the response cannot be parsed.
        """
        url = f"{_BASE_URL}/{paper_id}"
        params: dict[str, Any] = {
            "select": (
                "id,doi,title,display_name,publication_date,authorships,"
                "abstract_inverted_index,cited_by_count,open_access,locations,"
                "primary_location,concepts,keywords,type,biblio,primary_topic,language,"
                "is_retracted,funders"
            ),
        }
        params = self._prepare_params(params)
        try:
            response = self._get(url, params=params)
            data = response.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.debug("OpenAlex: work ID %s not found (404).", paper_id)
                return None
            logger.debug("OpenAlex: HTTP error fetching work ID %s: %s", paper_id, exc)
            return None
        except (requests.RequestException, ValueError):
            logger.debug("OpenAlex: failed to fetch work ID %s.", paper_id)
            return None

        return self._parse_paper(data)

    # ------------------------------------------------------------------
    # DOI lookup
    # ------------------------------------------------------------------

    def fetch_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a single paper by its DOI from OpenAlex.

        Queries ``GET /works/doi:{doi}`` and converts the response into a
        :class:`~findpapers.core.paper.Paper`.

        Parameters
        ----------
        doi : str
            Bare DOI identifier (e.g. ``"10.1038/nature12373"``).

        Returns
        -------
        Paper | None
            A populated :class:`~findpapers.core.paper.Paper`, or ``None``
            when the DOI is not found or the response cannot be parsed.
        """
        url = f"{_BASE_URL}/doi:{doi}"
        params: dict[str, Any] = {
            "select": (
                "id,doi,title,display_name,publication_date,authorships,"
                "abstract_inverted_index,cited_by_count,open_access,locations,"
                "primary_location,concepts,keywords,type,biblio,primary_topic,language,"
                "is_retracted,funders"
            ),
        }
        params = self._prepare_params(params)
        try:
            response = self._get(url, params=params)
            data = response.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.debug("OpenAlex: DOI %s not found (404).", doi)
                return None
            logger.debug("OpenAlex: HTTP error fetching DOI %s: %s", doi, exc)
            return None
        except (requests.RequestException, ValueError):
            logger.debug("OpenAlex: failed to fetch DOI %s.", doi)
            return None

        return self._parse_paper(data)

    # ------------------------------------------------------------------
    # Citation methods (CitationConnectorBase)
    # ------------------------------------------------------------------

    def get_expected_counts(self, paper: Paper) -> tuple[int | None, int | None]:
        """Return expected citation and reference counts for *paper*.

        Uses ``paper.citations`` for the citation count (already populated
        during search) and fetches the ``referenced_works`` list from
        OpenAlex to obtain the reference count.

        Parameters
        ----------
        paper : Paper
            The paper whose counts are requested.

        Returns
        -------
        tuple[int | None, int | None]
            ``(citation_count, reference_count)``.  Either may be ``None``
            when the information is unavailable.
        """
        if not paper.doi:
            return None, None

        cit_count: int | None = paper.citations

        ref_count: int | None = None
        url = f"{_BASE_URL}/doi:{paper.doi}"
        try:
            response = self._get(url, params={"select": "referenced_works"})
            data = response.json()
            referenced = data.get("referenced_works") or []
            ref_count = len(referenced)
        except (requests.RequestException, ValueError):
            pass

        return cit_count, ref_count

    def _resolve_openalex_id(self, paper: Paper) -> str | None:
        """Resolve a paper's OpenAlex ID via the DOI.

        Queries ``GET /works/doi:{doi}`` which returns the full work record
        whose ``id`` field is the canonical OpenAlex ID.

        Parameters
        ----------
        paper : Paper
            Paper with a DOI.

        Returns
        -------
        str | None
            The OpenAlex ID (e.g. ``"https://openalex.org/W123456"``), or
            ``None`` when the DOI cannot be resolved.
        """
        if not paper.doi:
            return None

        url = f"{_BASE_URL}/doi:{paper.doi}"
        try:
            response = self._get(url, params={"select": "id"})
            data = response.json()
            return (data.get("id") or "").strip() or None
        except (requests.RequestException, ValueError):
            logger.debug("Failed to resolve OpenAlex ID for DOI %s.", paper.doi)
            return None

    def _fetch_works_by_ids(
        self,
        openalex_ids: list[str],
        progress_callback: Callable[[int], None] | None = None,
    ) -> list[Paper]:
        """Fetch full work records for a list of OpenAlex IDs.

        Uses the pipe-separated ID filter (``openalex:{id1}|{id2}|...``) to
        batch-fetch works in chunks of :data:`_REFERENCES_BATCH_SIZE`.

        Parameters
        ----------
        openalex_ids : list[str]
            OpenAlex work IDs (full URLs like
            ``https://openalex.org/W123``).
        progress_callback : Callable[[int], None] | None
            Optional callback invoked after each batch with the number of
            papers fetched in that batch.

        Returns
        -------
        list[Paper]
            Parsed papers.
        """
        papers: list[Paper] = []
        if not openalex_ids:
            return papers

        for start in range(0, len(openalex_ids), _REFERENCES_BATCH_SIZE):
            batch = openalex_ids[start : start + _REFERENCES_BATCH_SIZE]
            id_filter = "|".join(batch)
            params: dict[str, Any] = {
                "filter": f"openalex:{id_filter}",
                "per-page": _PAGE_SIZE,
                "select": (
                    "id,doi,title,display_name,publication_date,authorships,"
                    "abstract_inverted_index,cited_by_count,open_access,locations,"
                    "primary_location,concepts,keywords,type,biblio,primary_topic,language,"
                    "is_retracted,funders"
                ),
            }
            try:
                response = self._get(_BASE_URL, params)
                data = response.json()
                batch_papers: list[Paper] = []
                for work in data.get("results") or []:
                    paper = self._parse_paper(work)
                    if paper is not None:
                        batch_papers.append(paper)
                papers.extend(batch_papers)
                if progress_callback is not None and batch_papers:
                    progress_callback(len(batch_papers))
            except (requests.RequestException, ValueError, KeyError, TypeError):
                logger.debug(
                    "Failed to fetch OpenAlex works batch (offset=%d, count=%d).",
                    start,
                    len(batch),
                )
        return papers

    def _fetch_cited_by_page(
        self,
        openalex_id: str,
        cursor: str,
    ) -> tuple[list[Paper], str | None]:
        """Fetch one page of papers that cite the given work.

        Parameters
        ----------
        openalex_id : str
            OpenAlex ID of the cited work.
        cursor : str
            Pagination cursor (``"*"`` for the first page).

        Returns
        -------
        tuple[list[Paper], str | None]
            Papers from this page and the next cursor (``None`` when done).
        """
        params: dict[str, Any] = {
            "filter": f"cites:{openalex_id}",
            "per-page": _PAGE_SIZE,
            "cursor": cursor,
            "select": (
                "id,doi,title,display_name,publication_date,authorships,"
                "abstract_inverted_index,cited_by_count,open_access,locations,"
                "primary_location,concepts,keywords,type,biblio,primary_topic,language,"
                "is_retracted,funders"
            ),
        }
        try:
            response = self._get(_BASE_URL, params)
        except requests.RequestException:
            logger.debug(
                "Failed to fetch cited-by page for %s (cursor=%s).",
                openalex_id,
                cursor,
            )
            return [], None

        data = response.json()
        papers: list[Paper] = []
        for work in data.get("results") or []:
            paper = self._parse_paper(work)
            if paper is not None:
                papers.append(paper)

        meta = data.get("meta") or {}
        next_cursor = meta.get("next_cursor")
        results = data.get("results") or []
        if not next_cursor or len(results) < _PAGE_SIZE:
            next_cursor = None

        return papers, next_cursor

    def fetch_references(
        self,
        paper: Paper,
        progress_callback: Callable[[int], None] | None = None,
    ) -> list[Paper]:
        """Return papers cited *by* the given paper (backward snowballing).

        Queries OpenAlex for the full work record (which includes the
        ``referenced_works`` field) and then batch-fetches the referenced
        works to build full :class:`Paper` objects.

        Parameters
        ----------
        paper : Paper
            The paper whose references should be fetched.  Must have a DOI.
        progress_callback : Callable[[int], None] | None
            Optional callback for per-page progress reporting.

        Returns
        -------
        list[Paper]
            Papers referenced by *paper*, or an empty list on failure.
        """
        if not paper.doi:
            return []

        # Fetch the full work record to get referenced_works.
        url = f"{_BASE_URL}/doi:{paper.doi}"
        try:
            response = self._get(url, params={"select": "id,referenced_works"})
            data = response.json()
        except (requests.RequestException, ValueError):
            logger.debug("Failed to fetch OpenAlex work for DOI %s.", paper.doi)
            return []

        referenced_ids = data.get("referenced_works") or []
        if not referenced_ids:
            return []

        logger.debug(
            "OpenAlex: fetching %d references for DOI %s.",
            len(referenced_ids),
            paper.doi,
        )
        return self._fetch_works_by_ids(referenced_ids, progress_callback=progress_callback)

    def fetch_cited_by(
        self,
        paper: Paper,
        progress_callback: Callable[[int], None] | None = None,
    ) -> list[Paper]:
        """Return papers that cite the given paper (forward snowballing).

        Uses the OpenAlex ``cites`` filter to paginate through all papers
        that cite the given work.

        Parameters
        ----------
        paper : Paper
            The paper whose citing papers should be fetched.  Must have a DOI.
        progress_callback : Callable[[int], None] | None
            Optional callback for per-page progress reporting.

        Returns
        -------
        list[Paper]
            Papers that cite *paper*, or an empty list on failure.
        """
        if not paper.doi:
            return []

        openalex_id = self._resolve_openalex_id(paper)
        if not openalex_id:
            return []

        logger.debug("OpenAlex: fetching cited-by for %s.", openalex_id)

        all_papers: list[Paper] = []
        cursor: str | None = "*"

        while cursor is not None:
            page_papers, cursor = self._fetch_cited_by_page(openalex_id, cursor)
            all_papers.extend(page_papers)
            if progress_callback is not None and page_papers:
                progress_callback(len(page_papers))

        return all_papers

    def _parse_paper(self, work: dict[str, Any]) -> Paper | None:
        """Parse a single OpenAlex work object into a :class:`Paper`.

        Parameters
        ----------
        work : dict
            OpenAlex work metadata dictionary.

        Returns
        -------
        Paper | None
            Parsed paper or ``None`` when required fields are missing.
        """
        title = (work.get("title") or work.get("display_name") or "").strip()
        if not title:
            return None

        # Abstract — stored as inverted index in OpenAlex
        abstract = ""
        inverted_index = work.get("abstract_inverted_index")
        if inverted_index:
            abstract = _reconstruct_abstract(inverted_index)

        # Authors
        authors: list[Author] = []
        for authorship in work.get("authorships", []):
            author_info = authorship.get("author") or {}
            name = (author_info.get("display_name") or "").strip()
            if name:
                # OpenAlex provides institutions per authorship entry.
                institutions = authorship.get("institutions") or []
                affiliation_parts = [
                    (inst.get("display_name") or "").strip()
                    for inst in institutions
                    if isinstance(inst, dict) and (inst.get("display_name") or "").strip()
                ]
                affiliation = "; ".join(affiliation_parts) if affiliation_parts else None
                authors.append(Author(name=name, affiliation=affiliation))

        # Publication date
        pub_date: datetime.date | None = None
        _pub_date_str = (work.get("publication_date") or "").strip()
        if _pub_date_str:
            with contextlib.suppress(ValueError):
                pub_date = datetime.date.fromisoformat(_pub_date_str[:10])

        # DOI / URL
        doi_raw: str | None = (work.get("doi") or "").strip() or None
        doi = normalize_doi(doi_raw) if doi_raw else None

        url: str | None = None
        open_access = work.get("open_access") or {}
        url = (open_access.get("oa_url") or "").strip() or None
        if not url:
            primary = work.get("primary_location") or {}
            url = (primary.get("landing_page_url") or "").strip() or None

        pdf_url: str | None = None
        for loc in work.get("locations", []):
            if isinstance(loc, dict) and loc.get("pdf_url"):
                pdf_url = loc["pdf_url"]
                break

        # Citations
        citations: int | None = work.get("cited_by_count")

        # Keywords / concepts
        keywords: set[str] = set()
        for concept in work.get("concepts", []):
            kw = (concept.get("display_name") or "").strip()
            if kw:
                keywords.add(kw)
        for kw_entry in work.get("keywords", []):
            if isinstance(kw_entry, str):
                kw = kw_entry.strip()
            elif isinstance(kw_entry, dict):
                kw = (kw_entry.get("display_name") or "").strip()
            else:
                kw = ""
            if kw:
                keywords.add(kw)

        # Source – prefer a journal or conference venue over a repository.
        # OpenAlex ``source.type`` may be "journal", "conference", "repository",
        # "ebook platform", or "book series".  Repository sources (e.g.
        # institutional repos, Zenodo) should not be used as the paper's
        # publication source since they represent the *hosting location*, not
        # the actual venue.
        source: Source | None = None
        source_data = _find_best_source(work)
        if source_data:
            pub_title = (source_data.get("display_name") or "").strip()
            if pub_title:
                issn_list = source_data.get("issn_l") or source_data.get("issn") or []
                issn = (
                    issn_list[0]
                    if isinstance(issn_list, list) and issn_list
                    else str(issn_list)
                    if issn_list
                    else None
                )
                raw_src_type = (source_data.get("type") or "").strip().lower()
                source_type = _OPENALEX_SOURCE_TYPE_MAP.get(raw_src_type)
                source = Source(title=pub_title, issn=issn, source_type=source_type)

        # When no formal source was found and the work is a preprint,
        # create a repository-type source from the repository location.
        if source is None:
            repo_source = _find_repository_source(work)
            if repo_source:
                repo_name = (repo_source.get("display_name") or "").strip()
                if repo_name:
                    source = Source(title=repo_name, source_type=SourceType.REPOSITORY)

        # Pages from biblio
        pages: str | None = None
        biblio = work.get("biblio") or {}
        first_page = (biblio.get("first_page") or "").strip()
        last_page = (biblio.get("last_page") or "").strip()
        if first_page and last_page:
            pages = f"{first_page}\u2013{last_page}"
        elif first_page:
            pages = first_page

        # Infer paper_type from the work-level "type" field.
        raw_work_type = (work.get("type") or "").strip().lower()
        paper_type = _OPENALEX_PAPER_TYPE_MAP.get(raw_work_type)

        # OpenAlex classifies conference papers as work.type "article".
        # When the source is a conference venue, promote to INPROCEEDINGS.
        if (
            paper_type is PaperType.ARTICLE
            and source is not None
            and source.source_type is SourceType.CONFERENCE
        ):
            paper_type = PaperType.INPROCEEDINGS

        # Extract fields_of_study and subjects from primary_topic.
        fields_of_study: set[str] = set()
        subjects: set[str] = set()
        primary_topic = work.get("primary_topic") or {}
        if primary_topic:
            # field → fields_of_study (broad area)
            field_info = primary_topic.get("field") or {}
            field_name = (field_info.get("display_name") or "").strip()
            if field_name:
                fields_of_study.add(field_name)
            # domain can serve as an additional broad classifier
            domain_info = primary_topic.get("domain") or {}
            domain_name = (domain_info.get("display_name") or "").strip()
            if domain_name and domain_name != field_name:
                fields_of_study.add(domain_name)
            # subfield → subjects (specific)
            subfield_info = primary_topic.get("subfield") or {}
            subfield_name = (subfield_info.get("display_name") or "").strip()
            if subfield_name:
                subjects.add(subfield_name)
            # topic display_name → subjects (even more specific)
            topic_name = (primary_topic.get("display_name") or "").strip()
            if topic_name:
                subjects.add(topic_name)

        # Funders — extracted from the funders list
        funders: set[str] = set()
        for funder in work.get("funders") or []:
            if isinstance(funder, dict):
                funder_name = (funder.get("display_name") or "").strip()
                if funder_name:
                    funders.add(funder_name)

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
                page_range=pages,
                databases={self.name},
                paper_type=paper_type,
                fields_of_study=fields_of_study if fields_of_study else None,
                subjects=subjects if subjects else None,
                language=normalize_language(work.get("language")),
                is_open_access=open_access.get("is_oa") if isinstance(open_access, dict) else None,
                is_retracted=work.get("is_retracted"),
                funders=funders if funders else None,
            )
        except ValueError:
            return None

        return paper

    def _fetch_single_query(
        self,
        query_params: dict[str, Any],
        max_papers: int | None,
        papers: list[Paper],
        progress_callback: Callable[[int, int | None], None] | None,
        since: datetime.date | None = None,
        until: datetime.date | None = None,
    ) -> None:
        """Fetch papers for one converted query variant using cursor-based pagination.

        Parameters
        ----------
        query_params : dict
            Query parameters from the builder.
        max_papers : int | None
            Overall cap (shared with caller list).
        papers : list[Paper]
            Accumulator — papers appended in-place.
        progress_callback : Callable | None
            Progress callback.
        since : datetime.date | None
            Only return papers published on or after this date.
        until : datetime.date | None
            Only return papers published on or before this date.
        """
        cursor = "*"
        total: int | None = None
        processed = 0

        # Cap results to at most 1 year in the future to avoid placeholder
        # dates (e.g. 2050-01-01) that some upstream metadata sources produce.
        _max_pub_date = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()

        while True:
            if max_papers is not None and len(papers) >= max_papers:
                break

            remaining = (max_papers - len(papers)) if max_papers is not None else _PAGE_SIZE
            page_size = min(_PAGE_SIZE, remaining)

            params: dict = {
                **query_params,
                "per-page": page_size,
                "cursor": cursor,
                "sort": "publication_date:desc",
                "select": (
                    "id,doi,title,display_name,publication_date,authorships,"
                    "abstract_inverted_index,cited_by_count,open_access,locations,"
                    "primary_location,concepts,keywords,type,biblio,primary_topic,language,"
                    "is_retracted,funders"
                ),
            }

            # Inject the date cap into the existing filter string.
            existing_filter = params.get("filter", "")
            date_cap = f"to_publication_date:{_max_pub_date}"
            if existing_filter:
                params["filter"] = f"{existing_filter},{date_cap}"
            else:
                params["filter"] = date_cap

            # Apply user-specified date bounds on top of the automatic cap.
            if since is not None:
                params["filter"] += f",from_publication_date:{since.isoformat()}"
            if until is not None and until.isoformat() < _max_pub_date:
                # Only override the cap when the user's bound is tighter.
                params["filter"] += f",to_publication_date:{until.isoformat()}"

            try:
                response = self._get(_BASE_URL, params)
            except requests.RequestException as exc:
                logger.warning("OpenAlex request failed (cursor=%s): %s", cursor, exc)
                logger.debug("OpenAlex request exception details:", exc_info=True)
                break

            data = response.json()
            meta = data.get("meta") or {}
            if total is None:
                total = meta.get("count")

            results = data.get("results") or []
            if not results:
                break

            for work in results:
                paper = self._parse_paper(work)
                if paper is not None:
                    papers.append(paper)

            processed += len(results)
            if progress_callback is not None:
                progress_callback(processed, total)

            if max_papers is not None and len(papers) >= max_papers:
                break

            next_cursor = meta.get("next_cursor")
            if not next_cursor or len(results) < page_size:
                break

            cursor = next_cursor

        # Ensure the progress bar is updated even when the loop exits early
        # (e.g. on the first request returning no results or a request error),
        # so the bar never stays frozen at its initial 0-paper state.
        if progress_callback is not None:
            progress_callback(processed, total)

    def _fetch_papers(
        self,
        query: Query,
        max_papers: int | None,
        progress_callback: Callable[[int, int | None], None] | None,
        since: datetime.date | None = None,
        until: datetime.date | None = None,
    ) -> list[Paper]:
        """Fetch papers from OpenAlex handling query expansion.

        Parameters
        ----------
        query : Query
            Validated query object.
        max_papers : int | None
            Maximum papers to retrieve.
        progress_callback : Callable[[int, int | None], None] | None
            Progress callback.
        since : datetime.date | None
            Only return papers published on or after this date.
        until : datetime.date | None
            Only return papers published on or before this date.

        Returns
        -------
        list[Paper]
            Retrieved papers, deduplicated by DOI.
        """
        expanded = self._query_builder.expand_query(query)
        all_papers: list[Paper] = []
        seen_keys: set[str] = set()

        for sub_query in expanded:
            # Use a fresh accumulator per sub-query so that any preceding
            # branch does not exhaust the budget and prevent later branches
            # from being fetched.  Each branch is allowed to return up to
            # max_papers results independently; the combined list is
            # deduplicated and truncated to max_papers at the very end.
            sub_papers: list[Paper] = []
            sub_params = self._query_builder.convert_query(sub_query)
            self._fetch_single_query(
                sub_params, max_papers, sub_papers, progress_callback, since, until
            )

            # Merge into the global accumulator, deduplicating across branches.
            for paper in sub_papers:
                key = paper.doi or paper.url or paper.title
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_papers.append(paper)

        return all_papers[:max_papers] if max_papers is not None else all_papers


def _find_best_source(work: dict[str, Any]) -> dict[str, Any] | None:
    """Select the best publication source from an OpenAlex work.

    OpenAlex distinguishes several source types (``journal``, ``conference``,
    ``repository``, ``ebook platform``, ``book series``).  Repository sources
    represent hosting locations (institutional repos, Zenodo, etc.) rather than
    the actual publication venue and should be avoided when a proper venue is
    available.

    The function scans all locations — starting with the primary one — and
    returns the first source whose type is **not** ``repository``.  If every
    source is a repository (or no source is present at all) it returns
    ``None``.

    Parameters
    ----------
    work : dict
        OpenAlex work metadata dictionary.

    Returns
    -------
    dict | None
        The chosen source dict, or ``None`` when no suitable source exists.
    """
    _EXCLUDED_SOURCE_TYPES = {"repository"}

    # Collect all candidate locations, primary first.
    locations: list[dict] = []
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        locations.append(primary)

    for loc in work.get("locations") or []:
        if isinstance(loc, dict) and loc is not primary:
            locations.append(loc)

    for loc in locations:
        src = loc.get("source")
        if not isinstance(src, dict):
            continue
        src_type = (src.get("type") or "").strip().lower()
        if src_type and src_type in _EXCLUDED_SOURCE_TYPES:
            continue
        # Accept sources with a known non-repository type or no type at all
        # (missing type still beats a confirmed repository).
        if (src.get("display_name") or "").strip():
            return src

    return None


def _find_repository_source(work: dict[str, Any]) -> dict[str, Any] | None:
    """Find a repository-type source when no formal venue is available.

    When ``_find_best_source`` yields ``None`` (i.e. the work is only hosted
    on repository platforms), this helper returns the first repository source
    so the caller can create a ``Source`` with ``source_type=REPOSITORY``.

    Parameters
    ----------
    work : dict
        OpenAlex work metadata dictionary.

    Returns
    -------
    dict | None
        A repository source dict, or ``None`` when none exists.
    """
    locations: list[dict] = []
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        locations.append(primary)

    for loc in work.get("locations") or []:
        if isinstance(loc, dict) and loc is not primary:
            locations.append(loc)

    for loc in locations:
        src = loc.get("source")
        if not isinstance(src, dict):
            continue
        src_type = (src.get("type") or "").strip().lower()
        if src_type == "repository" and (src.get("display_name") or "").strip():
            return src

    return None


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct plain text abstract from OpenAlex inverted index.

    Uses a pre-allocated list indexed by position to avoid sorting.

    Parameters
    ----------
    inverted_index : dict | None
        Mapping of word → list of positions, or ``None``.

    Returns
    -------
    str
        Reconstructed abstract string.
    """
    if not inverted_index:
        return ""

    # Find the maximum position to pre-allocate the output list
    max_pos = max(pos for positions in inverted_index.values() for pos in positions)
    words: list[str] = [""] * (max_pos + 1)

    for word, positions in inverted_index.items():
        for pos in positions:
            words[pos] = word

    return " ".join(w for w in words if w)
