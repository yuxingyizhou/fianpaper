"""Abstract base class for connectors that support citation lookups.

Extends :class:`~findpapers.connectors.connector_base.ConnectorBase` with
the citation-specific contract: fetching the papers referenced by a given
paper (backward snowballing) and the papers that cite it (forward
snowballing).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from findpapers.core.paper import Paper

from findpapers.connectors.connector_base import ConnectorBase


class CitationConnectorBase(ConnectorBase):
    """Abstract base class for connectors that provide citation data.

    Subclasses implement one or both of the citation-lookup methods so
    that the snowball runner can traverse the citation graph through
    multiple data sources.

    A connector that only supports backward snowballing (references) should
    override :meth:`fetch_references` and leave :meth:`fetch_cited_by`
    returning an empty list, and set :attr:`supports_forward` to ``False``.
    Vice-versa for forward-only connectors.
    """

    #: Whether this connector supports backward snowballing (fetch_references).
    supports_backward: bool = True

    #: Whether this connector supports forward snowballing (fetch_cited_by).
    supports_forward: bool = True

    def get_expected_counts(self, paper: Paper) -> tuple[int | None, int | None]:
        """Return expected citation and reference counts for *paper*.

        Used by the snowball runner to display determinate progress bars.
        The default implementation returns ``(None, None)``.  Subclasses
        may override this to provide counts from the API or from the
        paper's metadata.

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
        return None, None

    def fetch_references(
        self,
        paper: Paper,
        progress_callback: Callable[[int], None] | None = None,
    ) -> list[Paper]:
        """Return papers cited *by* the given paper (backward snowballing).

        The default implementation returns an empty list.  Subclasses that
        support backward snowballing should override this method.

        Parameters
        ----------
        paper : Paper
            The paper whose references should be fetched.
            Must have a DOI to be resolved.
        progress_callback : Callable[[int], None] | None
            Optional callback invoked with the number of new papers
            fetched after each page/batch.  Used by the runner to
            update a progress bar.

        Returns
        -------
        list[Paper]
            Papers referenced by *paper*.  May be empty when the paper
            has no DOI, the API returns no data, or an error occurs.
        """
        return []

    def fetch_cited_by(
        self,
        paper: Paper,
        progress_callback: Callable[[int], None] | None = None,
    ) -> list[Paper]:
        """Return papers that cite the given paper (forward snowballing).

        The default implementation returns an empty list.  Subclasses that
        support forward snowballing should override this method.

        Parameters
        ----------
        paper : Paper
            The paper whose citing papers should be fetched.
            Must have a DOI to be resolved.
        progress_callback : Callable[[int], None] | None
            Optional callback invoked with the number of new papers
            fetched after each page/batch.  Used by the runner to
            update a progress bar.

        Returns
        -------
        list[Paper]
            Papers that cite *paper*.  May be empty when the paper has
            no DOI, the API returns no data, or an error occurs.
        """
        return []
