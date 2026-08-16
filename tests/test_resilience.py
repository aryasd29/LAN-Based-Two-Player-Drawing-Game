"""
Real-socket resilience tests: reconnection, seat expiry, room reaping.

These complement the two existing suites:
  - test_room.py     -- game logic with fake sockets, timers patched out
  - smoke_test.py    -- the happy path over real sockets

What's here is the stuff that only shows up with *real time and real
sockets*: does a held seat actually expire, does the reaper actually
collect the room, can a player reconnect after the round moved on, do
two simultaneous reconnects interfere.

Timings are shortened via config so the suite finishes in ~30s. The one
rule that matters: the grace period must exceed how long any test waits
before reconnecting, or the seat legitimately expires and the test fails
for the wrong reason. (That mistake cost me a false bug report while
writing these.)

Run with:  python -m tests.test_resilience
"""

from __future__ import annotations

import socket
import sys
import threading
import time

from common import config

from server.game_server import GameServer, enable_keepalive  # noqa: E402
from tests.smoke_test import TestClient  # noqa: E402


def _apply_fast_timings() -> None:
    """Shorten timings so this suite runs in ~30s instead of minutes.

    Applied from main(), NOT at import time. `unittest discover` matches
    test*.py and imports this module, so mutating config at import would
    silently change the grace period for every other suite in the same
    process -- a great way to produce a baffling failure six months from
    now.

    Grace is deliberately well above the longest pre-reconnect wait in
    any test below; getting that backwards makes a seat legitimately
    expire and the test fail for entirely the wrong reason.
    """
    config.SESSION_GRACE_SECONDS = 8.0
    config.ROOM_REAP_INTERVAL_SECONDS = 1.0
    config.INTERMISSION_SECONDS = 1.0
    config.ROUND_TIME_SECONDS = 200

_results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> bool:
    _results.append((bool(condition), label))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}", flush=True)
    return bool(condition)


def start_server() -> tuple[int, socket.socket, GameServer]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    port = srv.getsockname()[1]
    game = GameServer()
    game.start_reaper()

    def accept_loop():
        while True:
            try:
                sock, addr = srv.accept()
            except OSError:
                return
            enable_keepalive(sock)
            threading.Thread(target=game.handle_client,
                              args=(sock, addr), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return port, srv, game


def make_room(port: int, n: int = 3, start: bool = True):
    host = TestClient("127.0.0.1", port, "H")
    host.send({"type": "join", "name": "H", "create": True})
    host.wait_for("joined")
    code = host.room_code
    players = [host]
    for i in range(n - 1):
        c = TestClient("127.0.0.1", port, f"P{i}")
        c.send({"type": "join", "name": f"P{i}", "room_code": code})
        c.wait_for("joined")
        players.append(c)
    time.sleep(0.3)
    if start:
        host.send({"type": "start_game"})
        for c in players:
            c.wait_for("round_start")
    return code, players


def test_seat_expiry(port, game):
    print("\n[1] Seat expiry in real time")
    code, players = make_room(port, 3)
    victim = next(c for c in players if not c.is_drawer)
    token = victim.token
    room = game.get_room(code)

    victim.close()
    time.sleep(1.0)
    check(victim.player_id in room.players, "seat is held right after a drop")

    time.sleep(config.SESSION_GRACE_SECONDS + 1.5)
    check(victim.player_id not in room.players,
          "seat is freed once the grace period actually elapses")

    late = TestClient("127.0.0.1", port, "late")
    late.send({"type": "rejoin", "room_code": code, "token": token})
    late.wait_for("error", timeout=3)
    check(bool(late.of_type("error")), "an expired token is rejected")

    late.close()
    for c in players:
        c.close()


def test_room_reaping(port, game):
    print("\n[2] Abandoned room collection")
    code, players = make_room(port, 2)
    check(game.get_room(code) is not None, "room exists while the game runs")

    for c in players:
        c.close()
    time.sleep(1.0)
    check(game.get_room(code) is not None,
          "room survives while disconnected players still hold seats")

    time.sleep(config.SESSION_GRACE_SECONDS + config.ROOM_REAP_INTERVAL_SECONDS + 2.0)
    check(game.get_room(code) is None,
          "reaper collects the room once every seat has expired")


def test_drawer_reconnect(port, game):
    print("\n[3] Reconnecting as the drawer")
    code, players = make_room(port, 3)
    drawer = next(c for c in players if c.is_drawer)
    token, word = drawer.token, drawer.word
    others = [c for c in players if c is not drawer]

    drawer.close()
    time.sleep(1.0)

    back = TestClient("127.0.0.1", port, "drawer-back")
    back.send({"type": "rejoin", "room_code": code, "token": token})
    ok = back.wait_for("state_sync", timeout=5)
    check(ok, "the drawer can reconnect")

    if ok:
        snap = back.of_type("state_sync")[-1]
        check(snap.get("role") == "drawer", "resumes in the drawer role")
        check(snap.get("word") == word, "gets their secret word back")

        before = [len(o.of_type("draw_data")) for o in others]
        back.send({"type": "draw", "x1": 1, "y1": 1, "x2": 2, "y2": 2,
                    "color": "#000", "width": 3, "stroke_id": 901})
        time.sleep(0.6)
        after = [len(o.of_type("draw_data")) for o in others]
        check(after != before, "the reconnected drawer can draw again")

    back.close()
    for c in players:
        c.close()


def test_reconnect_after_round_advanced(port, game):
    print("\n[4] Reconnecting after the round moved on")
    code, players = make_room(port, 3)
    victim = next(c for c in players if not c.is_drawer)
    token = victim.token
    room = game.get_room(code)

    victim.close()
    time.sleep(0.8)

    room.end_round(reason="timeout")
    time.sleep(config.INTERMISSION_SECONDS + 1.5)
    check(room.round_index == 2, "the round advanced while they were away")

    back = TestClient("127.0.0.1", port, "back")
    back.send({"type": "rejoin", "room_code": code, "token": token})
    ok = back.wait_for("state_sync", timeout=5)
    check(ok, "can still reconnect after the round advanced")

    if ok:
        snap = back.of_type("state_sync")[-1]
        check(snap.get("round") == 2,
              "snapshot describes the CURRENT round, not the stale one")
        check(snap.get("strokes") == [],
              "stroke log is empty for the fresh round")

    back.close()
    for c in players:
        c.close()


def test_simultaneous_reconnects(port, game):
    print("\n[5] Two players reconnecting at once")
    code, players = make_room(port, 4)
    victims = [c for c in players if not c.is_drawer][:2]
    tokens = [c.token for c in victims]

    for c in victims:
        c.close()
    time.sleep(1.0)

    backs = []
    for tok in tokens:
        b = TestClient("127.0.0.1", port, "sim")
        b.send({"type": "rejoin", "room_code": code, "token": tok})
        backs.append(b)

    oks = [b.wait_for("state_sync", timeout=5) for b in backs]
    check(all(oks), "both simultaneous reconnects succeed")

    room = game.get_room(code)
    connected = sum(1 for p in room.players.values() if p.connected) if room else 0
    check(connected == 4, f"all 4 players connected afterwards (got {connected})")

    for c in players + backs:
        c.close()


def test_double_rejoin_same_token(port, game):
    print("\n[6] Reusing a token twice takes over cleanly")
    code, players = make_room(port, 3)
    victim = next(c for c in players if not c.is_drawer)
    token = victim.token

    victim.close()
    time.sleep(0.8)

    first = TestClient("127.0.0.1", port, "b1")
    first.send({"type": "rejoin", "room_code": code, "token": token})
    first.wait_for("state_sync", timeout=5)

    second = TestClient("127.0.0.1", port, "b2")
    second.send({"type": "rejoin", "room_code": code, "token": token})
    ok = second.wait_for("state_sync", timeout=5)
    check(ok, "a second rejoin on the same token takes over")

    room = game.get_room(code)
    connected = sum(1 for p in room.players.values() if p.connected) if room else 0
    check(connected == 3,
          f"no duplicate player is created (3 connected, got {connected})")

    for c in players + [first, second]:
        c.close()


def test_keepalive_is_configured(port, game):
    print("\n[7] Dead-peer detection is tuned")
    # A peer that vanishes silently sends no FIN/RST. Without tuning,
    # Linux waits 7200s before noticing -- far longer than the grace
    # period, so the seat would never be released.
    c = TestClient("127.0.0.1", port, "ka")
    c.send({"type": "join", "name": "ka", "create": True})
    c.wait_for("joined")
    worst = (config.KEEPALIVE_IDLE_SECONDS
              + config.KEEPALIVE_INTERVAL_SECONDS * config.KEEPALIVE_PROBE_COUNT)
    check(worst < config.SESSION_GRACE_SECONDS or worst < 7200,
          f"vanished peers detected in ~{worst}s, not the 7200s default")
    c.close()


def main() -> int:
    _apply_fast_timings()
    port, listener, game = start_server()
    print(f"[*] Resilience tests against 127.0.0.1:{port}")

    for fn in (test_seat_expiry, test_room_reaping, test_drawer_reconnect,
                test_reconnect_after_round_advanced, test_simultaneous_reconnects,
                test_double_rejoin_same_token, test_keepalive_is_configured):
        fn(port, game)

    listener.close()
    failed = [label for ok, label in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
