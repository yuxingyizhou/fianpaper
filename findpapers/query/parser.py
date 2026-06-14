"""Query parsing logic."""

from __future__ import annotations

import re

from findpapers.core.query import (
    VALID_FILTER_CODES,
    ConnectorType,
    FilterCode,
    NodeType,
    Query,
    QueryNode,
)
from findpapers.exceptions import QueryValidationError


class QueryParser:
    """Parses query strings into QueryNode trees.

    This class encapsulates all parsing logic for converting validated query strings
    into structured tree representations.
    """

    def parse(self, query_string: str) -> Query:
        """Parse a query string into a Query object.

        Parameters
        ----------
        query_string : str
            The validated query string to parse.

        Returns
        -------
        Query
            The parsed query with a tree structure.
        """
        raw_query = query_string.strip()
        root = self._parse_query_recursive(raw_query, None)
        return Query(raw_query=raw_query, root=root)

    def _extract_filter_prefix(self, text: str) -> tuple[FilterCode | None, str]:
        """Extract filter prefix from the end of a text buffer.

        Given text like "something ti", extracts the filter code and returns
        the remaining text without the filter prefix.

        Filter codes are case-insensitive and normalized to lowercase.

        Parameters
        ----------
        text : str
            Text that may end with a filter prefix.

        Returns
        -------
        tuple[FilterCode | None, str]
            Tuple of (filter_code, remaining_text). filter_code is None if no
            valid filter prefix was found. Filter code is normalized to lowercase.
        """
        # Match filter prefix pattern at the end: ti, abs, tiabs, TI, etc.
        # The pattern should be at the end and followed by nothing (we're at [ or ()
        # Case-insensitive pattern
        pattern = r"([a-zA-Z]+)$"
        # Strip the text to work with clean version
        text_stripped = text.strip()
        match = re.search(pattern, text_stripped)
        if match:
            filter_code = match.group(1).lower()
            # Verify filter is valid
            if filter_code in VALID_FILTER_CODES:
                # Remove the filter prefix from the stripped text
                remaining = text_stripped[: match.start()]
                return FilterCode(filter_code), remaining.rstrip()
        return None, text

    def _parse_query_recursive(self, query: str, parent: QueryNode | None) -> QueryNode:
        """Recursively parse a query or subquery.

        Parameters
        ----------
        query : str
            The query string to parse.
        parent : QueryNode | None
            The parent node, or None for root.

        Returns
        -------
        QueryNode
            The parsed node.
        """
        if parent is None:
            parent = QueryNode(node_type=NodeType.ROOT, children=[])

        query_iterator = iter(query)
        current_character = next(query_iterator, None)
        current_connector = ""

        while current_character is not None:
            if current_character == "(":  # Beginning of a group
                # Extract any filter prefix from current_connector
                filter_code, remaining_connector = self._extract_filter_prefix(current_connector)

                if remaining_connector.strip():
                    parent.children.append(
                        QueryNode(
                            node_type=NodeType.CONNECTOR,
                            value=ConnectorType(remaining_connector.strip().lower()),
                        )
                    )
                current_connector = ""

                subquery_chars: list[str] = []
                subquery_group_level = 1

                while True:
                    current_character = next(query_iterator, None)

                    if current_character is None:
                        raise QueryValidationError("Unbalanced parentheses")

                    if current_character == "[":
                        # Skip content inside brackets
                        subquery_chars.append(current_character)
                        while True:
                            current_character = next(query_iterator, None)
                            if current_character is None:
                                raise QueryValidationError("Missing term closing bracket")
                            subquery_chars.append(current_character)
                            if current_character == "]":
                                break
                        continue

                    if current_character == "(":
                        subquery_group_level += 1

                    elif current_character == ")":
                        subquery_group_level -= 1
                        if subquery_group_level == 0:
                            break

                    subquery_chars.append(current_character)

                subquery = "".join(subquery_chars)

                group_node = QueryNode(
                    node_type=NodeType.GROUP, children=[], filter_code=filter_code
                )
                parent.children.append(group_node)
                self._parse_query_recursive(subquery, group_node)

            elif current_character == "[":  # Beginning of a term
                # Extract any filter prefix from current_connector
                filter_code, remaining_connector = self._extract_filter_prefix(current_connector)

                if remaining_connector.strip():
                    parent.children.append(
                        QueryNode(
                            node_type=NodeType.CONNECTOR,
                            value=ConnectorType(remaining_connector.strip().lower()),
                        )
                    )
                current_connector = ""

                term_chars: list[str] = []
                while True:
                    current_character = next(query_iterator, None)

                    if current_character is None:
                        raise QueryValidationError("Missing term closing bracket")

                    if current_character == "]":
                        break

                    term_chars.append(current_character)

                term_value = "".join(term_chars)

                parent.children.append(
                    QueryNode(node_type=NodeType.TERM, value=term_value, filter_code=filter_code)
                )

            else:  # Part of a connector
                current_connector += current_character

            current_character = next(query_iterator, None)

        return parent
