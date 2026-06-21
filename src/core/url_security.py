"""Small URL validation helpers."""

from __future__ import annotations

from urllib.parse import urlsplit


def url_has_allowed_host(
    url: str,
    allowed_host: str,
    *,
    allow_subdomains: bool = False,
) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    if parsed.scheme.lower() != "https":
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    hostname = hostname.rstrip(".").lower()
    allowed_host = allowed_host.rstrip(".").lower()

    return hostname == allowed_host or (
        allow_subdomains and hostname.endswith("." + allowed_host)
    )
