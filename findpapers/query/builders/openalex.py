"""OpenAlex query builder."""

from __future__ import annotations

import itertools

from findpapers.core.query import ConnectorType, FilterCode, NodeType, Query, QueryNode
from findpapers.exceptions import UnsupportedQueryError
from findpapers.query.builder import QueryBuilder, QueryValidationResult


class OpenAlexQueryBuilder(QueryBuilder):
    """Build OpenAlex-compatible query parameter dictionaries."""

    _SUPPORTED_FILTERS = frozenset(
        {
            FilterCode.TITLE,
            FilterCode.ABSTRACT,
            FilterCode.AUTHOR,
            FilterCode.AFFILIATION,
            FilterCode.TITLE_ABSTRACT,
        }
    )

    def validate_query(self, query: Query) -> QueryValidationResult:
        """Validate whether OpenAlex supports this query.

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
                    error_message=f"Filter '{filter_code}' is not supported by OpenAlex.",
                )
            if term.value and (self.has_wildcard(term.value) or "~" in term.value):
                return QueryValidationResult(
                    is_valid=False,
                    error_message="Wildcards are not supported by OpenAlex.",
                )
        return QueryValidationResult(is_valid=True)

    def convert_query(self, query: Query) -> dict:
        """Convert query into OpenAlex filter syntax.

        Parameters
        ----------
        query : Query
            Query to convert.

        Returns
        -------
        dict
            OpenAlex parameters.
        """
        connectors = set(self.iter_connectors(query.root))
        if ConnectorType.OR in connectors or ConnectorType.AND_NOT in connectors:
            return {"search": self._to_openalex_boolean_search(query.root)}

        filters: list[str] = []
        for term_node in self.iter_term_nodes(query.root):
            term = term_node.value or ""
            filter_code = self.get_effective_filter(term_node)
            filters.append(self._build_filter_fragment(filter_code, term))
        return {"filter": ",".join(filters)}

    def expand_query(self, query: Query) -> list[Query]:
        """Return query without expansion for OpenAlex.

        Parameters
        ----------
        query : Query
            Input query.

        Returns
        -------
        list[Query]
            Single query list.
        """
        # OpenAlex lacks field-aware OR inside `filter` for mixed cases.
        # Decompose pure OR branches into independent AND-only queries.
        connectors = set(self.iter_connectors(query.root))
        if ConnectorType.OR in connectors and ConnectorType.AND_NOT not in connectors:
            clauses = self._to_dnf_with_filters(query.root)
            return self._build_queries_from_clauses(clauses, query.raw_query)

        return [query]

    def _build_filter_fragment(self, filter_code: FilterCode, term: str) -> str:
        """Build OpenAlex filter fragment for one term.

        Parameters
        ----------
        filter_code : FilterCode
            Effective filter code.
        term : str
            Search term.

        Returns
        -------
        str
            OpenAlex filter fragment.
        """
        encoded_term = f'"{term}"' if " " in term else term
        if filter_code == FilterCode.TITLE:
            return f"title.search:{encoded_term}"
        if filter_code == FilterCode.ABSTRACT:
            return f"abstract.search:{encoded_term}"
        if filter_code == FilterCode.AUTHOR:
            return f"raw_author_name.search:{encoded_term}"
        if filter_code == FilterCode.AFFILIATION:
            return f"raw_affiliation_strings.search:{encoded_term}"
        if filter_code == FilterCode.TITLE_ABSTRACT:
            return f"title_and_abstract.search:{encoded_term}"
        raise UnsupportedQueryError(f"Unsupported filter code for OpenAlex: {filter_code}")

    def _to_openalex_boolean_search(self, node: QueryNode) -> str:
        """Convert query node to OpenAlex boolean search expression.

        Parameters
        ----------
        node : QueryNode
            Query node.

        Returns
        -------
        str
            Boolean search expression.
        """
        if node.node_type == NodeType.TERM:
            term = node.value or ""
            return f'"{term}"' if " " in term else term

        connector_map = {
            ConnectorType.AND: "AND",
            ConnectorType.OR: "OR",
            ConnectorType.AND_NOT: "NOT",
        }
        parts: list[str] = []
        for child in node.children:
            if child.node_type == NodeType.CONNECTOR and child.value:
                parts.append(connector_map[ConnectorType(child.value)])
                continue
            converted = self._to_openalex_boolean_search(child)
            if child.node_type == NodeType.GROUP:
                parts.append(f"({converted})")
            else:
                parts.append(converted)
        return " ".join(parts)

    def _to_dnf_with_filters(self, node: QueryNode) -> list[list[tuple[str, FilterCode]]]:
        """Convert query subtree to DNF preserving effective filters.

        Parameters
        ----------
        node : QueryNode
            Query node.

        Returns
        -------
        list[list[tuple[str, FilterCode]]]
            Clauses of (term, filter_code).
        """
        if node.node_type == NodeType.TERM:
            return [[(node.value or "", self.get_effective_filter(node))]]

        operands: list[QueryNode] = [
            child for child in node.children if child.node_type in (NodeType.TERM, NodeType.GROUP)
        ]
        connectors_list: list[ConnectorType] = [
            ConnectorType(child.value)
            for child in node.children
            if child.node_type == NodeType.CONNECTOR and child.value
        ]

        if not operands:
            return [[]]

        current = self._to_dnf_with_filters(operands[0])
        for index, connector in enumerate(connectors_list, start=1):
            right = self._to_dnf_with_filters(operands[index])
            if connector == ConnectorType.OR:
                current = current + right
            elif connector == ConnectorType.AND:
                # Cartesian product between left/right clauses keeps all valid
                # conjunction combinations when collapsing `(A OR B) AND (C OR D)`.
                product: list[list[tuple[str, FilterCode]]] = []
                for left_clause, right_clause in itertools.product(current, right):
                    product.append(left_clause + right_clause)
                current = product
            else:
                # `AND NOT` cannot be safely distributed into independent field
                # filters for this fallback path, so keep terms together.
                return [
                    [
                        (
                            term_node.value or "",
                            self.get_effective_filter(term_node),
                        )
                        for term_node in self.iter_term_nodes(node)
                    ]
                ]
        return current

    def _build_queries_from_clauses(
        self,
        clauses: list[list[tuple[str, FilterCode]]],
        raw_query: str,
    ) -> list[Query]:
        """Build Query objects from DNF clauses.

        Parameters
        ----------
        clauses : list[list[tuple[str, FilterCode]]]
            DNF clauses.
        raw_query : str
            Original query string.

        Returns
        -------
        list[Query]
            Expanded queries.
        """
        queries: list[Query] = []
        for clause in clauses:
            children: list[QueryNode] = []
            for index, (term, filter_code) in enumerate(clause):
                if index > 0:
                    children.append(
                        QueryNode(node_type=NodeType.CONNECTOR, value=ConnectorType.AND)
                    )
                children.append(
                    QueryNode(node_type=NodeType.TERM, value=term, filter_code=filter_code)
                )
            queries.append(
                Query(
                    raw_query=raw_query, root=QueryNode(node_type=NodeType.ROOT, children=children)
                )
            )
        return queries
