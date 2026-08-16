"""
Wire protocol helpers shared by client and server.

The original code duplicated the exact same "buffer + split on newline"
framing logic in both server.py and client.py. That logic was already
correct (TCP is a byte stream, so a single send() is not guaranteed to
arrive as a single recv() -- newline-delimited JSON handles that properly)
but duplicating it is a maintenance risk: a future fix to one copy could
silently diverge from the other. This module is the single source of truth
for framing, used by both sides.

Framing scheme: each message is a JSON object followed by "\n". Messages
are never expected to contain literal newlines (json.dumps does not emit
unescaped newlines), so splitting on "\n" is safe.
"""

import json
import socket
from typing import Iterator


class ConnectionClosed(Exception):
    """Raised when the peer closes the connection (recv() returns b"")."""


def send_message(sock: socket.socket, message: dict) -> None:
    """Serialize `message` as JSON and send it, newline-terminated.

    Raises whatever socket.sendall raises (e.g. BrokenPipeError,
    ConnectionResetError) on failure -- callers are expected to handle
    disconnects, not this function.
    """
    payload = (json.dumps(message) + "\n").encode("utf-8")
    sock.sendall(payload)


def receive_messages(sock: socket.socket, bufsize: int = 4096) -> Iterator[dict]:
    """Yield decoded JSON messages from `sock` as they arrive.

    Handles TCP stream behavior correctly:
    - A single recv() may contain zero, one, or several complete messages
      (if the peer sent multiple messages before we read).
    - A single message may be split across multiple recv() calls.
    Both cases are handled via an accumulating buffer split on "\n".

    Malformed (non-JSON) lines are skipped rather than crashing the
    connection -- a corrupt/partial line shouldn't take down the game.

    Raises ConnectionClosed when the peer closes the connection.
    """
    buffer = ""
    while True:
        chunk = sock.recv(bufsize).decode("utf-8", errors="replace")
        if not chunk:
            raise ConnectionClosed()
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Skip malformed frames instead of killing the connection.
                continue
