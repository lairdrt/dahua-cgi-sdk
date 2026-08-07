"""
Parsing utilities for Dahua CGI responses.
"""

from __future__ import annotations

from collections import defaultdict


def parse_cgi_properties(text: str) -> dict[str, str]:
    """
    Parse a Dahua CGI response consisting of key=value pairs.

    Blank lines are ignored.

    Keys are returned exactly as supplied by the recorder.
    """

    values: dict[str, str] = {}

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        values[key.strip()] = value.strip()

    return values


def parse_cgi_items(text: str) -> list[dict[str, str]]:
    """
    Parse indexed Dahua CGI items.

    Example::

        items[0].Channel=0
        items[0].StartTime=...
        items[1].Channel=1

    Returns:

        [
            {
                "Channel": "...",
                "StartTime": "...",
            },
            {
                "Channel": "...",
            },
        ]
    """

    groups: defaultdict[int, dict[str, str]] = defaultdict(dict)

    for line in text.splitlines():

        line = line.strip()

        if not line.startswith("items["):
            continue

        if "=" not in line:
            continue

        left, value = line.split("=", 1)

        try:
            prefix, field = left.split("].", 1)
            index = int(prefix[6:])

        except (ValueError, IndexError):
            continue

        groups[index][field] = value.strip()

    return [groups[index] for index in sorted(groups)]
