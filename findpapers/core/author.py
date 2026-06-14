"""Author model representing an academic paper author."""

from __future__ import annotations

from typing import Any

from findpapers.exceptions import ModelValidationError


class Author:
    """Represents a paper author with name and optional affiliation.

    Parameters
    ----------
    name : str
        Author's full name.
    affiliation : str | None
        Author's institutional affiliation, if known.

    Raises
    ------
    ModelValidationError
        If name is empty or ``None``.
    """

    def __init__(self, name: str, affiliation: str | None = None) -> None:
        if not name or not name.strip():
            raise ModelValidationError("Author name cannot be empty")
        self.name: str = name.strip()
        self.affiliation: str | None = affiliation.strip() if affiliation else None

    def __repr__(self) -> str:
        """Return a developer-friendly representation.

        Returns
        -------
        str
            Representation string.
        """
        if self.affiliation:
            return f"Author(name={self.name!r}, affiliation={self.affiliation!r})"
        return f"Author(name={self.name!r})"

    def __str__(self) -> str:
        """Return the author name as a string.

        Returns
        -------
        str
            Author name.
        """
        return self.name

    def __eq__(self, other: object) -> bool:
        """Check equality by name (case-insensitive).

        Parameters
        ----------
        other : object
            Object to compare against.

        Returns
        -------
        bool
            ``True`` if both are :class:`Author` and names match
            (case-insensitive).
        """
        if not isinstance(other, Author):
            return NotImplemented
        return self.name.lower() == other.name.lower()

    def __hash__(self) -> int:
        """Return a hash based on the lowered name.

        Returns
        -------
        int
            Hash value.
        """
        return hash(self.name.lower())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the author to a dictionary.

        Returns
        -------
        dict[str, Any]
            Author data with ``"name"`` and optionally ``"affiliation"``.
        """
        result: dict[str, Any] = {"name": self.name}
        if self.affiliation:
            result["affiliation"] = self.affiliation
        return result

    @classmethod
    def from_dict(cls, data: dict) -> Author:
        """Create an Author from a dictionary.

        Parameters
        ----------
        data : dict
            Author data with ``"name"`` and optionally ``"affiliation"``.

        Returns
        -------
        Author
            Deserialized author instance.

        Raises
        ------
        ValueError
            If the name is missing or empty.
        """
        return cls(
            name=data.get("name", ""),
            affiliation=data.get("affiliation"),
        )
