"""PubMed query builder."""

from __future__ import annotations

from findpapers.core.query import FilterCode, Query, QueryNode
from findpapers.query.builder import QueryBuilder, QueryValidationResult


class PubmedQueryBuilder(QueryBuilder):
    """Build PubMed-compatible query expressions."""

    _SUPPORTED_FILTERS = frozenset(
        {
            FilterCode.TITLE,
            FilterCode.ABSTRACT,
            FilterCode.KEYWORDS,
            FilterCode.AUTHOR,
            FilterCode.SOURCE,
            FilterCode.AFFILIATION,
            FilterCode.TITLE_ABSTRACT,
            FilterCode.TITLE_ABSTRACT_KEYWORDS,
        }
    )

    def validate_query(self, query: Query) -> QueryValidationResult:
        """Validate whether PubMed supports this query.

        Parameters
        ----------
        query : Query
            Query to validate.

        Returns
        -------
        QueryValidationResult
            Validation result.
        """
        for term in self.iter_term_nodes(query.root):
            filter_code = self.get_effective_filter(term)
            if not self.supports_filter(filter_code):
                return QueryValidationResult(
                    is_valid=False,
                    error_message=f"Filter '{filter_code}' is not supported by PubMed.",
                )
            if term.value and "?" in term.value:
                return QueryValidationResult(
                    is_valid=False,
                    error_message="Wildcard '?' is not supported by PubMed.",
                )
            if term.value and "*" in term.value:
                wildcard_index = term.value.find("*")
                if wildcard_index < 4:
                    return QueryValidationResult(
                        is_valid=False,
                        error_message=(
                            "PubMed wildcard '*' requires at least 4 characters before '*'."
                        ),
                    )
        return QueryValidationResult(is_valid=True)

    def convert_query(self, query: Query) -> str:
        """Convert query into PubMed syntax.

        Parameters
        ----------
        query : Query
            Query to convert.

        Returns
        -------
        str
            PubMed query string.
        """
        from findpapers.core.query import ConnectorType

        connector_map = {
            ConnectorType.AND: "AND",
            ConnectorType.OR: "OR",
            ConnectorType.AND_NOT: "NOT",
        }

        def convert_term(term_node: QueryNode) -> str:
            term = term_node.value or ""
            filter_code = self.get_effective_filter(term_node)

            def tagged(tag: str) -> str:
                return f'"{term}"[{tag}]'

            if filter_code == FilterCode.TITLE:
                return tagged("ti")
            if filter_code == FilterCode.ABSTRACT:
                return tagged("ab")
            if filter_code == FilterCode.KEYWORDS:
                # The [ot] tag searches the "Other Term" field which contains
                # the author-supplied keywords of the article.  Unlike [mh]
                # (MeSH headings assigned by NLM indexers), [ot] reflects the
                # actual keywords chosen by the paper's authors.
                # Note: not all articles have author keywords; older records
                # may only have MeSH terms.
                return tagged("ot")
            if filter_code == FilterCode.AUTHOR:
                return tagged("au")
            if filter_code == FilterCode.SOURCE:
                return tagged("journal")
            if filter_code == FilterCode.AFFILIATION:
                return tagged("ad")
            if filter_code == FilterCode.TITLE_ABSTRACT_KEYWORDS:
                return f"({tagged('tiab')} OR {tagged('ot')})"
            return tagged("tiab")

        def plain_term(term_node: QueryNode) -> str:
            """Convert term without filter prefix."""
            return f'"{term_node.value or ""}"'

        def group_wrapper(group_node: QueryNode, inner: str) -> str | None:
            """Wrap a plain group expression with PubMed postfix tag.

            Compound filters that expand to multiple tags (e.g. tiabskey)
            fall back to per-term conversion.
            """
            filter_code = self.get_effective_filter(group_node)
            tag_map: dict[FilterCode, str] = {
                FilterCode.TITLE: "ti",
                FilterCode.ABSTRACT: "ab",
                FilterCode.KEYWORDS: "ot",
                FilterCode.AUTHOR: "au",
                FilterCode.SOURCE: "journal",
                FilterCode.AFFILIATION: "ad",
                FilterCode.TITLE_ABSTRACT: "tiab",
            }
            tag = tag_map.get(filter_code)
            if tag is None:
                return None  # compound filters fall back to per-term
            return f"({inner})[{tag}]"

        return self.convert_expression(
            query.root,
            convert_term,
            connector_map,
            plain_term_converter=plain_term,
            optimized_group_converter=group_wrapper,
        )
