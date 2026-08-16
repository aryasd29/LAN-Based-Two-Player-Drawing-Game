"""
Self-contained benchmark runner.

Starts a real GameServer on a real socket inside this process, then runs
the load test against it at several load levels and prints a table. Doing
it in one process keeps the harness reproducible (no manual server
startup, no port juggling) and lets sender and receiver share a clock for
accurate latency measurement.

Usage:
    python -m bench.run_benchmarks
    python -m bench.run_benchmarks --seconds 15 --json results.json
"""

from __future__ import annotations

import argparse
import json
import socket
import threading

from server.game_server import GameServer, enable_keepalive
from bench.loadtest import run_load_test


def start_server() -> int:
    """Start a GameServer on an OS-assigned free port. Returns the port."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    port = server_socket.getsockname()[1]

    game = GameServer()

    def accept_loop() -> None:
        while True:
            try:
                sock, addr = server_socket.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            enable_keepalive(sock)
            threading.Thread(
                target=game.handle_client, args=(sock, addr), daemon=True
            ).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return port


# (rooms, players_per_room, strokes_per_second_per_drawer)
SCENARIOS = [
    (1, 2, 30),    # baseline: the original two-player case
    (1, 8, 30),    # one full room
    (5, 8, 30),    # multiple concurrent rooms
    (10, 8, 30),   # heavier fan-out
    (10, 8, 60),   # same players, double the stroke rate
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--json", help="also write raw results to this path")
    args = parser.parse_args()

    port = start_server()
    print(f"[*] Benchmark server listening on 127.0.0.1:{port}\n")

    results = []
    header = (
        f"{'Rooms':>5} {'Players':>8} {'Str/s':>6} {'Deliv/s':>9} "
        f"{'p50':>7} {'p95':>7} {'p99':>7} {'max':>8} {'Err':>4}"
    )
    print(header)
    print("-" * len(header))

    for rooms, ppr, sps in SCENARIOS:
        r = run_load_test(
            "127.0.0.1", port, rooms=rooms, players_per_room=ppr,
            seconds=args.seconds, strokes_per_second=sps,
        )
        r["strokes_per_second_per_drawer"] = sps
        results.append(r)
        lat = r["latency_ms"]
        print(
            f"{r['rooms']:>5} {r['players']:>8} {sps:>6} "
            f"{r['deliveries_per_second']:>9} "
            f"{lat['p50']:>7} {lat['p95']:>7} {lat['p99']:>7} "
            f"{lat['max']:>8} {r['error_count']:>4}"
        )

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"\n[*] Raw results written to {args.json}")


if __name__ == "__main__":
    main()
