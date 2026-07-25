"""Parse inbound WhatsApp text into a ParsedMessage.

Expected format::

    <end_odometer> <destination text> [private]

Examples::

    145230 Office
    145230 Office private
    145230 Kantoor Amsterdam #private -- lunch with client

Rules:
- First whitespace-delimited token must be the integer end odometer.
- The word ``private`` or tag ``#private`` (case-insensitive) anywhere marks
  the trip as private and is stripped from the destination.
- Text after ``--`` (or ``|``) is treated as a free-text note.
"""
from __future__ import annotations

import re

from .models import ParsedMessage

_PRIVATE_RE = re.compile(r"(?<!\w)#?private(?!\w)", re.IGNORECASE)
_NOTE_SPLIT_RE = re.compile(r"\s*(?:--|\|)\s*")


class ParseError(ValueError):
    """Raised when a message cannot be parsed into a trip."""


def parse_message(text: str) -> ParsedMessage:
    raw = text
    text = (text or "").strip()
    if not text:
        raise ParseError("Empty message. Send: <odometer> <destination> [private]")

    # Split off an optional free-text note.
    parts = _NOTE_SPLIT_RE.split(text, maxsplit=1)
    body = parts[0].strip()
    note = parts[1].strip() if len(parts) > 1 else ""

    tokens = body.split()
    if not tokens:
        raise ParseError("Missing odometer reading.")

    odo_token = tokens[0]
    if not odo_token.isdigit():
        raise ParseError(
            f"First value must be the odometer reading (a number), got '{odo_token}'."
        )
    end_odo = int(odo_token)

    remainder = " ".join(tokens[1:])
    is_private = bool(_PRIVATE_RE.search(remainder))
    destination = _PRIVATE_RE.sub("", remainder).strip()
    # tidy up leftovers: empty brackets/parens and stray punctuation
    destination = re.sub(r"[\(\[\{]\s*[\)\]\}]", "", destination)
    destination = re.sub(r"\s{2,}", " ", destination).strip(" -()[]{}")

    if not destination:
        # A bare "private" trip with no named destination is allowed.
        destination = "private" if is_private else ""
    if not destination:
        raise ParseError("Missing destination. Send: <odometer> <destination>.")

    return ParsedMessage(
        end_odo=end_odo,
        destination=destination,
        is_private=is_private,
        note=note,
        raw=raw,
    )
