from __future__ import annotations

import re
import unicodedata


def normalize_search_term(value: str) -> str:
    """Return a case- and accent-insensitive sequence of Unicode word tokens."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"\w+", without_accents, flags=re.UNICODE))
