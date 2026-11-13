"""LAN discovery helpers for phone/tablet sync.

The host can be reached over a USB cable (``adb reverse``) or over the local
Wi-Fi / hotspot.  This module reports the addresses a phone can actually dial
and, when the optional ``zeroconf`` package is installed, advertises
``_samrecall._tcp.local`` so devices can find the laptop without typing an IP.

No packet is sent to the internet: interface addresses are read from the
kernel, and the UDP socket used for the primary route only selects a route.
"""
from __future__ import annotations

import fcntl
import os
import socket
import struct
import threading
from pathlib import Path

PORT = int(os.environ.get("RECALL_PORT", "8787"))
SERVICE_TYPE = "_samrecall._tcp.local."
SERVICE_NAME = "Recall Study._samrecall._tcp.local."
_mdns = None
_mdns_error = ""


def _ipv4_addresses() -> list[str]:
    """Return every non-loopback IPv4 address currently assigned to an interface."""
    addresses: list[str] = []
    try:
        for _index, name in socket.if_nameindex():
            request = struct.pack("256s", name[:15].encode())
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                try:
                    packed = fcntl.ioctl(probe.fileno(), 0x8915, request)  # SIOCGIFADDR
                except OSError:
                    continue
                address = socket.inet_ntoa(packed[20:24])
                if address and not address.startswith("127."):
                    addresses.append(address)
    except Exception:
        pass
    return sorted(set(addresses), key=lambda value: (not value.startswith("192.168."), value))


def primary_address() -> str:
    """Pick the address the default route would use, without sending a packet."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))
            return str(probe.getsockname()[0])
        except OSError:
            return ""


def lan_urls(port: int | None = None) -> list[str]:
    """Return dialable ``http://<lan-ip>:<port>`` URLs, primary route first."""
    chosen = int(port or PORT)
    primary = primary_address()
    addresses = _ipv4_addresses()
    if primary and primary in addresses:
        addresses.remove(primary)
        addresses.insert(0, primary)
    return [f"http://{address}:{chosen}" for address in addresses]


def start_mdns(port: int | None = None) -> dict:
    """Advertise the service on the LAN when ``zeroconf`` is installed."""
    global _mdns, _mdns_error
    if _mdns is not None:
        return mdns_status()
    try:
        from zeroconf import ServiceInfo, Zeroconf  # optional dependency
    except Exception as error:  # metered/offline installs keep the IP display
        _mdns_error = type(error).__name__
        return mdns_status()
    address = primary_address()
    if not address:
        _mdns_error = "no LAN address"
        return mdns_status()
    try:
        info = ServiceInfo(
            SERVICE_TYPE,
            SERVICE_NAME,
            addresses=[socket.inet_aton(address)],
            port=int(port or PORT),
            properties={"path": "/", "app": "recall"},
        )
        server = Zeroconf()
        server.register_service(info)
        _mdns = (server, info)
        _mdns_error = ""
    except Exception as error:
        _mdns_error = type(error).__name__
    return mdns_status()


def stop_mdns() -> None:
    """Withdraw the mDNS advertisement on shutdown."""
    global _mdns
    if _mdns is None:
        return
    server, info = _mdns
    try:
        server.unregister_service(info)
        server.close()
    except Exception:
        pass
    _mdns = None


def mdns_status() -> dict:
    """Describe whether discovery is advertised, and why not when it is not."""
    return {
        "advertised": _mdns is not None,
        "service": SERVICE_TYPE,
        "name": SERVICE_NAME,
        "error": _mdns_error,
    }


def network_info(port: int | None = None) -> dict:
    """Everything the dashboard and the phone need to reach this host."""
    chosen = int(port or PORT)
    urls = lan_urls(chosen)
    return {
        "hostname": socket.gethostname(),
        "port": chosen,
        "urls": urls,
        "primary": urls[0] if urls else f"http://127.0.0.1:{chosen}",
        "loopback": f"http://127.0.0.1:{chosen}",
        "mdns": mdns_status(),
        "secure_context_note": (
            "Service workers only install on localhost or HTTPS. Use adb reverse "
            "for an offline-capable install; plain LAN HTTP still supports sync."
        ),
    }
