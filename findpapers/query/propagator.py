"""Filter propagation logic for query nodes."""

from __future__ import annotations

from findpapers.core.query import FilterCode, NodeType, Query, QueryNode


class FilterPropagator:
    """Propagates filter specifiers through query tree nodes.

    This class handles the logic of inheriting filter codes from parent nodes
    to child nodes in the query tree, following the rule: innermost filter wins.
    """

    def propagate(self, query: Query) -> Query:
        """Propagate filter specifiers through the query tree.

        This modifies the query in-place by updating inherited_filter_code
        and children_match_filter attributes on all nodes.

        Parameters
        ----------
        query : Query
            The query whose tree needs filter propagation.

        Returns
        -------
        Query
            The same query with updated filter inheritance.
        """
        self._propagate_filters(query.root)
        return query

    def _propagate_filters(self, node: QueryNode, parent_filter: FilterCode | None = None) -> None:
        """Propagate filter specifier from parent nodes to children.

        This calculates inherited_filter_code and children_match_filter for all nodes:
        1. inherited_filter_code: Always represents the filter from the parent node
        2. filter_code: Preserved as-is from the original query (not modified)
        3. Effective filter: filter_code if present, otherwise inherited_filter_code
        4. children_match_filter: For GROUP nodes, whether all children use the
           group's effective filter

        The innermost explicit filter always wins when determining the effective filter.

        Parameters
        ----------
        node : QueryNode
            The node to propagate filters from.
        parent_filter : FilterCode | None
            Filter inherited from the parent node.
        """
        # Set inherited filter from parent (regardless of explicit filter)
        node.inherited_filter_code = parent_filter

        if node.node_type == NodeType.TERM:
            # Terminal node: inherited_filter_code is already set
            pass
        elif node.node_type in (NodeType.ROOT, NodeType.GROUP):
            # Determine effective filter for this node (to pass to children)
            effective_filter: FilterCode | None = (
                node.filter_code if node.filter_code is not None else node.inherited_filter_code
            )

            # Propagate to children
            for child in node.children:
                self._propagate_filters(child, effective_filter)

            # For GROUP nodes, check if all children match the group's filter
            if node.node_type == NodeType.GROUP:
                node.children_match_filter = self._check_children_match_filter(node)

    def _check_children_match_filter(self, node: QueryNode) -> bool:
        """Check if all children use the same effective filter as this GROUP node.

        Parameters
        ----------
        node : QueryNode
            The GROUP node to check.

        Returns
        -------
        bool
            True if all children (recursively) use the group's effective filter.
        """
        # Group's effective filter (explicit or inherited)
        group_filter = (
            node.filter_code if node.filter_code is not None else node.inherited_filter_code
        )

        for child in node.children:
            if child.node_type == NodeType.CONNECTOR:
                continue
            elif child.node_type == NodeType.TERM:
                # Term's effective filter
                child_effective_filter = (
                    child.filter_code
                    if child.filter_code is not None
                    else child.inherited_filter_code
                )
                if child_effective_filter != group_filter:
                    return False
            elif child.node_type == NodeType.GROUP and not self._check_node_uses_filter(
                child, group_filter
            ):
                # Check if nested group and its children use the same filter
                return False

        return True

    def _check_node_uses_filter(self, node: QueryNode, target_filter: FilterCode | None) -> bool:
        """Recursively check if a node and all its children use the target effective filter.

        Parameters
        ----------
        node : QueryNode
            The node to check.
        target_filter : FilterCode | None
            The filter to match against.

        Returns
        -------
        bool
            True if the node and all descendants use the target filter.
        """
        if node.node_type == NodeType.CONNECTOR:
            return True
        elif node.node_type == NodeType.TERM:
            # Term's effective filter
            effective_filter = (
                node.filter_code if node.filter_code is not None else node.inherited_filter_code
            )
            return effective_filter == target_filter
        elif node.node_type in (NodeType.GROUP, NodeType.ROOT):
            for child in node.children:
                if not self._check_node_uses_filter(child, target_filter):
                    return False
            return True
        return True
