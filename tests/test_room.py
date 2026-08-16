"""
Unit tests for server/room.py (N-player game logic).

Uses fake sockets so these run instantly with no real network. Timers and
intermission sleeps are patched out where a test would otherwise wait on
wall-clock time.
"""

import json
import random
import threading
import unittest
from unittest.mock import patch

from common import config
from server.room import Room


class FakeSocket:
    def __init__(self, name="client"):
        self.name = name
        self.sent_messages = []
        self._lock = threading.Lock()

    def sendall(self, data: bytes) -> None:
        with self._lock:
            for line in data.decode().strip().split("\n"):
                if line:
                    self.sent_messages.append(json.loads(line))

    def close(self):
        pass

    def types(self):
        return [m["type"] for m in self.sent_messages]

    def of_type(self, t):
        return [m for m in self.sent_messages if m["type"] == t]

    def __repr__(self):
        return f"FakeSocket({self.name})"


def make_room(n_players: int, seed: int = 1):
    """Build a room with n players already joined. Returns (room, socks)."""
    room = Room("TEST", rng=random.Random(seed))
    socks = []
    for i in range(n_players):
        s = FakeSocket(f"p{i}")
        accepted, _reason, _token = room.add_player(f"id{i}", f"Player{i}", s)
        assert accepted
        socks.append(s)
    return room, socks


class TestLobby(unittest.TestCase):
    def test_first_player_becomes_host(self):
        room, _ = make_room(1)
        self.assertEqual(room.host_id, "id0")

    def test_host_reassigned_when_host_leaves(self):
        room, _ = make_room(3)
        room.remove_player("id0")
        self.assertEqual(room.host_id, "id1")

    def test_room_rejects_players_beyond_max(self):
        room, _ = make_room(config.MAX_PLAYERS)
        extra = FakeSocket("extra")
        accepted, reason, _ = room.add_player("extra", "Extra", extra)
        self.assertFalse(accepted)
        self.assertIn("full", reason.lower())

    def test_duplicate_names_are_disambiguated(self):
        room = Room("TEST", rng=random.Random(1))
        a, b = FakeSocket("a"), FakeSocket("b")
        room.add_player("id0", "Arya", a)
        room.add_player("id1", "Arya", b)
        names = {p.name for p in room.players.values()}
        self.assertEqual(names, {"Arya", "Arya (2)"})

    def test_late_joiner_is_admitted_and_caught_up(self):
        # Mid-game joins used to be rejected. Now that the server can
        # produce a full state snapshot (built for reconnection), a
        # latecomer gets the same treatment: admitted, then synced.
        room, _ = make_room(2)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        late = FakeSocket("late")
        accepted, reason, token = room.add_player("late", "Late", late)

        self.assertTrue(accepted, reason)
        self.assertTrue(token)
        syncs = late.of_type("state_sync")
        self.assertEqual(len(syncs), 1)
        self.assertEqual(syncs[0]["state"], "playing")

    def test_late_joiner_cannot_jump_the_drawing_queue(self):
        # _next_drawer picks the minimum times_drawn, so a fresh player
        # starting at 0 would immediately cut ahead of everyone.
        room, _ = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
            room.add_player("late", "Late", FakeSocket("late"))
            room.end_round(reason="timeout")
            room.begin_round()

        self.assertNotEqual(room.drawer_id, "late")

    def test_cannot_join_a_finished_game(self):
        room, _ = make_room(2)
        with room.lock:
            room.state = "finished"
        accepted, reason, _ = room.add_player("late", "Late", FakeSocket("late"))
        self.assertFalse(accepted)
        self.assertIn("finished", reason.lower())

    def test_non_host_cannot_start_game(self):
        room, _ = make_room(3)
        ok, reason = room.start_game("id1")
        self.assertFalse(ok)
        self.assertIn("host", reason.lower())

    def test_cannot_start_below_min_players(self):
        room, _ = make_room(1)
        ok, reason = room.start_game("id0")
        self.assertFalse(ok)
        self.assertIn("at least", reason.lower())

    def test_empty_room_reported_when_last_player_leaves(self):
        room, _ = make_room(1)
        self.assertTrue(room.remove_player("id0"))


class TestRoundFlow(unittest.TestCase):
    def test_total_rounds_scales_with_player_count(self):
        # The core fix: round count is no longer hardcoded to 2 players.
        for n in (2, 3, 5, 8):
            room, _ = make_room(n)
            with patch("server.room.threading.Thread"):
                room.start_game("id0")
            self.assertEqual(room.total_rounds, config.ROUNDS_PER_PLAYER * n)

    def test_exactly_one_drawer_per_round(self):
        room, socks = make_room(5)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        roles = []
        for s in socks:
            starts = s.of_type("round_start")
            self.assertEqual(len(starts), 1)
            roles.append(starts[0]["role"])
        self.assertEqual(roles.count("drawer"), 1)
        self.assertEqual(roles.count("guesser"), 4)

    def test_only_drawer_receives_the_word(self):
        room, socks = make_room(4)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        with_word = [s for s in socks if "word" in s.of_type("round_start")[0]]
        self.assertEqual(len(with_word), 1)

    def test_drawer_rotates_across_rounds(self):
        room, socks = make_room(4)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
            drawers = [room.drawer_id]
            for _ in range(3):
                room.end_round(reason="timeout")
                room.begin_round()
                drawers.append(room.drawer_id)
        # Every player should have drawn once before anyone draws twice.
        self.assertEqual(len(set(drawers)), 4)

    def test_correct_guess_scores_guesser_and_drawer(self):
        room, socks = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        guesser_id = next(p for p in room.players if p != drawer_id)

        with patch("server.room.threading.Thread"):
            room.handle_guess(guesser_id, room.current_word)

        self.assertGreaterEqual(room.players[guesser_id].score, config.GUESS_BASE_POINTS)
        self.assertEqual(room.players[drawer_id].score,
                          config.DRAWER_POINTS_PER_CORRECT_GUESS)

    def test_wrong_guess_is_private_and_scores_nothing(self):
        room, socks = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        guesser_id = next(p for p in room.players if p != drawer_id)
        guesser_sock = next(s for s, pid in zip(socks, ["id0", "id1", "id2"])
                             if pid == guesser_id)
        others = [s for s in socks if s is not guesser_sock]
        before = [len(s.sent_messages) for s in others]

        room.handle_guess(guesser_id, "definitely-wrong")

        self.assertEqual(guesser_sock.of_type("guess_result")[-1]["result"], "wrong")
        self.assertEqual([len(s.sent_messages) for s in others], before)
        self.assertEqual(room.players[guesser_id].score, 0)

    def test_drawer_cannot_guess_own_word(self):
        room, _ = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        room.handle_guess(drawer_id, room.current_word)
        self.assertEqual(room.players[drawer_id].score, 0)

    def test_guessing_twice_scores_only_once(self):
        room, _ = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        guesser_id = next(p for p in room.players if p != drawer_id)
        word = room.current_word

        with patch("server.room.threading.Thread"):
            room.handle_guess(guesser_id, word)
            first = room.players[guesser_id].score
            room.handle_guess(guesser_id, word)

        self.assertEqual(room.players[guesser_id].score, first)

    def test_other_guessers_are_not_told_the_word(self):
        # A correct guess must not spoil the round for players still guessing.
        room, socks = make_room(4)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        ids = ["id0", "id1", "id2", "id3"]
        guesser_id = next(p for p in ids if p != drawer_id)
        guesser_sock = socks[ids.index(guesser_id)]
        others = [s for s, pid in zip(socks, ids)
                   if pid not in (drawer_id, guesser_id)]

        with patch("server.room.threading.Thread"):
            room.handle_guess(guesser_id, room.current_word)

        for s in others:
            notices = s.of_type("player_guessed")
            self.assertTrue(notices)
            self.assertNotIn("word", notices[-1])

    def test_round_ends_early_when_all_guessers_correct(self):
        room, _ = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        word = room.current_word
        guessers = [p for p in room.players if p != drawer_id]
        started_round = room.round_index

        with patch("server.room.threading.Thread"):
            for g in guessers:
                room.handle_guess(g, word)

        # end_round bumps the token to invalidate the timer.
        self.assertEqual(room.round_index, started_round)
        self.assertGreater(room.round_token, 1)

    def test_faster_guess_scores_higher(self):
        room, _ = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        guessers = [p for p in room.players if p != drawer_id]
        word = room.current_word

        # First guesser answers immediately; second after most of the round.
        with patch("server.room.threading.Thread"):
            room.handle_guess(guessers[0], word)
            fast_score = room.players[guessers[0]].score
            room.round_started_at -= config.ROUND_TIME_SECONDS * 0.9
            room.handle_guess(guessers[1], word)
            slow_score = room.players[guessers[1]].score

        self.assertGreater(fast_score, slow_score)


class TestDrawRelay(unittest.TestCase):
    def test_only_drawer_can_draw(self):
        room, socks = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        non_drawer = next(p for p in room.players if p != drawer_id)
        before = [len(s.sent_messages) for s in socks]

        room.relay_draw(non_drawer, {"x1": 1, "y1": 2, "x2": 3, "y2": 4})

        self.assertEqual([len(s.sent_messages) for s in socks], before)

    def test_drawer_stroke_reaches_all_other_players(self):
        room, socks = make_room(4)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        ids = ["id0", "id1", "id2", "id3"]
        drawer_sock = socks[ids.index(drawer_id)]

        room.relay_draw(drawer_id, {"x1": 1, "y1": 2, "x2": 3, "y2": 4,
                                     "color": "#000", "width": 3, "stroke_id": 9})

        for s, pid in zip(socks, ids):
            if pid == drawer_id:
                self.assertEqual(drawer_sock.of_type("draw_data"), [])
            else:
                self.assertEqual(s.of_type("draw_data")[-1]["stroke_id"], 9)

    def test_drawer_cannot_chat_during_round(self):
        # Otherwise the drawer could just type the answer.
        room, socks = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        before = [len(s.sent_messages) for s in socks]
        room.broadcast_chat(room.drawer_id, "the word is obviously apple")
        self.assertEqual([len(s.sent_messages) for s in socks], before)


class TestDisconnects(unittest.TestCase):
    def test_game_ends_when_players_drop_below_minimum(self):
        room, socks = make_room(2)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        room.remove_player("id1")
        self.assertEqual(room.state, "finished")
        self.assertTrue(socks[0].of_type("game_over"))

    def test_game_continues_when_a_guesser_leaves_with_enough_players(self):
        room, _ = make_room(4)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        leaver = next(p for p in room.players if p != drawer_id)
        room.remove_player(leaver)
        self.assertEqual(room.state, "playing")

    def test_drawer_leaving_mid_round_ends_that_round(self):
        room, _ = make_room(4)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        drawer_id = room.drawer_id
        token_before = room.round_token
        with patch("server.room.threading.Thread"):
            room.remove_player(drawer_id)
        self.assertGreater(room.round_token, token_before)


class TestStaleTimer(unittest.TestCase):
    def test_timer_from_ended_round_does_not_end_the_next_one(self):
        """Regression guard: a round that ends early (everyone guessed)
        leaves its timer thread alive. Without the round_token check it
        would fire later and prematurely end whatever round is running."""
        room, _ = make_room(3)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        stale_token = room.round_token

        with patch("server.room.threading.Thread"):
            room.end_round(reason="all_guessed")
            room.begin_round()

        current_token = room.round_token
        # Simulate the stale timer expiring now.
        with patch("server.room.time.sleep"):
            room._run_timer(stale_token)

        self.assertEqual(room.round_token, current_token)  # unchanged


if __name__ == "__main__":
    unittest.main()


class TestReconnection(unittest.TestCase):
    """The differentiating feature: a dropped player keeps their seat,
    score, and role, and gets a full state snapshot when they return."""

    def _start(self, n=3):
        room = Room("TEST", rng=random.Random(7))
        socks, tokens = [], []
        for i in range(n):
            s = FakeSocket(f"p{i}")
            _, _, tok = room.add_player(f"id{i}", f"Player{i}", s)
            socks.append(s)
            tokens.append(tok)
        with patch("server.room.threading.Thread"):
            room.start_game("id0")
        return room, socks, tokens

    def test_join_issues_a_session_token(self):
        room, socks, tokens = self._start(2)
        self.assertTrue(all(tokens))
        self.assertEqual(len(set(tokens)), 2, "tokens must be unique")
        joined = socks[0].of_type("joined")[0]
        self.assertEqual(joined["token"], tokens[0])

    def test_disconnect_mid_game_holds_the_seat(self):
        room, socks, tokens = self._start(3)
        before = len(room.players)

        with patch("server.room.threading.Thread"):
            room.remove_player("id1", socks[1])

        self.assertEqual(len(room.players), before, "seat should be held")
        self.assertFalse(room.players["id1"].connected)
        self.assertIsNone(room.players["id1"].sock)

    def test_disconnect_in_lobby_frees_the_seat_immediately(self):
        # Nothing worth preserving before the game starts.
        room = Room("TEST", rng=random.Random(7))
        s = FakeSocket("p0")
        room.add_player("id0", "P0", s)
        room.add_player("id1", "P1", FakeSocket("p1"))
        room.remove_player("id1", None)
        self.assertNotIn("id1", room.players)

    def test_reattach_restores_score_and_identity(self):
        room, socks, tokens = self._start(3)
        room.players["id1"].score = 42
        with patch("server.room.threading.Thread"):
            room.remove_player("id1", socks[1])

        new_sock = FakeSocket("p1-new")
        ok, pid, name = room.reattach(tokens[1], new_sock)

        self.assertTrue(ok)
        self.assertEqual(pid, "id1")
        self.assertEqual(name, "Player1")
        self.assertEqual(room.players["id1"].score, 42)
        self.assertTrue(room.players["id1"].connected)

    def test_reattach_sends_a_state_snapshot(self):
        room, socks, tokens = self._start(3)
        with patch("server.room.threading.Thread"):
            room.remove_player("id1", socks[1])
        new_sock = FakeSocket("p1-new")
        room.reattach(tokens[1], new_sock)

        snap = new_sock.of_type("state_sync")[-1]
        self.assertEqual(snap["state"], "playing")
        self.assertIn("role", snap)
        self.assertIn("time_left", snap)
        self.assertIn("strokes", snap)
        self.assertEqual(len(snap["scores"]), 3)

    def test_snapshot_replays_strokes_drawn_while_away(self):
        room, socks, tokens = self._start(3)
        drawer_id = room.drawer_id
        away_id = next(p for p in room.players if p != drawer_id)
        away_sock = socks[["id0", "id1", "id2"].index(away_id)]

        with patch("server.room.threading.Thread"):
            room.remove_player(away_id, away_sock)
        # Drawing continues while they're gone.
        for i in range(4):
            room.relay_draw(drawer_id, {
                "x1": i, "y1": i, "x2": i + 1, "y2": i + 1,
                "color": "#000", "width": 3, "stroke_id": i,
            })

        new_sock = FakeSocket("returned")
        room.reattach(tokens[["id0", "id1", "id2"].index(away_id)], new_sock)

        snap = new_sock.of_type("state_sync")[-1]
        self.assertEqual(len(snap["strokes"]), 4,
                          "returning player should receive the missed drawing")

    def test_drawer_keeps_the_word_after_reconnecting(self):
        room, socks, tokens = self._start(3)
        drawer_id = room.drawer_id
        idx = ["id0", "id1", "id2"].index(drawer_id)
        word = room.current_word

        with patch("server.room.threading.Thread"):
            room.remove_player(drawer_id, socks[idx])
        new_sock = FakeSocket("drawer-back")
        room.reattach(tokens[idx], new_sock)

        snap = new_sock.of_type("state_sync")[-1]
        self.assertEqual(snap["role"], "drawer")
        self.assertEqual(snap["word"], word)

    def test_guesser_reconnect_does_not_leak_the_word(self):
        room, socks, tokens = self._start(3)
        drawer_id = room.drawer_id
        ids = ["id0", "id1", "id2"]
        guesser_id = next(p for p in ids if p != drawer_id)
        idx = ids.index(guesser_id)

        with patch("server.room.threading.Thread"):
            room.remove_player(guesser_id, socks[idx])
        new_sock = FakeSocket("guesser-back")
        room.reattach(tokens[idx], new_sock)

        snap = new_sock.of_type("state_sync")[-1]
        self.assertEqual(snap["role"], "guesser")
        self.assertNotIn("word", snap)
        self.assertIn("word_length", snap)

    def test_player_who_already_guessed_gets_the_word_back(self):
        # They legitimately earned it before dropping; hiding it would
        # make the restored view inconsistent with what they saw.
        room, socks, tokens = self._start(3)
        drawer_id = room.drawer_id
        ids = ["id0", "id1", "id2"]
        guesser_id = next(p for p in ids if p != drawer_id)
        idx = ids.index(guesser_id)
        word = room.current_word

        with patch("server.room.threading.Thread"):
            room.handle_guess(guesser_id, word)
            room.remove_player(guesser_id, socks[idx])
        new_sock = FakeSocket("back")
        room.reattach(tokens[idx], new_sock)

        snap = new_sock.of_type("state_sync")[-1]
        self.assertEqual(snap["word"], word)
        self.assertTrue(snap["already_guessed"])

    def test_reattach_with_unknown_token_is_rejected(self):
        room, _, _ = self._start(2)
        ok, reason, _ = room.reattach("not-a-real-token", FakeSocket("x"))
        self.assertFalse(ok)
        self.assertIn("expired", reason.lower())

    def test_reattach_evicts_the_stale_socket(self):
        """A client often reconnects before the server's recv loop has
        noticed the old socket died. Without eviction the room would hold
        two live sockets claiming to be the same player."""
        room, socks, tokens = self._start(3)
        old_sock = socks[1]
        new_sock = FakeSocket("p1-new")

        room.reattach(tokens[1], new_sock)  # no disconnect first

        self.assertIs(room.players["id1"].sock, new_sock)
        self.assertIsNot(room.players["id1"].sock, old_sock)

    def test_stale_recv_loop_cannot_disconnect_a_reconnected_player(self):
        """The old socket's recv loop fires remove_player *after* the
        player has already returned on a new socket. That callback must
        be ignored, or it would knock them straight back offline."""
        room, socks, tokens = self._start(3)
        old_sock = socks[1]

        with patch("server.room.threading.Thread"):
            room.remove_player("id1", old_sock)
        new_sock = FakeSocket("p1-new")
        room.reattach(tokens[1], new_sock)

        # Late callback from the dead connection.
        with patch("server.room.threading.Thread"):
            room.remove_player("id1", old_sock)

        self.assertTrue(room.players["id1"].connected)
        self.assertIs(room.players["id1"].sock, new_sock)

    def test_seat_expires_after_grace_period(self):
        room, socks, tokens = self._start(3)
        with patch("server.room.threading.Thread"):
            room.remove_player("id1", socks[1])
        self.assertIn("id1", room.players)

        # Run the expiry body directly rather than waiting on wall clock.
        with patch("server.room.time.sleep"):
            room._expire_seat("id1", tokens[1])

        self.assertNotIn("id1", room.players)

    def test_seat_expiry_does_not_evict_a_returned_player(self):
        room, socks, tokens = self._start(3)
        with patch("server.room.threading.Thread"):
            room.remove_player("id1", socks[1])
        room.reattach(tokens[1], FakeSocket("p1-new"))

        with patch("server.room.time.sleep"):
            room._expire_seat("id1", tokens[1])

        self.assertIn("id1", room.players, "returned player must keep their seat")

    def test_disconnected_players_are_not_sent_messages(self):
        room, socks, tokens = self._start(3)
        with patch("server.room.threading.Thread"):
            room.remove_player("id1", socks[1])
        before = len(socks[1].sent_messages)

        room.relay_draw(room.drawer_id, {
            "x1": 1, "y1": 1, "x2": 2, "y2": 2,
            "color": "#000", "width": 3, "stroke_id": 1,
        })

        self.assertEqual(len(socks[1].sent_messages), before)

    def test_room_is_abandoned_when_all_players_disconnect(self):
        room, socks, tokens = self._start(2)
        with patch("server.room.threading.Thread"):
            room.remove_player("id0", socks[0])
            room.remove_player("id1", socks[1])
        self.assertTrue(room.is_abandoned())
        # Seats are still held, so the room must not be collectable yet.
        self.assertTrue(room.players)

    def test_stroke_log_is_bounded(self):
        """A misbehaving client must not be able to grow the replay log
        without limit."""
        room, socks, tokens = self._start(2)
        drawer_id = room.drawer_id
        for i in range(config.MAX_STROKES_PER_ROUND + 50):
            room.relay_draw(drawer_id, {
                "x1": 0, "y1": 0, "x2": 1, "y2": 1,
                "color": "#000", "width": 3, "stroke_id": i,
            })
        self.assertLessEqual(len(room.stroke_log), config.MAX_STROKES_PER_ROUND)

    def test_stroke_log_clears_on_new_round(self):
        room, socks, tokens = self._start(2)
        room.relay_draw(room.drawer_id, {
            "x1": 0, "y1": 0, "x2": 1, "y2": 1,
            "color": "#000", "width": 3, "stroke_id": 1,
        })
        self.assertTrue(room.stroke_log)
        with patch("server.room.threading.Thread"):
            room.end_round(reason="timeout")
            room.begin_round()
        self.assertEqual(room.stroke_log, [])
