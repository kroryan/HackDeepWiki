"""Outbound network policy for crawlers and security scanners.

The desktop profile intentionally permits private/LAN targets because scanning
one's own services is a core local use case. A trusted-LAN deployment defaults
to public destinations only, preventing the server from becoming an
internal-network proxy.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from api.settings import DeploymentProfile, get_settings


class OutboundTargetRejected(ValueError):
    pass


def _policy() -> str:
    configured = os.environ.get("HACKDEEPWIKI_EGRESS_POLICY", "").strip().lower()
    if configured:
        if configured not in {"any", "public"}:
            raise ValueError("HACKDEEPWIKI_EGRESS_POLICY must be 'any' or 'public'")
        return configured
    return (
        "public"
        if get_settings().deployment_profile is DeploymentProfile.TRUSTED_LAN
        else "any"
    )


def validate_outbound_url(url: str) -> str:
    """Validate scheme, credentials and resolved addresses for one HTTP URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OutboundTargetRejected("Only absolute HTTP/HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise OutboundTargetRejected("Credentials embedded in URLs are not allowed")
    if _policy() == "any":
        return url

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise OutboundTargetRejected("Target hostname could not be resolved") from exc
    if not addresses:
        raise OutboundTargetRejected("Target hostname did not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise OutboundTargetRejected(
                "Private, loopback, link-local and reserved targets are blocked "
                "by the current egress policy"
            )
    return url
