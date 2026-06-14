"""Query validation logic."""

from __future__ import annotations

import re

from findpapers.core.query import VALID_FILTER_CODES
from findpapers.exceptions import QueryValidationError


class QueryValidator:
    """Validates query strings before parsing.

    This class encapsulates all validation logic for search query strings,
    ensuring they follow the required syntax rules.
    """

    def validate(self, query_string: str) -> None:
        """Validate a query string.

        Parameters
        ----------
        query_string : str
            The query string to validate.

        Raises
        ------
        QueryValidationError
            If the query is invalid.
        """
        query = query_string.strip()

        if not query:
            raise QueryValidationError("Query cannot be empty")

        # Check for balanced brackets
        self._check_balanced_brackets(query)

        # Check for balanced parentheses
        self._check_balanced_parentheses(query)

        # Check for empty terms
        if "[]" in query:
            raise QueryValidationError("Terms cannot be empty: found []")

        # Validate filter specifiers
        self._validate_filter_codes(query)

        # Extract and validate all terms
        terms = re.findall(r"\[([^\]]*)\]", query)
        for term in terms:
            self._validate_term(term)

        # Check for consecutive terms without operators
        self._check_consecutive_terms(query)

        # Validate operators
        self._validate_operators(query)

        # Validate query structure (must have at least one term)
        self._validate_query_structure(query)

    def _validate_filter_codes(self, query: str) -> None:
        """Validate filter specifier codes in the query.

        Filter codes are case-insensitive (TI is the same as ti).

        Parameters
        ----------
        query : str
            The query string.

        Raises
        ------
        QueryValidationError
            If invalid filter codes are found.
        """
        # Pattern to match filter prefixes before terms or groups
        # Matches patterns like: ti, abs, tiabs, TIABS, etc. directly before [ or (
        # Case-insensitive to catch both valid and invalid cases
        filter_prefix_pattern = r"(?<![a-zA-Z])([a-zA-Z]+)(?=\[|\()"

        matches = re.finditer(filter_prefix_pattern, query)
        for match in matches:
            filter_code = match.group(1)
            # Normalize to lowercase for validation
            filter_code_lower = filter_code.lower()
            # Skip if it's a boolean operator (AND, OR, NOT)
            if filter_code_lower in {"and", "or", "not"}:
                continue
            if filter_code_lower not in VALID_FILTER_CODES:
                raise QueryValidationError(
                    f"Invalid filter code '{filter_code}'. "
                    f"Valid codes are: {', '.join(sorted(VALID_FILTER_CODES))}"
                )

    def _check_balanced_brackets(self, query: str) -> None:
        """Check that square brackets are balanced.

        Parameters
        ----------
        query : str
            The query string.

        Raises
        ------
        QueryValidationError
            If brackets are not balanced.
        """
        count = 0
        for char in query:
            if char == "[":
                count += 1
            elif char == "]":
                count -= 1
            if count < 0:
                raise QueryValidationError("Unbalanced square brackets")
        if count != 0:
            raise QueryValidationError("Unbalanced square brackets")

    def _check_balanced_parentheses(self, query: str) -> None:
        """Check that parentheses are balanced.

        Parameters
        ----------
        query : str
            The query string.

        Raises
        ------
        QueryValidationError
            If parentheses are not balanced.
        """
        count = 0
        inside_term = False
        for char in query:
            if char == "[":
                inside_term = True
            elif char == "]":
                inside_term = False
            elif not inside_term:
                if char == "(":
                    count += 1
                elif char == ")":
                    count -= 1
                if count < 0:
                    raise QueryValidationError("Unbalanced parentheses")
        if count != 0:
            raise QueryValidationError("Unbalanced parentheses")

    def _validate_term(self, term: str) -> None:
        """Validate a single term.

        Parameters
        ----------
        term : str
            The term content (without brackets).

        Raises
        ------
        QueryValidationError
            If the term is invalid.
        """
        if not term or not term.strip():
            raise QueryValidationError("Terms cannot be empty")

        # Terms cannot contain double quotes
        if '"' in term:
            raise QueryValidationError(f"Terms cannot contain double quotes: [{term}]")

        # Count wildcards
        question_count = term.count("?")
        asterisk_count = term.count("*")
        total_wildcards = question_count + asterisk_count

        if total_wildcards == 0:
            return  # No wildcards, term is valid

        # Only one wildcard per term
        if total_wildcards > 1:
            raise QueryValidationError(
                f"Only one wildcard can be included in a search term: [{term}]"
            )

        # Wildcards cannot be at the start
        if term.startswith("?") or term.startswith("*"):
            raise QueryValidationError(
                f"Wildcards cannot be used at the start of a search term: [{term}]"
            )

        # Wildcards only in single terms (no spaces)
        if " " in term and total_wildcards > 0:
            raise QueryValidationError(
                f"Wildcards can be used only in single terms (no spaces): [{term}]"
            )

        # Asterisk-specific rules
        if asterisk_count > 0:
            asterisk_pos = term.index("*")

            # Asterisk can only be at the end
            if asterisk_pos != len(term) - 1:
                raise QueryValidationError(
                    f"The asterisk wildcard can only be used at the end of a search term: [{term}]"
                )

    def _check_consecutive_terms(self, query: str) -> None:
        """Check for consecutive terms without operators.

        Parameters
        ----------
        query : str
            The query string.

        Raises
        ------
        QueryValidationError
            If consecutive terms are found without operators.
        """
        # Pattern: ] followed by optional whitespace then [
        pattern = r"\]\s*\["
        if re.search(pattern, query):
            raise QueryValidationError(
                "Terms must be separated by boolean operators (AND, OR, AND NOT)"
            )

    def _validate_operators(self, query: str) -> None:
        """Validate boolean operators in the query.

        Parameters
        ----------
        query : str
            The query string.

        Raises
        ------
        QueryValidationError
            If operators are invalid.
        """
        # Remove terms to check operators and normalize to uppercase for validation
        query_without_terms = re.sub(r"\[[^\]]*\]", "TERM", query)
        query_upper = query_without_terms.upper()

        # Check for operators without proper whitespace (case-insensitive)
        if re.search(r"TERM(AND|OR|NOT)", query_upper):
            raise QueryValidationError("Operators must have whitespace before and after them")
        if re.search(r"(AND|OR|NOT)TERM", query_upper):
            raise QueryValidationError("Operators must have whitespace before and after them")

        # Check for NOT without preceding AND (case-insensitive)
        # Find all NOT occurrences and check if preceded by AND
        not_matches = list(re.finditer(r"\bNOT\b", query_upper))
        for match in not_matches:
            pos = match.start()
            before = query_upper[:pos].strip()
            # Must end with AND
            if not before.endswith("AND"):
                raise QueryValidationError(
                    "NOT operator must be preceded by AND: use 'AND NOT' instead of 'OR NOT' or just 'NOT'"
                )

        # Check for invalid operators
        words = query_upper.split()
        valid_keywords = {"AND", "OR", "NOT", "TERM", "(", ")"}
        for word in words:
            # Clean parentheses
            clean_word = word.strip("()")
            if (
                clean_word
                and clean_word not in valid_keywords
                and clean_word in {"XOR", "NAND", "NOR"}
            ):
                raise QueryValidationError(f"Invalid boolean operator: {clean_word}")

    def _validate_connector_placement(self, structure: str) -> None:
        """Validate that connectors are properly placed between terms/groups.

        Connectors (AND, OR, AND NOT) must be between terms or groups.
        A single term cannot have connectors.

        Parameters
        ----------
        structure : str
            Query structure with terms replaced by TERM markers.

        Raises
        ------
        QueryValidationError
            If connectors are not properly placed.
        """
        # Normalize: replace groups with GROUP marker, treating (...) as a unit
        # First, handle nested parentheses by iteratively replacing innermost groups
        normalized = structure
        while "(" in normalized:
            # Replace innermost parentheses groups
            normalized = re.sub(r"\([^()]*\)", " GROUP ", normalized)

        # Now we have a flat sequence of TERM, GROUP, and operators
        # Normalize whitespace and uppercase
        normalized = " ".join(normalized.upper().split())

        # Replace "AND NOT" with a single token to treat as one connector
        normalized = normalized.replace("AND NOT", "ANDNOT")

        tokens = normalized.split()

        if not tokens:
            return

        # Check: first token cannot be a connector
        if tokens[0] in {"AND", "OR", "ANDNOT", "NOT"}:
            raise QueryValidationError(
                "Connectors cannot appear at the beginning of a query or subquery"
            )

        # Check: last token cannot be a connector
        if tokens[-1] in {"AND", "OR", "ANDNOT", "NOT"}:
            raise QueryValidationError("Connectors cannot appear at the end of a query or subquery")

        # Check: connectors must be between terms/groups (not consecutive)
        for i, token in enumerate(tokens):
            if token in {"AND", "OR", "ANDNOT"}:
                # Previous token must be TERM or GROUP
                if i > 0 and tokens[i - 1] in {"AND", "OR", "ANDNOT", "NOT"}:
                    raise QueryValidationError(
                        "Connectors must be between terms or groups, not consecutive"
                    )
                # Next token must be TERM or GROUP
                if i < len(tokens) - 1 and tokens[i + 1] in {"AND", "OR", "ANDNOT"}:
                    raise QueryValidationError(
                        "Connectors must be between terms or groups, not consecutive"
                    )

    def _validate_query_structure(self, query: str) -> None:
        """Validate query structure - must have terms and proper connectors.

        Parameters
        ----------
        query : str
            The query string.

        Raises
        ------
        QueryValidationError
            If query structure is invalid.
        """
        # Replace terms with a marker and parentheses with nothing
        # to check what's left (should only be whitespace and valid operators)
        structure = re.sub(r"\[[^\]]*\]", " TERM ", query)

        # Check if the query contains any terms at all
        if "TERM" not in structure:
            raise QueryValidationError("Query must contain at least one term enclosed in []")

        # Validate connector placement - connectors must be between terms/groups
        self._validate_connector_placement(structure)

        # Remove all filter prefixes before checking for invalid content
        # A filter prefix is a sequence of letters directly followed by ( or TERM
        # First handle filter prefixes before TERM (from original brackets)
        structure_cleaned = re.sub(r"[a-zA-Z]+(?=\s*TERM)", " ", structure)
        # Then handle filter prefixes before ( (groups)
        structure_cleaned = re.sub(r"[a-zA-Z]+(?=\s*\()", " ", structure_cleaned)

        # Check between terms/groups for invalid content
        # Split by TERM and check each segment
        segments = structure_cleaned.split("TERM")

        for segment in segments:
            # Skip empty segments
            if not segment.strip():
                continue

            # Remove parentheses
            cleaned = segment.replace("(", " ").replace(")", " ")
            cleaned = cleaned.strip()

            if not cleaned:
                continue

            # The segment should only contain valid operators (case-insensitive)
            words = cleaned.split()
            for word in words:
                word_upper = word.upper()
                if (
                    word_upper not in {"AND", "OR", "NOT"}
                    and word
                    and not word.startswith("(")
                    and not word.endswith(")")
                ):
                    raise QueryValidationError(
                        f"All terms must be enclosed in square brackets: found '{word}'"
                    )
