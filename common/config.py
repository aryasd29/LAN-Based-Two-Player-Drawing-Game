"""
Shared configuration for the drawing game.

All values can be overridden with environment variables so players don't
have to hand-edit source files to change the host IP, port, or game
rules.

Example:
    set SKETCHMESH_PORT=5555 && python -m server.game_server
"""

import os

# --- Network ---
SERVER_IP = os.environ.get("SKETCHMESH_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SKETCHMESH_PORT", "12345"))

CLIENT_CONNECT_IP = os.environ.get("SKETCHMESH_CONNECT_IP", "127.0.0.1")
CLIENT_CONNECT_PORT = int(os.environ.get("SKETCHMESH_PORT", "12345"))

# --- Room / lobby ---
MIN_PLAYERS = int(os.environ.get("SKETCHMESH_MIN_PLAYERS", "2"))
MAX_PLAYERS = int(os.environ.get("SKETCHMESH_MAX_PLAYERS", "8"))
ROOM_CODE_LENGTH = 4
# Excludes visually ambiguous characters (0/O, 1/I/L) so players can read
# a room code off someone else's screen without mistyping it.
ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# --- Game rules ---
ROUNDS_PER_PLAYER = int(os.environ.get("SKETCHMESH_ROUNDS", "2"))
ROUND_TIME_SECONDS = int(os.environ.get("SKETCHMESH_ROUND_TIME", "60"))
INTERMISSION_SECONDS = float(os.environ.get("SKETCHMESH_INTERMISSION", "4"))

# Scoring: a correct guess is worth a base amount plus a speed bonus that
# decays with the fraction of the round already elapsed. This rewards
# guessing fast without making a late correct guess worthless.
GUESS_BASE_POINTS = 10
GUESS_SPEED_BONUS_MAX = 10
# The drawer earns per player who successfully guessed their drawing,
# so drawing well for a full room is worth more than for one person.
DRAWER_POINTS_PER_CORRECT_GUESS = 5

WORDS = [
    "fish", "apple", "house", "tree", "car", "ball", "dog", "cat", "star",
    "book", "phone", "hat", "shoe", "computer", "pencil", "cup", "table",
    "chair", "window", "door", "sun", "moon", "bridge", "rocket", "guitar",
    "island", "ladder", "castle", "camera", "umbrella", "volcano", "anchor",
]

# --- Networking internals ---
RECV_BUFFER_SIZE = 4096
CONNECT_TIMEOUT_SECONDS = 5

# --- Sessions / reconnection ---
# How long a disconnected player's seat (and score) is held before the
# server gives it up. Long enough to survive a laptop sleeping or WiFi
# flapping; short enough that a room isn't clogged by someone who left.
SESSION_GRACE_SECONDS = float(os.environ.get("SKETCHMESH_GRACE", "90"))

# The server keeps the current round's strokes so a reconnecting (or
# late-joining) player can be sent the drawing so far. Bounded so a
# pathological client can't grow the log without limit; past this point
# new strokes are relayed live but not recorded for replay.
MAX_STROKES_PER_ROUND = int(os.environ.get("SKETCHMESH_MAX_STROKES", "4000"))

# Client-side reconnect behaviour.
RECONNECT_ATTEMPTS = 6
RECONNECT_DELAY_SECONDS = 2.0

# How often the server sweeps for rooms whose players have all left and
# whose held seats have since expired. Only affects cleanup latency.
ROOM_REAP_INTERVAL_SECONDS = float(os.environ.get("SKETCHMESH_REAP_INTERVAL", "30"))

# --- Client reconnection ---
# How many times a dropped client retries before giving up. The total
# retry window should stay under SESSION_GRACE_SECONDS, otherwise the
# client would still be retrying after the server has already freed
# the seat.
CLIENT_RECONNECT_ATTEMPTS = int(os.environ.get("SKETCHMESH_RECONNECT_ATTEMPTS", "10"))
CLIENT_RECONNECT_BASE_DELAY_MS = 700
CLIENT_RECONNECT_MAX_DELAY_MS = 4000

# --- Dead-peer detection ---
# A peer that vanishes silently (lid closed, WiFi dropped) sends no FIN
# or RST, so recv() blocks until TCP keepalive fires. The OS default is
# 7200s; these tune it to detect a vanished peer in roughly
# IDLE + INTERVAL * COUNT seconds.
KEEPALIVE_IDLE_SECONDS = int(os.environ.get("SKETCHMESH_KEEPALIVE_IDLE", "20"))
KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("SKETCHMESH_KEEPALIVE_INTERVAL", "5"))
KEEPALIVE_PROBE_COUNT = int(os.environ.get("SKETCHMESH_KEEPALIVE_COUNT", "3"))
