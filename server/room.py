"""
A single game room: N players, one drawer per round, rotating.

This replaces the original two-player design, where the drawer was
selected as `clients[current_round % 2]` -- an assumption baked into the
round logic that made "support more players" a rewrite rather than a
config change. Here, the drawer rotates through `self.order`, which
works for any player count from MIN_PLAYERS to MAX_PLAYERS.

Concurrency model
-----------------
Every room has its own RLock. All state reads/writes happen under it.
Crucially, sockets are NEVER written to while the lock is held: methods
build up an "outbox" of (socket, message) pairs under the lock, release
it, then flush. A slow or half-dead client can block in sendall(), and
holding the room lock across that would stall every other player in the
room -- so the outbox pattern keeps lock hold times bounded by
computation, not by network I/O.

Round timers use a monotonically increasing `round_token`. A timer thread
captures the token when it starts and exits immediately if the token has
changed, which prevents a stale timer from a round that ended early
(everyone guessed) from firing into the next round.

Sessions and reconnection
-------------------------
A player's identity is a session token, NOT their socket. When a socket
dies mid-game the player is marked disconnected but keeps their seat,
score, and role for SESSION_GRACE_SECONDS. Reconnecting with the same
token swaps a fresh socket into the existing Player and replies with a
full state snapshot (scores, role, remaining time, and every stroke drawn
so far this round), so the client can rebuild exactly what it lost.

This is why the room keeps a per-round `stroke_log`: the server otherwise
has no reason to remember drawing content, but without it a returning
player would stare at a blank canvas for the rest of the round. The log
is bounded (MAX_STROKES_PER_ROUND) so a misbehaving client can't grow it
without limit, and it is cleared on every round start and canvas clear.

The same snapshot machinery is what makes joining a game already in
progress possible -- a brand-new player and a returning player have the
same problem (an empty client that needs catching up), so they get the
same solution.
"""

from __future__ import annotations

import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

from common import config
from common.protocol import send_message


@dataclass
class Player:
    player_id: str
    name: str
    # None while the player is disconnected but still holding their seat.
    sock: socket.socket | None
    token: str
    score: int = 0
    connected: bool = True
    # Rounds this player has drawn -- used to build the rotation so the
    # count stays fair even when players join or leave mid-game.
    times_drawn: int = field(default=0)
    # Monotonic time the socket dropped, or None while connected. Used to
    # expire the held seat once the grace period passes.
    disconnected_at: float | None = None


class Room:
    def __init__(self, code: str, word_pool: list[str] | None = None,
                 rng: random.Random | None = None):
        self.code = code
        self.lock = threading.RLock()
        self.players: dict[str, Player] = {}
        self.tokens: dict[str, str] = {}    # session token -> player_id
        self.order: list[str] = []          # join order; drawer rotation
        # Strokes drawn so far this round, replayed to reconnecting and
        # late-joining players. Cleared each round and on canvas clear.
        self.stroke_log: list[dict] = []
        self.host_id: str | None = None
        self.state = "lobby"                 # lobby | playing | finished
        self.round_index = 0
        self.total_rounds = 0
        self.current_word: str | None = None
        self.drawer_id: str | None = None
        self.correct_guessers: set[str] = set()
        self.round_started_at: float | None = None
        self.round_token = 0
        self._word_pool = word_pool if word_pool is not None else list(config.WORDS)
        self._rng = rng or random.Random()
        self._recent_words: list[str] = []

    # ------------------------------------------------------------------
    # Outbox helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flush(outbox: list[tuple[socket.socket, dict]]) -> None:
        """Send queued messages. Called with the room lock NOT held."""
        for sock, msg in outbox:
            try:
                send_message(sock, msg)
            except OSError:
                # The peer's own recv loop will notice and trigger
                # remove_player(); don't let one dead socket stop the
                # rest of the room from receiving this message.
                continue

    def _queue_all(self, outbox: list, msg: dict, exclude: str | None = None) -> None:
        """Queue a message for every *connected* player. Lock must be held.

        Players holding a seat while disconnected have sock=None, so they
        are skipped here; they receive the state they missed as a single
        snapshot when they reconnect rather than a replayed message
        backlog.
        """
        for pid, player in self.players.items():
            if pid != exclude and player.connected and player.sock is not None:
                outbox.append((player.sock, msg))

    # ------------------------------------------------------------------
    # Lobby
    # ------------------------------------------------------------------

    def add_player(self, player_id: str, name: str,
                   sock: socket.socket) -> tuple[bool, str, str]:
        """Add a brand-new player. Returns (accepted, reason, token).

        Joining mid-game is allowed: the new player is caught up with the
        same state snapshot a reconnecting player receives. They enter the
        rotation behind everyone else so they can't jump the queue to
        draw, and total_rounds is not extended -- they take part in
        whatever rounds remain.
        """
        outbox: list = []
        snapshot_needed = False
        with self.lock:
            if len(self.players) >= config.MAX_PLAYERS:
                return False, "This room is full.", ""
            if self.state == "finished":
                return False, "That game has already finished.", ""

            # Disambiguate duplicate display names so the scoreboard and
            # "X guessed it!" messages stay readable.
            existing = {p.name for p in self.players.values()}
            unique_name = name
            suffix = 2
            while unique_name in existing:
                unique_name = f"{name} ({suffix})"
                suffix += 1

            token = uuid.uuid4().hex
            player = Player(player_id, unique_name, sock, token)
            if self.state == "playing":
                # Behind everyone in the rotation: _next_drawer() picks the
                # minimum times_drawn, so a fresh 0 would let a latecomer
                # immediately cut the line.
                player.times_drawn = max(
                    (p.times_drawn for p in self.players.values()), default=0
                )
                snapshot_needed = True

            self.players[player_id] = player
            self.tokens[token] = player_id
            self.order.append(player_id)
            if self.host_id is None:
                self.host_id = player_id

            outbox.append((sock, {
                "type": "joined",
                "room_code": self.code,
                "player_id": player_id,
                "your_name": unique_name,
                "token": token,
            }))
            self._queue_all(outbox, self._lobby_state_msg())
            if snapshot_needed:
                outbox.append((sock, self._snapshot(player_id)))

        self._flush(outbox)
        return True, "", token

    def reattach(self, token: str, sock: socket.socket) -> tuple[bool, str, str]:
        """Resume an existing session on a new socket.

        Returns (ok, player_id_or_reason, name). The old socket is closed
        and replaced: a client often reconnects before the server's recv
        loop has noticed the previous connection died, so without evicting
        the stale socket the room would briefly hold two connections
        claiming to be the same player.
        """
        outbox: list = []
        with self.lock:
            player_id = self.tokens.get(token)
            if not player_id or player_id not in self.players:
                return False, "That session has expired.", ""

            player = self.players[player_id]
            old_sock = player.sock
            player.sock = sock
            player.connected = True
            player.disconnected_at = None

            outbox.append((sock, {
                "type": "joined",
                "room_code": self.code,
                "player_id": player_id,
                "your_name": player.name,
                "token": token,
                "resumed": True,
            }))
            outbox.append((sock, self._snapshot(player_id)))
            self._queue_all(outbox, {
                "type": "player_reconnected", "name": player.name,
            }, exclude=player_id)
            self._queue_all(outbox, self._lobby_state_msg())
            name = player.name

        # Close the stale socket outside the lock -- close() on a wedged
        # socket can block, and the outbox pattern exists precisely to
        # keep I/O off the critical section.
        if old_sock is not None and old_sock is not sock:
            try:
                old_sock.close()
            except OSError:
                pass

        self._flush(outbox)
        return True, player_id, name

    def _snapshot(self, player_id: str) -> dict:
        """Everything a client needs to rebuild its view. Lock must be held."""
        player = self.players.get(player_id)
        snap: dict = {
            "type": "state_sync",
            "state": self.state,
            "room_code": self.code,
            "host_id": self.host_id,
            "your_id": player_id,
            "scores": self._scores_payload(),
            "round": self.round_index,
            "total_rounds": self.total_rounds,
        }
        if self.state != "playing" or not player:
            return snap

        elapsed = time.monotonic() - (self.round_started_at or time.monotonic())
        drawer = self.players.get(self.drawer_id) if self.drawer_id else None
        is_drawer = player_id == self.drawer_id
        snap.update({
            "role": "drawer" if is_drawer else "guesser",
            "drawer_id": self.drawer_id,
            "drawer_name": drawer.name if drawer else "",
            "duration": config.ROUND_TIME_SECONDS,
            "time_left": max(0, int(config.ROUND_TIME_SECONDS - elapsed)),
            # Replay the drawing so a returning player doesn't sit in
            # front of a blank canvas for the rest of the round.
            "strokes": list(self.stroke_log),
            "already_guessed": player_id in self.correct_guessers,
        })
        if is_drawer:
            snap["word"] = self.current_word
        else:
            snap["word_length"] = len(self.current_word or "")
            # A player who already guessed correctly before dropping has
            # legitimately earned the word; withholding it would make the
            # reconnected view inconsistent with what they saw before.
            if player_id in self.correct_guessers:
                snap["word"] = self.current_word
        return snap

    def remove_player(self, player_id: str, sock: socket.socket | None = None) -> bool:
        """Handle a dropped socket. Returns True if the room is now empty.

        During a game the seat is HELD, not freed: the player is marked
        disconnected and keeps their score, role, and place in the
        rotation until the grace period expires. In the lobby there is no
        state worth preserving, so they're removed outright.

        `sock` guards against a late-firing recv loop from a socket that
        has already been replaced by a reconnect -- if the player's
        current socket isn't the one that died, this is a stale callback
        and must be ignored, or it would knock the freshly reconnected
        player straight back out.
        """
        outbox: list = []
        end_round_now = False
        with self.lock:
            player = self.players.get(player_id)
            if player is None:
                return not self.players
            if sock is not None and player.sock is not sock:
                return False  # stale disconnect for an already-replaced socket

            if self.state == "playing":
                player.connected = False
                player.sock = None
                player.disconnected_at = time.monotonic()

                self._queue_all(outbox, {
                    "type": "player_left", "name": player.name,
                    "temporary": True,
                })

                if self._connected_count() < config.MIN_PLAYERS:
                    self.state = "finished"
                    self.round_token += 1
                    self._queue_all(outbox, {
                        "type": "game_over",
                        "scores": self._scores_payload(),
                        "reason": "Not enough players to continue.",
                    })
                elif player_id == self.drawer_id:
                    # The drawer is gone; end the round rather than run
                    # the clock down on a canvas nobody is drawing on.
                    end_round_now = True

                self._queue_all(outbox, self._lobby_state_msg())
                token = player.token
                threading.Thread(
                    target=self._expire_seat, args=(player_id, token),
                    daemon=True,
                ).start()
                self._flush(outbox)
                if end_round_now:
                    self.end_round(reason="drawer_left")
                return False

            # Lobby or finished: nothing to preserve, drop them entirely.
            self._drop_player(player_id, outbox)
            empty = not self.players

        self._flush(outbox)
        return empty

    def _drop_player(self, player_id: str, outbox: list) -> None:
        """Permanently remove a player. Lock must be held."""
        player = self.players.pop(player_id, None)
        if player is None:
            return
        self.tokens.pop(player.token, None)
        if player_id in self.order:
            self.order.remove(player_id)
        self.correct_guessers.discard(player_id)
        if self.host_id == player_id:
            self.host_id = self.order[0] if self.order else None
        if self.players:
            self._queue_all(outbox, {"type": "player_left", "name": player.name})
            self._queue_all(outbox, self._lobby_state_msg())

    def _expire_seat(self, player_id: str, token: str) -> None:
        """Free a held seat if the player hasn't returned in time."""
        time.sleep(config.SESSION_GRACE_SECONDS)
        outbox: list = []
        with self.lock:
            player = self.players.get(player_id)
            # Still the same session, and still gone? Then give up on them.
            if player is None or player.token != token or player.connected:
                return
            self._drop_player(player_id, outbox)
        self._flush(outbox)

    def _connected_count(self) -> int:
        """Lock must be held."""
        return sum(1 for p in self.players.values() if p.connected)

    def is_abandoned(self) -> bool:
        """True when the room holds nobody who is still connected."""
        with self.lock:
            return self._connected_count() == 0

    def _lobby_state_msg(self) -> dict:
        """Lock must be held."""
        return {
            "type": "lobby_update",
            "state": self.state,
            "host_id": self.host_id,
            "connected_count": self._connected_count(),
            "min_players": config.MIN_PLAYERS,
            "max_players": config.MAX_PLAYERS,
            "players": [
                {"player_id": p.player_id, "name": p.name,
                 "score": p.score, "connected": p.connected}
                for p in self._ordered_players()
            ],
        }

    def _ordered_players(self) -> list[Player]:
        """Lock must be held."""
        return [self.players[pid] for pid in self.order if pid in self.players]

    def _scores_payload(self) -> list[dict]:
        """Lock must be held."""
        ranked = sorted(self._ordered_players(), key=lambda p: p.score, reverse=True)
        return [{"player_id": p.player_id, "name": p.name, "score": p.score}
                for p in ranked]

    # ------------------------------------------------------------------
    # Game flow
    # ------------------------------------------------------------------

    def start_game(self, requester_id: str) -> tuple[bool, str]:
        with self.lock:
            if requester_id != self.host_id:
                return False, "Only the host can start the game."
            if self.state == "playing":
                return False, "The game has already started."
            if self._connected_count() < config.MIN_PLAYERS:
                return False, f"Need at least {config.MIN_PLAYERS} players to start."

            self.state = "playing"
            self.round_index = 0
            # Every player draws the same number of times, so total rounds
            # scales with the room size instead of being hardcoded.
            self.total_rounds = config.ROUNDS_PER_PLAYER * len(self.players)
            for p in self.players.values():
                p.score = 0
                p.times_drawn = 0

        self.begin_round()
        return True, ""

    def begin_round(self) -> None:
        outbox: list = []
        with self.lock:
            if self.state != "playing":
                return
            if (self.round_index >= self.total_rounds
                    or self._connected_count() < config.MIN_PLAYERS):
                self.state = "finished"
                self._queue_all(outbox, {
                    "type": "game_over",
                    "scores": self._scores_payload(),
                })
                self._flush(outbox)
                return

            self.round_token += 1
            token = self.round_token
            self.correct_guessers.clear()
            self.stroke_log.clear()  # fresh canvas -> fresh replay log
            self.current_word = self._pick_word()
            self.drawer_id = self._next_drawer()
            if self.drawer_id is None:
                return
            self.players[self.drawer_id].times_drawn += 1
            self.round_started_at = time.monotonic()
            self.round_index += 1

            drawer = self.players[self.drawer_id]
            round_meta = {
                "round": self.round_index,
                "total_rounds": self.total_rounds,
                "drawer_id": self.drawer_id,
                "drawer_name": drawer.name,
                "duration": config.ROUND_TIME_SECONDS,
                "word_length": len(self.current_word),
            }
            outbox.append((drawer.sock, {
                "type": "round_start", "role": "drawer",
                "word": self.current_word, **round_meta,
            }))
            for pid, p in self.players.items():
                if pid != self.drawer_id and p.connected:
                    outbox.append((p.sock, {
                        "type": "round_start", "role": "guesser", **round_meta,
                    }))
            self._queue_all(outbox, {"type": "clear_canvas"})

        self._flush(outbox)
        threading.Thread(target=self._run_timer, args=(token,), daemon=True).start()

    def _pick_word(self) -> str:
        """Lock must be held. Avoids repeating recent words so a short
        game doesn't serve the same word twice."""
        candidates = [w for w in self._word_pool if w not in self._recent_words]
        if not candidates:
            self._recent_words.clear()
            candidates = list(self._word_pool)
        word = self._rng.choice(candidates)
        self._recent_words.append(word)
        if len(self._recent_words) > max(1, len(self._word_pool) // 2):
            self._recent_words.pop(0)
        return word

    def _next_drawer(self) -> str | None:
        """Lock must be held. Picks whoever has drawn least, breaking ties
        by join order. Using a counter instead of `order[round % n]` keeps
        the rotation fair when players join or leave mid-game.

        Only currently-connected players are eligible: a player holding a
        seat through the grace period keeps their score and place, but
        handing the drawer role to an empty chair would stall the round
        until the timer expired.
        """
        candidates = [p for p in self._ordered_players() if p.connected]
        if not candidates:
            return None
        fewest = min(p.times_drawn for p in candidates)
        for p in candidates:
            if p.times_drawn == fewest:
                return p.player_id
        return candidates[0].player_id

    def handle_guess(self, player_id: str, guess: str) -> None:
        outbox: list = []
        finish_round = False
        with self.lock:
            if self.state != "playing" or player_id == self.drawer_id:
                return
            player = self.players.get(player_id)
            if player is None or player_id in self.correct_guessers:
                return
            if not isinstance(guess, str) or not self.current_word:
                return

            if guess.strip().lower() != self.current_word.lower():
                outbox.append((player.sock, {
                    "type": "guess_result", "result": "wrong", "guess": guess,
                }))
                self._flush(outbox)
                return

            # Correct. Award base + speed bonus.
            elapsed = time.monotonic() - (self.round_started_at or time.monotonic())
            remaining_frac = max(0.0, 1.0 - elapsed / config.ROUND_TIME_SECONDS)
            bonus = int(round(config.GUESS_SPEED_BONUS_MAX * remaining_frac))
            gained = config.GUESS_BASE_POINTS + bonus
            player.score += gained
            self.correct_guessers.add(player_id)

            drawer = self.players.get(self.drawer_id) if self.drawer_id else None
            if drawer:
                drawer.score += config.DRAWER_POINTS_PER_CORRECT_GUESS

            outbox.append((player.sock, {
                "type": "guess_result", "result": "correct",
                "word": self.current_word, "gained": gained,
            }))
            # Everyone else is told *that* they got it, but not the word --
            # otherwise one correct guess would spoil the round for the
            # players still guessing.
            self._queue_all(outbox, {
                "type": "player_guessed",
                "name": player.name,
                "gained": gained,
            }, exclude=player_id)
            self._queue_all(outbox, {
                "type": "score_update", "scores": self._scores_payload(),
            })

            guessers = [p for p in self.players if p != self.drawer_id]
            if guessers and all(p in self.correct_guessers for p in guessers):
                finish_round = True

        self._flush(outbox)
        if finish_round:
            self.end_round(reason="all_guessed")

    def end_round(self, reason: str = "timeout") -> None:
        outbox: list = []
        with self.lock:
            if self.state != "playing":
                return
            self.round_token += 1  # invalidate any in-flight timer
            word = self.current_word
            self._queue_all(outbox, {
                "type": "round_end",
                "word": word,
                "reason": reason,
                "scores": self._scores_payload(),
            })
            game_finished = self.round_index >= self.total_rounds
        self._flush(outbox)

        if game_finished:
            final: list = []
            with self.lock:
                self.state = "finished"
                self._queue_all(final, {
                    "type": "game_over", "scores": self._scores_payload(),
                })
            self._flush(final)
        else:
            threading.Thread(target=self._delayed_next_round, daemon=True).start()

    def _delayed_next_round(self) -> None:
        time.sleep(config.INTERMISSION_SECONDS)
        self.begin_round()

    def _run_timer(self, token: int) -> None:
        """One thread per round. Exits immediately if its round already
        ended (token bumped), so a stale timer can't tick into the next
        round or end it prematurely."""
        for remaining in range(config.ROUND_TIME_SECONDS, 0, -1):
            with self.lock:
                if self.round_token != token or self.state != "playing":
                    return
            outbox: list = []
            with self.lock:
                self._queue_all(outbox, {
                    "type": "timer_update", "time_left": remaining,
                })
            self._flush(outbox)
            time.sleep(1)

        with self.lock:
            if self.round_token != token or self.state != "playing":
                return
        self.end_round(reason="timeout")

    # ------------------------------------------------------------------
    # Drawing relay
    # ------------------------------------------------------------------

    def relay_draw(self, player_id: str, msg: dict) -> None:
        outbox: list = []
        with self.lock:
            if self.state != "playing" or player_id != self.drawer_id:
                return  # only the current drawer may draw
            # Record for replay, but stop growing past the cap: beyond it
            # strokes still reach live players, they just won't appear for
            # someone who reconnects later. Bounded memory matters more
            # than a perfect replay of a pathologically long round.
            if len(self.stroke_log) < config.MAX_STROKES_PER_ROUND:
                self.stroke_log.append(dict(msg))
            self._queue_all(outbox, {"type": "draw_data", **msg}, exclude=player_id)
        self._flush(outbox)

    def relay_simple(self, player_id: str, msg: dict) -> None:
        """Relay clear/undo from the drawer only, keeping the replay log
        consistent with what live clients are showing."""
        outbox: list = []
        with self.lock:
            if self.state != "playing" or player_id != self.drawer_id:
                return
            if msg.get("type") == "clear_canvas":
                self.stroke_log.clear()
            elif msg.get("type") == "undo_stroke":
                sid = msg.get("stroke_id")
                self.stroke_log = [
                    s for s in self.stroke_log if s.get("stroke_id") != sid
                ]
            self._queue_all(outbox, msg, exclude=player_id)
        self._flush(outbox)

    def broadcast_chat(self, player_id: str, text: str) -> None:
        outbox: list = []
        with self.lock:
            player = self.players.get(player_id)
            if not player or not isinstance(text, str) or not text.strip():
                return
            # The drawer can't chat during a round -- it'd be trivial to
            # just type the word.
            if self.state == "playing" and player_id == self.drawer_id:
                return
            self._queue_all(outbox, {
                "type": "chat", "name": player.name, "text": text.strip()[:200],
            })
        self._flush(outbox)
