"""Scopus query builder."""

from __future__ import annotations

import re

from findpapers.core.query import FilterCode, Query, QueryNode
from findpapers.query.builder import QueryBuilder, QueryValidationResult


class ScopusQueryBuilder(QueryBuilder):
    """Build Scopus-compatible query expressions."""

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
        """Validate whether Scopus supports this query.

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
                    error_message=f"Filter '{filter_code}' is not supported by Scopus.",
                )
            if not term.value:
                continue

            first_wildcard = min(
                [index for index in (term.value.find("*"), term.value.find("?")) if index != -1],
                default=-1,
            )
            if first_wildcard != -1 and first_wildcard < 3:
                return QueryValidationResult(
                    is_valid=False,
                    error_message=(
                        "Scopus wildcards require at least 3 characters before '*' or '?'."
                    ),
                )

            if re.search(r"[-.][*?]|[*?][-.]", term.value):
                return QueryValidationResult(
                    is_valid=False,
                    error_message="Scopus does not support wildcard combined with hyphen or dot.",
                )
        return QueryValidationResult(is_valid=True)

    def convert_query(self, query: Query) -> str:
        """Convert query into Scopus syntax.

        When a GROUP node's ``children_match_filter`` is ``True`` the filter is
        applied once at the group level (e.g. ``TITLE("a" OR "b")``), avoiding
        redundant per-term wrapping.

        Parameters
        ----------
        query : Query
            Query to convert.

        Returns
        -------
        str
            Scopus query string.
        """
        from findpapers.core.query import ConnectorType

        connector_map = {
            ConnectorType.AND: "AND",
            ConnectorType.OR: "OR",
            ConnectorType.AND_NOT: "AND NOT",
        }

        def convert_term(term_node: QueryNode) -> str:
            term = term_node.value or ""
            quoted = f'"{term}"'
            filter_code = self.get_effective_filter(term_node)

            if filter_code == FilterCode.TITLE:
                return f"TITLE({quoted})"
            if filter_code == FilterCode.ABSTRACT:
                return f"ABS({quoted})"
            if filter_code == FilterCode.KEYWORDS:
                return f"KEY({quoted})"
            if filter_code == FilterCode.AUTHOR:
                return f"AUTH({quoted})"
            if filter_code == FilterCode.SOURCE:
                return f"SRCTITLE({quoted})"
            if filter_code == FilterCode.AFFILIATION:
                return f"AFFIL({quoted})"
            if filter_code == FilterCode.TITLE_ABSTRACT:
                return f"TITLE-ABS({quoted})"
            return f"TITLE-ABS-KEY({quoted})"

        def plain_term(term_node: QueryNode) -> str:
            """Convert term without filter prefix."""
            return f'"{term_node.value or ""}"'

        def group_wrapper(group_node: QueryNode, inner: str) -> str | None:
            """Wrap a plain group expression with the Scopus field operator."""
            return _scopus_field_wrap(self.get_effective_filter(group_node), inner)

        return self.convert_expression(
            query.root,
            convert_term,
            connector_map,
            plain_term_converter=plain_term,
            optimized_group_converter=group_wrapper,
        )


def _scopus_field_wrap(filter_code: FilterCode, inner: str) -> str:
    """Wrap an inner expression with the Scopus field operator.

    Parameters
    ----------
    filter_code : FilterCode
        Effective filter code for the group.
    inner : str
        Plain inner expression (terms without individual filter prefixes).

    Returns
    -------
    str
        Expression wrapped in the appropriate Scopus field function.
    """
    field_map: dict[FilterCode, str] = {
        FilterCode.TITLE: "TITLE",
        FilterCode.ABSTRACT: "ABS",
        FilterCode.KEYWORDS: "KEY",
        FilterCode.AUTHOR: "AUTH",
        FilterCode.SOURCE: "SRCTITLE",
        FilterCode.AFFILIATION: "AFFIL",
        FilterCode.TITLE_ABSTRACT: "TITLE-ABS",
        FilterCode.TITLE_ABSTRACT_KEYWORDS: "TITLE-ABS-KEY",
    }
    field = field_map.get(filter_code, "TITLE-ABS-KEY")
    return f"{field}({inner})"
