"""Host-header policy shared by both local servers.

The one signal a DNS-rebinding attack cannot forge is the ``Host`` header: the
attacker's page makes the browser connect to *our* socket, so the peer address is
loopback and even the page's own Origin matches its Host. What the browser still
sends honestly is the name the user typed.

Round 2 got this wrong for real studios: allowing only the bind address and
loopback killed a dashboard reached by LAN IP or by the machine's name, which is
how most studios use it. The rule here keeps the defence and loses the false
positives:

* loopback names and the address we bound are always fine;
* an **IP literal** is always fine - a rebinding attack needs a DNS name it
  controls, and it cannot make the browser put an attacker-chosen IP in Host;
* this machine's own hostname/FQDN is fine;
* anything else must be named with --allow-host.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Iterable
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def host_name(header: str) -> str:
    """'example.com:8080' -> 'example.com'; brackets stripped, lowercased."""
    try:
        return (urlsplit("//" + header.strip()).hostname or "").lower()
    except ValueError:
        return ""


def is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _with_short_form(value: str) -> set[str]:
    value = str(value or "").strip().lower()
    if not value:
        return set()
    return {value, value.split(".", 1)[0]} if "." in value else {value}


#: socket.getfqdn() does a reverse DNS lookup, which on macOS can take many
#: seconds. It used to run on every request carrying a non-loopback Host
#: header, so such a request could hang (found by the macOS CI job, 9 Nov 2026).
#: It now runs once, in the background; until it answers, the short host name
#: from gethostname() (instant) is all that is matched.
_FQDN: set[str] = set()


def _resolve_fqdn() -> None:
    try:
        _FQDN.update(_with_short_form(socket.getfqdn()))
    except OSError:                          # pragma: no cover - exotic setups
        pass


threading.Thread(target=_resolve_fqdn, name="agk-fqdn", daemon=True).start()


def local_names() -> set[str]:
    """The names this machine answers to, best effort, never fatal, never slow."""
    names: set[str] = set(_FQDN)
    try:
        names |= _with_short_form(socket.gethostname())
    except OSError:                          # pragma: no cover - exotic setups
        pass
    return names


def host_allowed(header: str, bind_address: str = "",
                 allow_hosts: Iterable[str] = ()) -> bool:
    """Should a request carrying this Host header be answered at all?"""
    name = host_name(header)
    if not name:
        return False                         # HTTP/1.1 always sends one
    if name in LOOPBACK_NAMES:
        return True
    if is_ip_literal(name):
        return True
    allowed = {str(item).strip().lower() for item in allow_hosts if str(item).strip()}
    if name in allowed:
        return True
    # An allow-host entry may carry a port or be given as a full URL.
    for item in list(allowed):
        if host_name(item) == name and host_name(item):
            return True
    bound = str(bind_address or "").strip("[]").lower()
    if bound and name == host_name(bound):
        return True
    return name in local_names()


def refusal_detail(header: str) -> str:
    return (
        f"this server does not answer to the name {header or '(empty)'!r}. "
        "If you reach it by DNS name, start it with --allow-host <name>. "
        "Requests whose Host names something else are refused because that is "
        "how a DNS-rebinding page tries to drive a local dashboard."
    )
