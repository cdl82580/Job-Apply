"""
scripts/ssrf.py — Shared SSRF guard for outbound-URL features (webhooks, etc).

Used both at admin-configuration time and again at delivery time (to guard
against DNS rebinding between the two checks).
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
    )
]


def is_ssrf_url(url: str) -> bool:
    """Return True if the URL resolves to a private/loopback/link-local address."""
    try:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.hostname or ""
        addrs  = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if any(ip in net for net in PRIVATE_NETS):
                return True
    except Exception:
        pass
    return False
