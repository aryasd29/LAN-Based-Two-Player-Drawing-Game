"""
End-to-end smoke test over real TCP sockets (no GUI).

The unit tests in test_room.py use fake sockets to test game logic in
isolation. This complements them by exercising the actual network path:
real sockets, real threads, real framing, real server dispatch. It runs
headless, so CI can execute it without a display server.

Run with:  python -m tests.smoke_test
Exits non-zero on failure so CI fails loudly.
"""

from __future__ import annotations

import socket
import sys
import threading
import time

from common import config
from common.protocol import receive_messages, send_message
from server.game_server import GameServer, enable_keepalive


class TestClient:
    def __init__(self, host, port, name):
        self.name = name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.messages: list[dict] = []
        self.lock = threading.Lock()
        self.player_id = None
        self.room_code = None
        self.token = None
        self.is_drawer = False
        self.word = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            for msg in receive_messages(self.sock, config.RECV_BUFFER_SIZE):
                with self.lock:
                    self.messages.append(msg)
                t = msg.get("type")
                if t == "joined":
                    self.player_id = msg["player_id"]
                    self.room_code = msg["room_code"]
                    if msg.get("token"):
                        self.token = msg["token"]
                elif t == "round_start":
                    self.is_drawer = msg.get("role") == "drawer"
                    self.word = msg.get("word")
        except Exception:
            return

    def send(self, msg):
        send_message(self.sock, msg)

    def types(self):
        with self.lock:
            return [m["type"] for m in self.messages]

    def of_type(self, t):
        with self.lock:
            return [m for m in self.messages if m["type"] == t]

    def wait_for(self, msg_type, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.of_type(msg_type):
                return True
            time.sleep(0.02)
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def start_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(32)
    port = s.getsockname()[1]
    game = GameServer()

    def accept():
        while True:
            try:
                sock, addr = s.accept()
            except OSError:
                return
            enable_keepalive(sock)
            threading.Thread(target=game.handle_client,
                              args=(sock, addr), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return port, s


def check(condition, label):
    if condition:
        print(f"  PASS  {label}")
        return True
    print(f"  FAIL  {label}")
    return False


def main() -> int:
    port, listener = start_server()
    failures = 0
    print(f"[*] Smoke test against 127.0.0.1:{port}\n")

    # --- room creation and joining ---
    host = TestClient("127.0.0.1", port, "Host")
    host.send({"type": "join", "name": "Host", "create": True})
    if not host.wait_for("joined"):
        print("  FAIL  host could not join")
        return 1
    code = host.room_code
    failures += not check(bool(code), f"host created room (code={code})")

    others = []
    for i in range(3):
        c = TestClient("127.0.0.1", port, f"P{i}")
        c.send({"type": "join", "name": f"P{i}", "room_code": code})
        c.wait_for("joined")
        others.append(c)
    everyone = [host] + others

    time.sleep(0.3)
    lobby = host.of_type("lobby_update")[-1]
    failures += not check(len(lobby["players"]) == 4, "4 players in lobby")

    # --- wrong room code is rejected ---
    stranger = TestClient("127.0.0.1", port, "Stranger")
    stranger.send({"type": "join", "name": "Stranger", "room_code": "ZZZZ"})
    stranger.wait_for("error", timeout=2)
    failures += not check(bool(stranger.of_type("error")),
                           "bad room code returns an error")
    stranger.close()

    # --- non-host cannot start ---
    others[0].send({"type": "start_game"})
    others[0].wait_for("error", timeout=2)
    failures += not check(bool(others[0].of_type("error")),
                           "non-host cannot start the game")

    # --- host starts ---
    host.send({"type": "start_game"})
    for c in everyone:
        c.wait_for("round_start")
    drawers = [c for c in everyone if c.is_drawer]
    failures += not check(len(drawers) == 1, "exactly one drawer assigned")
    failures += not check(
        sum(1 for c in everyone if c.word) == 1,
        "only the drawer receives the word",
    )

    drawer = drawers[0]
    guessers = [c for c in everyone if not c.is_drawer]

    # --- drawing fans out to guessers only ---
    drawer.send({"type": "draw", "x1": 1, "y1": 2, "x2": 3, "y2": 4,
                  "color": "#000", "width": 3, "stroke_id": 1})
    time.sleep(0.4)
    failures += not check(
        all(g.of_type("draw_data") for g in guessers),
        "stroke reached every guesser",
    )
    failures += not check(not drawer.of_type("draw_data"),
                           "stroke not echoed back to the drawer")

    # --- a guesser cannot draw ---
    before = [len(g.of_type("draw_data")) for g in guessers]
    guessers[0].send({"type": "draw", "x1": 9, "y1": 9, "x2": 9, "y2": 9,
                       "color": "#000", "width": 3, "stroke_id": 99})
    time.sleep(0.4)
    failures += not check(
        [len(g.of_type("draw_data")) for g in guessers] == before,
        "non-drawer strokes are rejected",
    )

    # --- undo relays ---
    drawer.send({"type": "undo", "stroke_id": 1})
    time.sleep(0.4)
    failures += not check(
        all(g.of_type("undo_stroke") for g in guessers),
        "undo relayed to every guesser",
    )

    # --- wrong guess is private ---
    counts_before = [len(c.messages) for c in everyone if c is not guessers[0]]
    guessers[0].send({"type": "guess", "guess": "not-the-word"})
    time.sleep(0.4)
    counts_after = [len(c.messages) for c in everyone if c is not guessers[0]]
    failures += not check(counts_before == counts_after,
                           "wrong guess is not broadcast to others")

    # --- correct guess scores and notifies ---
    guessers[0].send({"type": "guess", "guess": drawer.word})
    time.sleep(0.5)
    correct = [m for m in guessers[0].of_type("guess_result")
                if m.get("result") == "correct"]
    failures += not check(bool(correct), "correct guess acknowledged")
    failures += not check(
        bool(correct) and correct[0].get("gained", 0) > 0,
        "correct guess awarded points",
    )
    failures += not check(
        all(g.of_type("player_guessed") for g in guessers[1:]),
        "other guessers notified without the word",
    )
    spoiled = any("word" in m for g in guessers[1:]
                   for m in g.of_type("player_guessed"))
    failures += not check(not spoiled,
                           "the word is not leaked to still-guessing players")

    su = host.of_type("score_update")
    failures += not check(bool(su) and len(su[-1]["scores"]) == 4,
                           "score update lists all players")

    # --- disconnect handling ---
    # Poll rather than sleeping a fixed amount: the server only notices
    # the drop when its recv() returns empty, and how quickly that
    # happens depends on OS scheduling, so a fixed sleep is flaky.
    guessers[-1].close()
    failures += not check(host.wait_for("player_left", timeout=5),
                           "remaining players told when someone leaves")

    # --- reconnection: the differentiating feature ---
    # Rebuild a clean 3-player game so the earlier disconnect doesn't
    # muddy what we're asserting here.
    for c in everyone:
        c.close()
    time.sleep(0.3)

    rh = TestClient("127.0.0.1", port, "RHost")
    rh.send({"type": "join", "name": "RHost", "create": True})
    rh.wait_for("joined")
    rcode = rh.room_code
    rplayers = [rh]
    for i in range(2):
        c = TestClient("127.0.0.1", port, f"R{i}")
        c.send({"type": "join", "name": f"R{i}", "room_code": rcode})
        c.wait_for("joined")
        rplayers.append(c)
    time.sleep(0.3)
    rh.send({"type": "start_game"})
    for c in rplayers:
        c.wait_for("round_start")

    failures += not check(all(c.token for c in rplayers),
                           "every player receives a session token")

    rdrawer = next(c for c in rplayers if c.is_drawer)
    victim = next(c for c in rplayers if not c.is_drawer)
    victim_token = victim.token

    for i in range(3):
        rdrawer.send({"type": "draw", "x1": i, "y1": i, "x2": i + 1, "y2": i + 1,
                       "color": "#000", "width": 3, "stroke_id": i})
    time.sleep(0.3)

    victim.close()          # hard socket kill
    time.sleep(0.6)

    for i in range(10, 15):  # drawing continues while they're away
        rdrawer.send({"type": "draw", "x1": i, "y1": i, "x2": i + 1, "y2": i + 1,
                       "color": "#f00", "width": 5, "stroke_id": i})
    time.sleep(0.4)

    returned = TestClient("127.0.0.1", port, "returned")
    returned.send({"type": "rejoin", "room_code": rcode, "token": victim_token})
    resumed = returned.wait_for("state_sync", timeout=5)
    failures += not check(resumed, "dropped player can resume with their token")

    if resumed:
        snap = returned.of_type("state_sync")[-1]
        failures += not check(
            returned.of_type("joined")[-1].get("resumed") is True,
            "resumed session is flagged as a resume, not a fresh join",
        )
        failures += not check(len(snap.get("strokes", [])) == 8,
                               "snapshot replays strokes drawn before AND during the outage")
        failures += not check(snap.get("role") == "guesser",
                               "resumed player keeps their role")
        failures += not check("word" not in snap and snap.get("word_length", 0) > 0,
                               "resume does not leak the word to a guesser")
        failures += not check(snap.get("time_left", 0) > 0,
                               "snapshot carries the live round timer")
        failures += not check(len(snap.get("scores", [])) == 3,
                               "snapshot carries the full scoreboard")
        failures += not check(bool(rdrawer.of_type("player_reconnected")),
                               "others are told the player came back")

        returned.send({"type": "guess", "guess": rdrawer.word})
        time.sleep(0.5)
        scored = [m for m in returned.of_type("guess_result")
                   if m.get("result") == "correct"]
        failures += not check(bool(scored),
                               "resumed player can still play and score")

    # A bad token must not grant a session.
    imposter = TestClient("127.0.0.1", port, "imposter")
    imposter.send({"type": "rejoin", "room_code": rcode, "token": "bogus-token"})
    imposter.wait_for("error", timeout=3)
    failures += not check(bool(imposter.of_type("error")),
                           "invalid session token is rejected")
    imposter.close()

    # --- late join into a running game ---
    latecomer = TestClient("127.0.0.1", port, "Latecomer")
    latecomer.send({"type": "join", "name": "Latecomer", "room_code": rcode})
    got = latecomer.wait_for("state_sync", timeout=5)
    failures += not check(got, "a new player can join a game in progress")
    if got:
        failures += not check(
            latecomer.of_type("state_sync")[-1].get("state") == "playing",
            "late joiner is synced into the running round",
        )
    latecomer.close()

    for c in rplayers + [returned]:
        c.close()
    listener.close()

    print()
    if failures:
        print(f"[!] {failures} check(s) FAILED")
        return 1
    print("[*] All smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
