"""Semantic Scholar query builder."""

from __future__ import annotations

import re

from findpapers.core.query import FilterCode, Query, QueryNode
from findpapers.query.builder import QueryBuilder, QueryValidationResult


class SemanticScholarQueryBuilder(QueryBuilder):
    """Build Semantic Scholar bulk-search payloads."""

    _SUPPORTED_FILTERS = frozenset({FilterCode.TITLE_ABSTRACT})

    def validate_query(self, query: Query) -> QueryValidationResult:
        """Validate whether Semantic Scholar supports this query.

        Parameters
        ----------
        query : Query
            Query to validate.

        Returns
        -------
        QueryValidationResult
            Validation result.
        """
        term_nodes = self.iter_term_nodes(query.root)

        for term in term_nodes:
            filter_code = self.get_effective_filter(term)
            if not self.supports_filter(filter_code):
                return QueryValidationResult(
                    is_valid=False,
                    error_message=f"Filter '{filter_code}' is not supported by Semantic Scholar.",
                )
            if term.value and "?" in term.value:
                return QueryValidationResult(
                    is_valid=False,
                    error_message="Wildcard '?' is not supported by Semantic Scholar bulk search.",
                )
        return QueryValidationResult(is_valid=True)

    def convert_query(self, query: Query) -> dict:
        """Convert query into Semantic Scholar bulk-search parameters.

        Parameters
        ----------
        query : Query
            Query to convert.

        Returns
        -------
        dict
            Semantic Scholar request parameters.
        """
        from findpapers.core.query import ConnectorType

        preprocessed = self.preprocess_terms(query)

        connector_map = {
            ConnectorType.AND: "+",
            ConnectorType.OR: "|",
            ConnectorType.AND_NOT: "-",
        }

        def convert_term(term_node: QueryNode) -> str:
            term = term_node.value or ""
            return f'"{term}"' if " " in term else term

        expression = self.convert_expression(preprocessed.root, convert_term, connector_map)
        normalized_query = " ".join(expression.split())
        normalized_query = re.sub(r"\(\s*\)", "", normalized_query)
        normalized_query = " ".join(normalized_query.split())

        return {"query": normalized_query or "*"}

    def preprocess_terms(self, query: Query) -> Query:
        """Replace hyphens with spaces due to API tokenization behavior.

        Parameters
        ----------
        query : Query
            Query to preprocess.

        Returns
        -------
        Query
            Preprocessed query.
        """
        cloned = self.clone_query(query)
        for term in self.iter_term_nodes(cloned.root):
            if term.value:
                term.value = term.value.replace("-", " ")
        return cloned
