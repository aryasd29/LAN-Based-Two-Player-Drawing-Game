"""
Drawing game server: connection handling and room registry.

Architecture
------------
The server owns a registry of Rooms and routes each client's messages to
whichever room they've joined. All game logic lives in server/room.py --
this module is deliberately thin, handling only sockets, the join
handshake, and dispatch.

This is a change from the earlier version, where a single GameServer held
the two players directly and selected the drawer as
`clients[current_round % 2]`. That hardcoded two players into the round
logic. Splitting rooms out means player count is now a config range
(MIN_PLAYERS..MAX_PLAYERS) rather than an architectural assumption, and
multiple independent games can run on one server process.

Threading
---------
- One thread per accepted connection, running that client's recv loop.
- One thread per active round timer (owned by the Room).
- A registry lock guards the rooms dict; each Room has its own lock for
  its internal state, so activity in one room never blocks another.
"""

from __future__ import annotations

import random
import socket
import threading
import time
import uuid

from common import config
from common.protocol import ConnectionClosed, receive_messages, send_message
from server.room import Room


def enable_keepalive(sock: socket.socket) -> None:
    """Detect peers that vanish without closing the connection.

    A client that exits or crashes sends FIN or RST, and recv() returns
    promptly (measured at ~50ms locally). But a laptop whose lid closes,
    or a device that drops off WiFi, sends nothing at all -- the socket
    stays open on our side and recv() blocks until TCP keepalive fires.
    The Linux default for that is 7200 seconds, so without tuning, a
    player who vanished would hold their seat for two hours: their slot
    stays occupied against MAX_PLAYERS, they linger in the lobby list,
    and the room is never collected.

    That case matters here specifically because "my WiFi dropped" is the
    exact scenario the reconnection feature exists to handle.

    These settings probe an idle connection after KEEPALIVE_IDLE seconds,
    retry every KEEPALIVE_INTERVAL, and give up after KEEPALIVE_COUNT
    failures -- so a vanished peer is detected in roughly
    idle + interval x count seconds instead of two hours.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return  # keepalive unsupported; nothing more to do
    for opt_name, value in (
        ("TCP_KEEPIDLE", config.KEEPALIVE_IDLE_SECONDS),
        ("TCP_KEEPINTVL", config.KEEPALIVE_INTERVAL_SECONDS),
        ("TCP_KEEPCNT", config.KEEPALIVE_PROBE_COUNT),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                # Platform-specific (e.g. macOS names TCP_KEEPIDLE
                # differently). SO_KEEPALIVE alone still applies, just
                # with the OS default timing.
                pass


class GameServer:
    def __init__(self, rng: random.Random | None = None):
        self.registry_lock = threading.Lock()
        self.rooms: dict[str, Room] = {}
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------
    # Room registry
    # ------------------------------------------------------------------

    def _generate_room_code(self) -> str:
        """Registry lock must be held."""
        while True:
            code = "".join(
                self._rng.choice(config.ROOM_CODE_ALPHABET)
                for _ in range(config.ROOM_CODE_LENGTH)
            )
            if code not in self.rooms:
                return code

    def create_room(self) -> Room:
        with self.registry_lock:
            code = self._generate_room_code()
            room = Room(code, rng=self._rng)
            self.rooms[code] = room
            return room

    def get_room(self, code: str) -> Room | None:
        with self.registry_lock:
            return self.rooms.get(code.strip().upper())

    def find_open_room(self) -> Room | None:
        """Quick-play: first room still in lobby with space."""
        with self.registry_lock:
            for room in self.rooms.values():
                with room.lock:
                    if room.state == "lobby" and len(room.players) < config.MAX_PLAYERS:
                        return room
        return None

    def _drop_room_if_empty(self, room: Room) -> None:
        """Delete a room only once it holds no players at all.

        Note this deliberately checks `room.players`, not connected
        players: during a game a disconnected player still HOLDS their
        seat until the grace period lapses, and deleting the room out from
        under them would make reconnection impossible. Rooms whose players
        have all gone but whose seats haven't expired yet are cleaned up
        by the reaper below.
        """
        with self.registry_lock:
            with room.lock:
                empty = not room.players
            if empty and self.rooms.get(room.code) is room:
                del self.rooms[room.code]
                print(f"[*] Room {room.code} closed (empty)")

    def start_reaper(self) -> threading.Thread:
        """Periodically drop rooms nobody is connected to any more.

        Without this, a room where every player dropped mid-game would
        linger in the registry: each held seat expires on its own timer,
        but the last expiry has no way to notify the registry that the
        room is now collectable.
        """
        def reap() -> None:
            while True:
                time.sleep(config.ROOM_REAP_INTERVAL_SECONDS)
                with self.registry_lock:
                    codes = list(self.rooms)
                for code in codes:
                    with self.registry_lock:
                        room = self.rooms.get(code)
                    if room is None:
                        continue
                    with room.lock:
                        collectable = not room.players
                    if collectable:
                        self._drop_room_if_empty(room)

        thread = threading.Thread(target=reap, daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def handle_client(self, sock: socket.socket, addr) -> None:
        print(f"[+] {addr} connected")
        player_id = uuid.uuid4().hex[:12]
        room: Room | None = None

        try:
            for msg in receive_messages(sock, config.RECV_BUFFER_SIZE):
                if room is None:
                    # Until a join succeeds, the only messages we accept
                    # are join/rejoin -- this keeps unjoined sockets from
                    # touching any room state.
                    room, resumed_id = self._handle_join(sock, player_id, msg)
                    if resumed_id:
                        # This connection has adopted an existing session,
                        # so it must act as that player from here on.
                        player_id = resumed_id
                else:
                    self._dispatch(room, player_id, msg)
        except ConnectionClosed:
            pass
        except OSError as e:
            print(f"[-] Connection error with {addr}: {e}")
        finally:
            if room is not None:
                # Pass the socket so the room can ignore this callback if
                # the player has already reconnected on a newer socket.
                room.remove_player(player_id, sock)
                self._drop_room_if_empty(room)
            try:
                sock.close()
            except OSError:
                pass
            print(f"[-] {addr} disconnected")

    def _handle_join(self, sock: socket.socket, player_id: str,
                      msg: dict) -> tuple[Room | None, str | None]:
        """Handle the first message on a connection.

        Returns (room, resumed_player_id). `resumed_player_id` is set only
        when an existing session was reattached, in which case the caller
        must adopt that identity instead of the freshly generated one.
        """
        msg_type = msg.get("type")

        if msg_type == "rejoin":
            return self._handle_rejoin(sock, msg)

        if msg_type != "join":
            self._send_error(sock, "Join a room before sending anything else.")
            return None, None

        name = str(msg.get("name", "")).strip()[:20] or "Player"
        code = str(msg.get("room_code", "")).strip().upper()

        if code:
            room = self.get_room(code)
            if room is None:
                self._send_error(sock, f"No room found with code {code}.")
                return None, None
        elif msg.get("create"):
            room = self.create_room()
            print(f"[*] Room {room.code} created")
        else:
            room = self.find_open_room()
            if room is None:
                room = self.create_room()
                print(f"[*] Room {room.code} created (quick play)")

        accepted, reason, _token = room.add_player(player_id, name, sock)
        if not accepted:
            self._send_error(sock, reason)
            self._drop_room_if_empty(room)
            return None, None
        return room, None

    def _handle_rejoin(self, sock: socket.socket,
                        msg: dict) -> tuple[Room | None, str | None]:
        """Resume a session after a dropped connection.

        The client supplies the room code and the session token it was
        given when it first joined. A token is only meaningful within its
        own room, so both are required -- this also avoids scanning every
        room for a matching token.
        """
        code = str(msg.get("room_code", "")).strip().upper()
        token = str(msg.get("token", "")).strip()

        if not code or not token:
            self._send_error(sock, "Reconnect needs both a room code and a token.")
            return None, None

        room = self.get_room(code)
        if room is None:
            # The room may have been garbage-collected while the player
            # was away (everyone left, or the grace period lapsed).
            self._send_error(sock, "That room no longer exists.")
            return None, None

        ok, player_id_or_reason, name = room.reattach(token, sock)
        if not ok:
            self._send_error(sock, player_id_or_reason)
            return None, None

        print(f"[*] {name} resumed session in room {room.code}")
        return room, player_id_or_reason

    def _dispatch(self, room: Room, player_id: str, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "start_game":
            ok, reason = room.start_game(player_id)
            if not ok:
                with room.lock:
                    player = room.players.get(player_id)
                if player:
                    self._send_error(player.sock, reason)

        elif msg_type == "draw":
            room.relay_draw(player_id, {
                k: msg[k] for k in ("x1", "y1", "x2", "y2", "color", "width", "stroke_id")
                if k in msg
            })

        elif msg_type == "clear":
            room.relay_simple(player_id, {"type": "clear_canvas"})

        elif msg_type == "undo":
            room.relay_simple(player_id, {
                "type": "undo_stroke", "stroke_id": msg.get("stroke_id"),
            })

        elif msg_type == "guess":
            room.handle_guess(player_id, msg.get("guess", ""))

        elif msg_type == "chat":
            room.broadcast_chat(player_id, msg.get("text", ""))

    @staticmethod
    def _send_error(sock: socket.socket, message: str) -> None:
        try:
            send_message(sock, {"type": "error", "message": message})
        except OSError:
            pass


def main() -> None:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((config.SERVER_IP, config.SERVER_PORT))
    except OSError as e:
        print(f"[!] Could not bind to {config.SERVER_IP}:{config.SERVER_PORT} -- {e}")
        print("    Is the port already in use? Try a different SKETCHMESH_PORT.")
        return

    server_socket.listen(64)
    print(f"[*] Server ready on {config.SERVER_IP}:{config.SERVER_PORT}")
    print(f"    Rooms hold {config.MIN_PLAYERS}-{config.MAX_PLAYERS} players.")

    game = GameServer()
    game.start_reaper()

    try:
        while True:
            sock, addr = server_socket.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            enable_keepalive(sock)
            threading.Thread(
                target=game.handle_client, args=(sock, addr), daemon=True
            ).start()
    except KeyboardInterrupt:
        print("\n[*] Shutting down server...")
    finally:
        server_socket.close()


if __name__ == "__main__":
    main()
