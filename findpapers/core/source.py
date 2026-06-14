"""Source domain model representing a publication venue."""

from __future__ import annotations

import contextlib
from enum import StrEnum
from typing import Any

from findpapers.exceptions import ModelValidationError

from ..utils.merge import merge_value


class SourceType(StrEnum):
    """Classification of academic publication sources.

    Each value represents a broad category of venue or platform
    where scholarly work is published or deposited.

    Attributes
    ----------
    JOURNAL : str
        Peer-reviewed periodicals with regular publication schedules
        (e.g. Nature, IEEE Transactions, PLOS ONE).
    CONFERENCE : str
        Conferences, symposia, and workshops that publish proceedings
        (e.g. NeurIPS, ACL, ICML, CVPR).
    BOOK : str
        Books, book series, edited volumes, and monographs
        (e.g. Lecture Notes in Computer Science, Springer Tracts).
    REPOSITORY : str
        Preprint servers and deposit platforms
        (e.g. arXiv, bioRxiv, SSRN, Zenodo).
    OTHER : str
        Sources that do not fit any of the above categories
        (e.g. technical reports, newsletters, institutional publications).
    """

    JOURNAL = "journal"
    CONFERENCE = "conference"
    BOOK = "book"
    REPOSITORY = "repository"
    OTHER = "other"


class Source:
    """Represents a source (journal, conference proceedings, or book)."""

    def __init__(
        self,
        title: str,
        isbn: str | None = None,
        issn: str | None = None,
        publisher: str | None = None,
        source_type: SourceType | None = None,
    ) -> None:
        """Create a Source instance.

        Parameters
        ----------
        title : str
            Source title.
        isbn : str | None
            Source ISBN.
        issn : str | None
            Source ISSN.
        publisher : str | None
            Source publisher name.
        source_type : SourceType | None
            Type of the source (journal, conference, book, repository, other).

        Raises
        ------
        ModelValidationError
            If title is empty.
        """
        if not title:
            raise ModelValidationError("Source's title cannot be null")

        self.title = title
        self.isbn = isbn
        self.issn = issn
        self.publisher = publisher
        self.source_type = source_type

    def __eq__(self, other: object) -> bool:
        """Check equality by title (case-insensitive).

        Parameters
        ----------
        other : object
            Object to compare against.

        Returns
        -------
        bool
            ``True`` if both are :class:`Source` with matching titles.
        """
        if not isinstance(other, Source):
            return NotImplemented
        return self.title.strip().lower() == other.title.strip().lower()

    def __hash__(self) -> int:
        """Return a hash based on the lowered title.

        Returns
        -------
        int
            Hash value.
        """
        return hash(self.title.strip().lower())

    def __repr__(self) -> str:
        """Return a developer-friendly representation.

        Returns
        -------
        str
            Representation string.
        """
        return f"Source(title={self.title!r})"

    def __str__(self) -> str:
        """Return a human-readable representation.

        Format: ``"Title (Publisher)"`` when a publisher is available,
        otherwise just the title.

        Returns
        -------
        str
            Friendly string.
        """
        if self.publisher:
            return f"{self.title} ({self.publisher})"
        return self.title

    def merge(self, source: Source) -> None:
        """Merge another source into this one.

        Parameters
        ----------
        source : Source
            Source to merge into this one.

        Returns
        -------
        None
        """
        # Merge scalar and collection fields using shared rules.
        self.title = merge_value(self.title, source.title)
        self.isbn = merge_value(self.isbn, source.isbn)
        self.issn = merge_value(self.issn, source.issn)
        self.publisher = merge_value(self.publisher, source.publisher)
        if self.source_type is None:
            self.source_type = source.source_type

    @classmethod
    def from_dict(cls, source_dict: dict) -> Source:
        """Create a Source from a dict.

        Parameters
        ----------
        source_dict : dict
            Source dictionary.

        Returns
        -------
        Source
            Source instance.

        Raises
        ------
        ModelValidationError
            If the title is missing.
        """
        title = source_dict.get("title")
        if not isinstance(title, str) or not title:
            raise ModelValidationError("Source's title cannot be null")
        raw_source_type = source_dict.get("source_type")
        source_type: SourceType | None = None
        if raw_source_type is not None:
            with contextlib.suppress(ValueError):
                source_type = SourceType(raw_source_type)

        return cls(
            title=title,
            isbn=source_dict.get("isbn"),
            issn=source_dict.get("issn"),
            publisher=source_dict.get("publisher"),
            source_type=source_type,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this Source to a plain dictionary.

        Returns
        -------
        dict[str, Any]
            Source data suitable for JSON serialization.
        """
        return {
            "title": self.title,
            "isbn": self.isbn,
            "issn": self.issn,
            "publisher": self.publisher,
            "source_type": self.source_type.value if self.source_type else None,
        }
