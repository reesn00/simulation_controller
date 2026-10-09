from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class UrlPolicyError(ValueError):
    pass


def sanitize_audit_url(url: str, allowed_query_keys: Iterable[str] = ()) -> str:
    parsed = urlsplit(url)
    allowed = set(allowed_query_keys)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key in allowed])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


async def validate_public_url(url: str, *, allow_local_for_tests: bool = False) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UrlPolicyError("Only http/https URLs are allowed")
    if parsed.username or parsed.password:
        raise UrlPolicyError("URL credentials are forbidden")
    hostname = parsed.hostname
    if not hostname:
        raise UrlPolicyError("URL hostname is missing")
    if hostname.casefold() == "localhost" or "." not in hostname:
        if not allow_local_for_tests:
            raise UrlPolicyError("Local and single-label hostnames are forbidden")
        return
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlPolicyError(f"DNS resolution failed: {exc}") from exc
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global and not allow_local_for_tests:
            raise UrlPolicyError(f"Non-public address is forbidden: {address}")
