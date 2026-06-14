"""arXiv query builder."""

from __future__ import annotations

from findpapers.core.query import FilterCode, Query, QueryNode
from findpapers.query.builder import QueryBuilder, QueryValidationResult


class ArxivQueryBuilder(QueryBuilder):
    """Build arXiv-compatible query expressions."""

    _SUPPORTED_FILTERS = frozenset(
        {
            FilterCode.TITLE,
            FilterCode.ABSTRACT,
            FilterCode.AUTHOR,
            FilterCode.TITLE_ABSTRACT,
        }
    )

    def validate_query(self, query: Query) -> QueryValidationResult:
        """Validate whether arXiv supports this query.

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
                    error_message=f"Filter '{filter_code}' is not supported by arXiv.",
                )
        return QueryValidationResult(is_valid=True)

    def convert_query(self, query: Query) -> str:
        """Convert query into arXiv search expression.

        Parameters
        ----------
        query : Query
            Query to convert.

        Returns
        -------
        str
            arXiv query string.
        """
        from findpapers.core.query import ConnectorType

        preprocessed = self.preprocess_terms(query)

        connector_map = {
            ConnectorType.AND: "AND",
            ConnectorType.OR: "OR",
            ConnectorType.AND_NOT: "ANDNOT",
        }

        # NOTE: arXiv uses Apache Lucene internally, which applies stemming to
        # all text searches (ti:, abs:, all:, etc.). This means a search like
        # ti:"transformer" will also match "transformations", "transformed", etc.
        # This is a server-side behavior that cannot be disabled via the API,
        # and is most noticeable in title filters where exact matches are expected.
        def convert_term(term_node: QueryNode) -> str:
            term = self.quote_term(term_node.value or "")
            filter_code = self.get_effective_filter(term_node)

            if filter_code == FilterCode.TITLE:
                return f"ti:{term}"
            if filter_code == FilterCode.ABSTRACT:
                return f"abs:{term}"
            if filter_code == FilterCode.AUTHOR:
                return f"au:{term}"
            return f"(ti:{term} OR abs:{term})"

        return self.convert_expression(preprocessed.root, convert_term, connector_map)

    def preprocess_terms(self, query: Query) -> Query:
        """Replace hyphens with spaces for arXiv compatibility.

        Parameters
        ----------
        query : Query
            Query to preprocess.

        Returns
        -------
        Query
            Query with terms normalized for arXiv.
        """
        cloned_query = self.clone_query(query)
        for term in self.iter_term_nodes(cloned_query.root):
            if term.value:
                term.value = term.value.replace("-", " ")
        return cloned_query
