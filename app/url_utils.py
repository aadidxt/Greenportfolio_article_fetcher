from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "source",
    "campaign",
}


def is_tracking_parameter(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS


def normalize_url(url: str, base_url: str | None = None) -> str:
    absolute = urljoin(base_url or "", url.strip())
    parts = urlsplit(absolute)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only public HTTP(S) article URLs are allowed")

    hostname = parts.hostname.casefold().rstrip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    port = parts.port
    if port and not (
        (parts.scheme.casefold() == "http" and port == 80)
        or (parts.scheme.casefold() == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"

    path = parts.path or "/"
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/":
        path = path.rstrip("/")

    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not is_tracking_parameter(key)
        ),
        doseq=True,
    )
    # Scheme is normalized so HTTP/HTTPS variants resolve to one duplicate key.
    return urlunsplit(("https", hostname, path, query, ""))


def article_id(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def validate_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Invalid HTTP(S) URL")
    hostname = parts.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("Local URLs are not allowed")
    try:
        addresses = socket.getaddrinfo(hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Article host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if any(
            (
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )
        ):
            raise ValueError("Private or reserved article hosts are not allowed")

