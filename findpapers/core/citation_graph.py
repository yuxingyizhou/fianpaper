"""Citation graph model for snowballing operations.

A :class:`CitationGraph` captures the directed citation relationships between
academic papers discovered during a snowballing process.  Each node is a
:class:`~findpapers.core.paper.Paper` and each directed edge indicates that
one paper cites another.
"""

from __future__ import annotations

from typing import Any, Literal

from findpapers.core.paper import Paper
from findpapers.exceptions import InvalidParameterError
from findpapers.utils.version import package_version


class CitationEdge:
    """A directed citation relationship: *source* cites *target*.

    Parameters
    ----------
    source : Paper
        The citing paper.
    target : Paper
        The cited paper.
    """

    def __init__(self, source: Paper, target: Paper) -> None:
        """Create a CitationEdge.

        Parameters
        ----------
        source : Paper
            The citing paper.
        target : Paper
            The cited paper.
        """
        self.source = source
        self.target = target

    def to_dict(self) -> dict[str, Any]:
        """Serialize the edge to a dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary with ``source_doi``, ``source_title``,
            ``target_doi`` and ``target_title`` keys.
        """
        return {
            "source_doi": self.source.doi,
            "source_title": self.source.title,
            "target_doi": self.target.doi,
            "target_title": self.target.title,
        }


class CitationGraph:
    """A directed citation graph built by snowballing from seed papers.

    The graph contains a set of :class:`~findpapers.core.paper.Paper` nodes
    and :class:`CitationEdge` directed edges (``source`` → ``target`` means
    *source cites target*).

    Parameters
    ----------
    seed_papers : list[Paper]
        The initial papers from which the snowball started.
    max_depth : int
        Maximum traversal depth used during construction.
    direction : Literal["both", "backward", "forward"]
        The snowball direction(s) used during construction.
    """

    def __init__(
        self,
        seed_papers: list[Paper],
        max_depth: int,
        direction: Literal["both", "backward", "forward"],
    ) -> None:
        """Create a CitationGraph.

        Parameters
        ----------
        seed_papers : list[Paper]
            Initial seed papers.
        max_depth : int
            Maximum traversal depth.
        direction : Literal["both", "backward", "forward"]
            Snowball direction(s).

        Raises
        ------
        InvalidParameterError
            If *max_depth* is less than 1.
        """
        if max_depth < 1:
            raise InvalidParameterError(f"max_depth must be >= 1, got {max_depth}")
        self.seed_papers: list[Paper] = list(seed_papers)
        self.max_depth = max_depth
        self.direction = direction
        # All nodes in the graph, keyed by a unique identifier (DOI preferred,
        # falling back to title).
        self._nodes: dict[str, Paper] = {}
        self._edges: list[CitationEdge] = []
        # Set of (source_key, target_key) tuples for O(1) duplicate edge detection.
        self._edge_keys: set[tuple[str, str]] = set()
        # Adjacency dicts for O(1) neighbor lookups.
        # _forward_adj: source_key → list of target Papers  (references)
        # _backward_adj: target_key → list of source Papers (cited-by)
        self._forward_adj: dict[str, list[Paper]] = {}
        self._backward_adj: dict[str, list[Paper]] = {}
        # Track the depth at which each node was first discovered.
        self._node_depths: dict[str, int] = {}

        # Register seed nodes at depth 0.
        for paper in self.seed_papers:
            key = self._paper_key(paper)
            if key:
                self._nodes[key] = paper
                self._node_depths[key] = 0

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def nodes(self) -> list[Paper]:
        """Return all nodes in the graph.

        Returns
        -------
        list[Paper]
            All paper nodes.
        """
        return list(self._nodes.values())

    @property
    def edges(self) -> list[CitationEdge]:
        """Return all citation edges.

        Returns
        -------
        list[CitationEdge]
            Directed citation edges.
        """
        return list(self._edges)

    # ------------------------------------------------------------------
    # Graph construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _paper_key(paper: Paper) -> str | None:
        """Return a unique key for a paper, or ``None`` if not identifiable.

        Prefers DOI; falls back to lowercased title.

        Parameters
        ----------
        paper : Paper
            The paper to key.

        Returns
        -------
        str | None
            A unique string key, or ``None``.
        """
        if paper.doi:
            return paper.doi.strip().lower()
        if paper.title:
            return paper.title.strip().lower()
        return None

    def contains(self, paper: Paper) -> bool:
        """Check whether the graph already contains a paper.

        Parameters
        ----------
        paper : Paper
            Paper to check.

        Returns
        -------
        bool
            ``True`` if the paper (by DOI or title) is already in the graph.
        """
        key = self._paper_key(paper)
        return key is not None and key in self._nodes

    def add_node(self, paper: Paper, discovered_from: Paper) -> Paper:
        """Add a node to the graph (or merge with an existing entry).

        The node's depth is automatically computed as
        ``get_node_depth(discovered_from) + 1``.

        If the node already exists it is merged and the existing instance
        is returned.  Otherwise the new node is stored and returned.

        Parameters
        ----------
        paper : Paper
            Paper to add as a node.
        discovered_from : Paper
            The parent node from which *paper* was discovered.  Must
            already be in the graph so that its depth can be resolved.

        Returns
        -------
        Paper
            The canonical node instance in the graph.

        Raises
        ------
        InvalidParameterError
            If *discovered_from* is not in the graph.
        """
        parent_depth = self.get_node_depth(discovered_from)
        if parent_depth is None:
            raise InvalidParameterError(
                "discovered_from paper is not in the graph; add it first or pass it as a seed."
            )
        depth = parent_depth + 1

        key = self._paper_key(paper)
        if key is None:
            return paper

        if key in self._nodes:
            self._nodes[key].merge(paper)
            # Keep the shallowest depth.
            if depth < self._node_depths.get(key, depth + 1):
                self._node_depths[key] = depth
            return self._nodes[key]

        self._nodes[key] = paper
        self._node_depths[key] = depth
        return paper

    def add_edge(self, source: Paper, target: Paper) -> None:
        """Record a citation edge (``source`` cites ``target``).

        Both papers must already be present in the graph (via
        :meth:`add_node`).  Duplicate edges are silently ignored.

        Parameters
        ----------
        source : Paper
            The citing paper.
        target : Paper
            The cited paper.
        """
        # Resolve to canonical instances.
        source_key = self._paper_key(source)
        target_key = self._paper_key(target)
        if source_key is None or target_key is None:
            return

        canonical_source = self._nodes.get(source_key, source)
        canonical_target = self._nodes.get(target_key, target)

        # Prevent duplicate edges (O(1) lookup).
        edge_key = (source_key, target_key)
        if edge_key in self._edge_keys:
            return

        self._edge_keys.add(edge_key)
        self._edges.append(CitationEdge(source=canonical_source, target=canonical_target))

        # Update adjacency dicts for O(1) lookups.
        self._forward_adj.setdefault(source_key, []).append(canonical_target)
        self._backward_adj.setdefault(target_key, []).append(canonical_source)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_references(self, paper: Paper) -> list[Paper]:
        """Return papers cited *by* the given paper (backward direction).

        Parameters
        ----------
        paper : Paper
            The citing paper.

        Returns
        -------
        list[Paper]
            Papers that *paper* cites.
        """
        key = self._paper_key(paper)
        if key is None:
            return []
        return list(self._forward_adj.get(key, []))

    def get_cited_by(self, paper: Paper) -> list[Paper]:
        """Return papers that cite the given paper (forward direction).

        Parameters
        ----------
        paper : Paper
            The cited paper.

        Returns
        -------
        list[Paper]
            Papers that cite *paper*.
        """
        key = self._paper_key(paper)
        if key is None:
            return []
        return list(self._backward_adj.get(key, []))

    def get_node_depth(self, paper: Paper) -> int | None:
        """Return the traversal depth at which a node was first discovered.

        Seed nodes have depth 0.

        Parameters
        ----------
        paper : Paper
            Paper to query.

        Returns
        -------
        int | None
            Depth, or ``None`` if the node is not in the graph.
        """
        key = self._paper_key(paper)
        if key is None:
            return None
        return self._node_depths.get(key)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize the citation graph to a dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary with ``metadata``, ``nodes`` and ``edges`` keys.
        """
        return {
            "metadata": {
                "seed_papers": [{"doi": p.doi, "title": p.title} for p in self.seed_papers],
                "max_depth": self.max_depth,
                "direction": self.direction,
                "total_nodes": len(self._nodes),
                "total_edges": len(self._edges),
                "version": package_version(),
            },
            "nodes": [
                {
                    **paper.to_dict(),
                    "snowball_depth": self._node_depths.get(
                        self._paper_key(paper),  # type: ignore[arg-type]
                        -1,
                    ),
                }
                for paper in self._nodes.values()
            ],
            "edges": [edge.to_dict() for edge in self._edges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CitationGraph:
        """Reconstruct a CitationGraph from a dictionary.

        Accepts the format produced by :meth:`to_dict` (and by
        :func:`~findpapers.utils.persistence.save_to_json`).

        Parameters
        ----------
        data : dict
            Dictionary with ``"metadata"``, ``"nodes"`` and ``"edges"``
            keys.

        Returns
        -------
        CitationGraph
            Reconstructed instance.
        """
        metadata = data.get("metadata", {})
        direction = metadata.get("direction", "both")
        max_depth = metadata.get("max_depth", metadata.get("depth", 1))

        # Rebuild nodes keyed by DOI / title.
        nodes: dict[str, Paper] = {}
        node_depths: dict[str, int] = {}
        for node in data.get("nodes", []):
            paper = Paper.from_dict(node)
            key = (paper.doi or "").strip().lower() or (paper.title or "").strip().lower()
            if not key:
                continue
            nodes[key] = paper
            node_depths[key] = node.get("snowball_depth", -1)

        # Identify seeds (depth == 0).
        seed_papers = [p for k, p in nodes.items() if node_depths.get(k) == 0]

        graph = cls(seed_papers=[], max_depth=max_depth, direction=direction)
        graph._nodes = nodes
        graph._node_depths = node_depths
        graph.seed_papers = seed_papers

        # Rebuild edges.
        for edge_dict in data.get("edges", []):
            src_doi = (edge_dict.get("source_doi") or "").strip().lower()
            src_title = (edge_dict.get("source_title") or "").strip().lower()
            tgt_doi = (edge_dict.get("target_doi") or "").strip().lower()
            tgt_title = (edge_dict.get("target_title") or "").strip().lower()

            src_key = src_doi or src_title
            tgt_key = tgt_doi or tgt_title
            if src_key in nodes and tgt_key in nodes:
                graph._edges.append(CitationEdge(source=nodes[src_key], target=nodes[tgt_key]))
                graph._edge_keys.add((src_key, tgt_key))
                # Rebuild adjacency dicts.
                graph._forward_adj.setdefault(src_key, []).append(nodes[tgt_key])
                graph._backward_adj.setdefault(tgt_key, []).append(nodes[src_key])

        return graph

    @property
    def node_count(self) -> int:
        """Return the number of unique nodes in the graph.

        Returns
        -------
        int
            Number of paper nodes.
        """
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        """Return the number of citation edges.

        Returns
        -------
        int
            Number of directed edges.
        """
        return len(self._edges)
