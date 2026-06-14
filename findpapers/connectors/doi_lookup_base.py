"""Abstract base class for connectors that support DOI-based paper lookup.

Extends :class:`~findpapers.connectors.connector_base.ConnectorBase` with
the DOI-lookup contract: fetching a single paper by its DOI identifier.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from findpapers.core.paper import Paper

from findpapers.connectors.connector_base import ConnectorBase


class DOILookupConnectorBase(ConnectorBase):
    """Abstract base class for connectors that can resolve a DOI to a Paper.

    Any connector that implements :meth:`fetch_paper_by_doi` should inherit
    from this class so that :class:`~findpapers.runners.get_runner.GetRunner`
    can accept it via a well-typed interface.
    """

    @abstractmethod
    def fetch_paper_by_doi(self, doi: str) -> Paper | None:
        """Fetch a single paper by its DOI.

        Parameters
        ----------
        doi : str
            Bare DOI identifier (e.g. ``"10.1038/nature12373"``).

        Returns
        -------
        Paper | None
            A populated :class:`~findpapers.core.paper.Paper`, or ``None``
            when the DOI is not found or cannot be resolved by this connector.
        """
