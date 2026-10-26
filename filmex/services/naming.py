"""Roll folder naming: render names from a template, slug the parts.

Template tokens: ``{date}`` (YYYY-MM-DD), ``{year}``, ``{month}``,
``{camera}``, ``{film}``, ``{lens}``, ``{place}``, ``{roll}``. Camera, film
and lens values are the entity's ``short_name`` when set, otherwise its
display name, slugged. ``{roll}`` is a per-year sequence number, zero-padded.

Slug rule: NFC, Unicode letters and digits kept (so ``Jāņos`` stays
``Jāņos``), characters that are illegal in file names dropped, whitespace
runs turned into ``-``, repeats collapsed. Rendered names are always NFC;
the share may store NFD, which is why comparisons normalize first.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TOKENS = ("date", "year", "month", "camera", "film", "lens", "place", "roll")
DEFAULT_TEMPLATE = "{date}-{camera}-{film}"
PLACE_SUFFIX = "-{place}"
ROLL_PAD = 2

_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")
_FORBIDDEN_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_DASH_RUN_RE = re.compile(r"-{2,}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OPTIONAL_PLACE_RE = re.compile(r"[-_ ]?\{place\}|\{place\}[-_ ]?")


class NamingError(ValueError):
    """Bad template or a token that cannot be filled."""


@dataclass(frozen=True)
class NameContext:
    date: str | None = None  # YYYY-MM-DD
    camera: str | None = None
    film: str | None = None
    lens: str | None = None
    place: str | None = None
    roll: int | None = None


def slug(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value)).strip()
    text = _FORBIDDEN_RE.sub("", text)
    text = _WHITESPACE_RE.sub("-", text)
    text = _DASH_RUN_RE.sub("-", text)
    return text.strip("-. ")


def short_name_for(display_name: str | None, short_name: str | None) -> str:
    """The slug used in folder names for a camera, film or lens."""
    return slug(short_name) or slug(display_name)


def tokens_in(template: str) -> list[str]:
    return _TOKEN_RE.findall(template or "")


def validate_template(template: str) -> str:
    """Return the cleaned template or raise ``NamingError``."""
    cleaned = (template or "").strip()
    if not cleaned:
        raise NamingError("template is empty")
    found = tokens_in(cleaned)
    unknown = [t for t in found if t not in TOKENS]
    if unknown:
        raise NamingError(f"unknown token(s): {', '.join('{' + t + '}' for t in unknown)}")
    if not found:
        raise NamingError("template has no tokens")
    if _FORBIDDEN_RE.search(cleaned):
        raise NamingError("template contains characters that are illegal in folder names")
    return cleaned


def is_date(value: str | None) -> bool:
    """True for a ``YYYY-MM-DD`` string, the only date form ``{date}`` takes."""
    return bool(value) and bool(_DATE_RE.match(value))


def render(
    template: str,
    ctx: NameContext,
    *,
    place_suffix: bool = True,
    roll_pad: int = ROLL_PAD,
) -> str:
    """Render a folder name. Raises ``NamingError`` when a required token is missing.

    ``{place}`` is optional: when the context has no place, the token and its
    adjacent separator disappear. With ``place_suffix`` and a template that
    does not mention ``{place}``, a set place is appended as ``-{place}``.
    """
    cleaned = validate_template(template)
    if place_suffix and "{place}" not in cleaned and ctx.place:
        cleaned = cleaned + PLACE_SUFFIX
    if not ctx.place:
        cleaned = _OPTIONAL_PLACE_RE.sub("", cleaned)

    if ctx.date is not None and not _DATE_RE.match(ctx.date):
        raise NamingError(f"date must be YYYY-MM-DD, got {ctx.date!r}")

    values: dict[str, str | None] = {
        "date": ctx.date,
        "year": ctx.date[:4] if ctx.date else None,
        "month": ctx.date[5:7] if ctx.date else None,
        "camera": slug(ctx.camera) or None,
        "film": slug(ctx.film) or None,
        "lens": slug(ctx.lens) or None,
        "place": slug(ctx.place) or None,
        "roll": f"{ctx.roll:0{roll_pad}d}" if ctx.roll is not None else None,
    }

    def fill(match: re.Match) -> str:
        token = match.group(1)
        value = values.get(token)
        if value is None:
            raise NamingError(f"missing value for {{{token}}}")
        return value

    name = _TOKEN_RE.sub(fill, cleaned)
    name = _DASH_RUN_RE.sub("-", name).strip("-. ")
    if not name:
        raise NamingError("rendered name is empty")
    return unicodedata.normalize("NFC", name)


def same_name(a: str, b: str) -> bool:
    """Equality that ignores NFC/NFD differences."""
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)
