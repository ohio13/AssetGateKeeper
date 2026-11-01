"""A minimal Chrome DevTools Protocol client: just enough to drive a page.

This exists because the browser smoke test must fail on console errors and CSP
refusals, and neither is visible from ``--dump-dom``. It is deliberately small:
an RFC 6455 client (masked text frames, fragmentation, ping/pong, close) and a
JSON-RPC request/event pump. No third-party packages, no Node, no Playwright.

``tools/browser_smoke.py`` is the only caller.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
from typing import Any, Callable, Optional
from urllib.parse import urlsplit


class CDPError(RuntimeError):
    """The browser refused a command, or the socket died."""


class WebSocket:
    """Client side of RFC 6455, over one TCP socket, for text frames only."""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "http"):
            raise CDPError(f"not a websocket url: {url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("the browser closed the handshake")
            header += chunk
        status = header.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise CDPError(f"handshake refused: {status!r}")
        self._buffer = header.split(b"\r\n\r\n", 1)[1]

    # - framing - #

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])          # FIN + text
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("the browser closed the connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def recv_text(self) -> str:
        """One complete text message, skipping control frames."""
        chunks: list[bytes] = []
        while True:
            first, second = self._read(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            if second & 0x80:                # server frames must not be masked
                raise CDPError("masked frame from the browser")
            payload = self._read(length) if length else b""
            if opcode == 0x8:                # close
                raise CDPError("the browser closed the socket")
            if opcode == 0x9:                # ping -> pong
                self._control(0xA, payload)
                continue
            if opcode == 0xA:                # pong
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def _control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def close(self) -> None:
        try:
            self._control(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """JSON-RPC over one browser-level WebSocket, with flattened sessions."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.ws = WebSocket(url, timeout=timeout)
        self._next_id = 0
        self._events: list[dict] = []
        self._listeners: list[Callable[[dict], None]] = []

    def on(self, listener: Callable[[dict], None]) -> None:
        self._listeners.append(listener)

    def send(self, method: str, params: Optional[dict] = None,
             session_id: Optional[str] = None) -> dict:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method,
                                   "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self.ws.send_text(json.dumps(message))
        while True:
            reply = json.loads(self.ws.recv_text())
            if "id" in reply and reply["id"] == self._next_id:
                if "error" in reply:
                    raise CDPError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            for listener in self._listeners:
                listener(reply)

    def wait_for(self, method: str, session_id: Optional[str] = None,
                 timeout: float = 20.0) -> dict:
        """Block until an event arrives, running other listeners meanwhile."""
        self.ws.sock.settimeout(timeout)
        try:
            while True:
                reply = json.loads(self.ws.recv_text())
                if "id" in reply:
                    continue                 # a stray command reply; nothing waits on it
                if reply.get("method") == method and (
                        session_id is None or reply.get("sessionId") == session_id):
                    return reply.get("params", {})
                for listener in self._listeners:
                    listener(reply)
        finally:
            self.ws.sock.settimeout(30.0)

    def close(self) -> None:
        self.ws.close()
