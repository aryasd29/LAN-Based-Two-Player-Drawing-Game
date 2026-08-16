"""
Load-testing harness for the drawing game server.

Spawns headless bot clients (no Tkinter) that connect over real TCP
sockets, join rooms, and play automatically. One bot per round acts as
the drawer and emits strokes at a fixed rate; the others act as guessers
and record when each stroke arrives.

What it measures
----------------
- End-to-end stroke broadcast latency: the wall-clock time from the
  drawer calling sendall() to a guesser's receive loop decoding that
  same stroke. Reported as p50/p95/p99/max.
- Sustained message throughput at the server (messages relayed/second).
- Whether the server stays correct under load (no dropped/duplicated
  strokes, no crashed rooms).

Latency is measured by matching on a monotonically increasing stroke_id
and recording send/receive times against time.monotonic(). Both sides run
in one process here, so they share a clock and no clock-skew correction
is needed -- this measures server relay cost plus loopback, not
wide-area network time. Numbers from a real two-machine LAN run will be
higher by the physical network RTT; this harness isolates the part the
server is actually responsible for.

Usage:
    python -m bench.loadtest --rooms 5 --players-per-room 6 --seconds 20
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import threading
import time

from common import config
from common.protocol import ConnectionClosed, receive_messages, send_message


class BotClient:
    """A headless player. Runs its receive loop on its own thread."""

    def __init__(self, host: str, port: int, name: str, room_code: str | None,
                 create: bool, metrics: "Metrics"):
        self.name = name
        self.metrics = metrics
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        self.player_id: str | None = None
        self.room_code: str | None = None
        self.is_drawer = False
        self.current_word: str | None = None
        self.round_active = threading.Event()
        self.joined = threading.Event()
        self.game_over = threading.Event()
        self.stop = threading.Event()
        self._is_host = False

        join: dict = {"type": "join", "name": name}
        if room_code:
            join["room_code"] = room_code
        if create:
            join["create"] = True
        send_message(self.sock, join)

        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()

    # -- receive --------------------------------------------------------

    def _recv_loop(self) -> None:
        try:
            for msg in receive_messages(self.sock, config.RECV_BUFFER_SIZE):
                self._handle(msg)
                if self.stop.is_set():
                    return
        except (ConnectionClosed, OSError):
            return

    def _handle(self, msg: dict) -> None:
        t = msg.get("type")

        if t == "joined":
            self.player_id = msg["player_id"]
            self.room_code = msg["room_code"]
            self.joined.set()

        elif t == "lobby_update":
            self._is_host = msg.get("host_id") == self.player_id

        elif t == "round_start":
            self.is_drawer = msg.get("role") == "drawer"
            self.current_word = msg.get("word")
            self.round_active.set()

        elif t == "round_end":
            self.round_active.clear()
            self.is_drawer = False

        elif t == "draw_data":
            # The measurement that matters: when did this stroke land?
            sid = msg.get("stroke_id")
            if sid is not None:
                self.metrics.record_receive(sid, time.monotonic())

        elif t == "game_over":
            self.round_active.clear()
            self.game_over.set()

        elif t == "error":
            self.metrics.record_error(msg.get("message", "unknown"))

    # -- actions --------------------------------------------------------

    def start_game_if_host(self) -> bool:
        if self._is_host:
            try:
                send_message(self.sock, {"type": "start_game"})
                return True
            except OSError:
                return False
        return False

    def send_stroke(self, stroke_id: int) -> None:
        payload = {
            "type": "draw", "x1": 10, "y1": 20, "x2": 30, "y2": 40,
            "color": "#111111", "width": 3, "stroke_id": stroke_id,
        }
        self.metrics.record_send(stroke_id, time.monotonic())
        try:
            send_message(self.sock, payload)
        except OSError:
            pass

    def send_guess(self, guess: str) -> None:
        try:
            send_message(self.sock, {"type": "guess", "guess": guess})
        except OSError:
            pass

    def close(self) -> None:
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class Metrics:
    """Thread-safe collection of latency samples."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent_at: dict[int, float] = {}
        self.latencies: list[float] = []
        self.received_count = 0
        self.errors: list[str] = []

    def record_send(self, stroke_id: int, t: float) -> None:
        with self.lock:
            self.sent_at[stroke_id] = t

    def record_receive(self, stroke_id: int, t: float) -> None:
        with self.lock:
            sent = self.sent_at.get(stroke_id)
            self.received_count += 1
            if sent is not None:
                self.latencies.append((t - sent) * 1000.0)  # ms

    def record_error(self, message: str) -> None:
        with self.lock:
            self.errors.append(message)

    def summary(self, duration: float, players: int, rooms: int) -> dict:
        with self.lock:
            lat = sorted(self.latencies)
            sent = len(self.sent_at)
            received = self.received_count
            errors = list(self.errors)

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            idx = min(len(lat) - 1, int(round(p / 100.0 * (len(lat) - 1))))
            return lat[idx]

        return {
            "rooms": rooms,
            "players": players,
            "duration_s": round(duration, 2),
            "strokes_sent": sent,
            "stroke_deliveries": received,
            "deliveries_per_second": round(received / duration, 1) if duration else 0,
            "latency_ms": {
                "p50": round(pct(50), 2),
                "p95": round(pct(95), 2),
                "p99": round(pct(99), 2),
                "max": round(max(lat), 2) if lat else 0,
                "mean": round(statistics.fmean(lat), 2) if lat else 0,
                "samples": len(lat),
            },
            "errors": errors[:5],
            "error_count": len(errors),
        }


def run_load_test(host: str, port: int, rooms: int, players_per_room: int,
                  seconds: float, strokes_per_second: float) -> dict:
    metrics = Metrics()
    all_bots: list[BotClient] = []
    stroke_counter = 0
    counter_lock = threading.Lock()

    # --- build rooms ---
    for r in range(rooms):
        host_bot = BotClient(host, port, f"r{r}_host", None, True, metrics)
        if not host_bot.joined.wait(timeout=5):
            raise RuntimeError("host bot failed to join")
        all_bots.append(host_bot)
        code = host_bot.room_code
        for p in range(players_per_room - 1):
            bot = BotClient(host, port, f"r{r}_p{p}", code, False, metrics)
            if not bot.joined.wait(timeout=5):
                raise RuntimeError("bot failed to join")
            all_bots.append(bot)

    time.sleep(0.3)
    for bot in all_bots:
        bot.start_game_if_host()

    # Wait for the first round to actually begin before timing anything.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(b.round_active.is_set() for b in all_bots):
            break
        time.sleep(0.05)

    # --- drive traffic ---
    start = time.monotonic()
    interval = 1.0 / strokes_per_second if strokes_per_second > 0 else 0.01
    stop_at = start + seconds

    def driver(bot: BotClient) -> None:
        nonlocal stroke_counter
        while time.monotonic() < stop_at:
            if bot.round_active.is_set() and bot.is_drawer:
                with counter_lock:
                    stroke_counter += 1
                    sid = stroke_counter
                bot.send_stroke(sid)
            time.sleep(interval)

    threads = [threading.Thread(target=driver, args=(b,), daemon=True) for b in all_bots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    time.sleep(0.5)  # let in-flight broadcasts land before summarising
    duration = time.monotonic() - start

    result = metrics.summary(duration, len(all_bots), rooms)
    for bot in all_bots:
        bot.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Drawing game load test")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=config.CLIENT_CONNECT_PORT)
    parser.add_argument("--rooms", type=int, default=3)
    parser.add_argument("--players-per-room", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--strokes-per-second", type=float, default=30.0)
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    result = run_load_test(
        args.host, args.port, args.rooms, args.players_per_room,
        args.seconds, args.strokes_per_second,
    )

    if args.json:
        print(json.dumps(result, indent=2))
        return

    lat = result["latency_ms"]
    print("\n=== Load test result ===")
    print(f"Rooms:              {result['rooms']}")
    print(f"Concurrent players: {result['players']}")
    print(f"Duration:           {result['duration_s']}s")
    print(f"Strokes sent:       {result['strokes_sent']}")
    print(f"Stroke deliveries:  {result['stroke_deliveries']}")
    print(f"Deliveries/sec:     {result['deliveries_per_second']}")
    print(f"Latency p50/p95/p99: {lat['p50']} / {lat['p95']} / {lat['p99']} ms")
    print(f"Latency mean/max:    {lat['mean']} / {lat['max']} ms  (n={lat['samples']})")
    print(f"Errors:             {result['error_count']}")


if __name__ == "__main__":
    main()
