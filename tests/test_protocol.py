"""
Unit tests for common/protocol.py.

Uses a fake socket object instead of a real TCP connection so these run
instantly and deterministically -- no network, no ports, no threads.
"""

import json
import unittest

from common.protocol import ConnectionClosed, receive_messages, send_message


class FakeSocket:
    """Minimal stand-in for socket.socket, just enough for protocol.py."""

    def __init__(self, incoming_chunks=None):
        self.sent = b""
        self._chunks = list(incoming_chunks or [])

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _bufsize: int) -> bytes:
        if not self._chunks:
            return b""  # simulate peer closing the connection
        return self._chunks.pop(0)


class TestSendMessage(unittest.TestCase):
    def test_send_message_is_newline_terminated_json(self):
        sock = FakeSocket()
        send_message(sock, {"type": "ping"})
        self.assertEqual(sock.sent, b'{"type": "ping"}\n')


class TestReceiveMessages(unittest.TestCase):
    def test_single_message_single_recv(self):
        sock = FakeSocket([b'{"type": "a"}\n'])
        messages = list(self._collect(sock))
        self.assertEqual(messages, [{"type": "a"}])

    def test_message_split_across_multiple_recv_calls(self):
        # Simulates TCP splitting one send() across several recv()s --
        # the exact scenario the framing code exists to handle.
        raw = json.dumps({"type": "draw_data", "x1": 1}) + "\n"
        mid = len(raw) // 2
        sock = FakeSocket([raw[:mid].encode(), raw[mid:].encode()])
        messages = list(self._collect(sock))
        self.assertEqual(messages, [{"type": "draw_data", "x1": 1}])

    def test_multiple_messages_in_single_recv_call(self):
        # Simulates the peer sending two messages back-to-back before we
        # get a chance to read -- both should arrive in one recv().
        raw = (json.dumps({"n": 1}) + "\n" + json.dumps({"n": 2}) + "\n").encode()
        sock = FakeSocket([raw])
        messages = list(self._collect(sock))
        self.assertEqual(messages, [{"n": 1}, {"n": 2}])

    def test_malformed_line_is_skipped_not_fatal(self):
        raw = b"not json\n" + b'{"n": 1}\n'
        sock = FakeSocket([raw])
        messages = list(self._collect(sock))
        self.assertEqual(messages, [{"n": 1}])

    def test_connection_closed_raises(self):
        sock = FakeSocket([])  # recv() immediately returns b""
        with self.assertRaises(ConnectionClosed):
            list(receive_messages(sock))

    @staticmethod
    def _collect(sock):
        # receive_messages is an infinite generator that keeps calling
        # recv() until the peer closes the connection (matching real
        # socket behavior). These tests only care about the messages
        # produced from the queued chunks, so stop cleanly once the fake
        # socket runs out of data instead of propagating ConnectionClosed.
        try:
            yield from receive_messages(sock)
        except ConnectionClosed:
            return


if __name__ == "__main__":
    unittest.main()
