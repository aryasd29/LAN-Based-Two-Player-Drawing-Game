"""
Drawing game client (Tkinter GUI).

Three screens in one window:
  1. ConnectScreen -- name, server, and room code (or quick play).
  2. LobbyScreen   -- player list, room code to share, host start button.
  3. GameScreen    -- canvas, tools, live scoreboard, chat/guess feed.

Threading
---------
Tkinter is not thread-safe: widgets must only be touched from the thread
running mainloop(). The network-receive thread never touches widgets
directly -- it pushes decoded messages onto a queue.Queue, and the main
thread drains that queue via root.after(). This is the standard safe
pattern for combining a blocking socket read loop with a Tk event loop.

The client is deliberately "dumb": it renders whatever the server sends
and forwards user input. It never decides whose turn it is, what the
score is, or whether a guess was right -- all of that is server
authoritative, so two clients can't disagree about game state.
"""

from __future__ import annotations

import argparse
import itertools
import queue
import socket
import threading
import tkinter as tk
from tkinter import colorchooser, font as tkfont

from common import config
from common.protocol import ConnectionClosed, receive_messages, send_message

# --- Theme -----------------------------------------------------------------

BG = "#12141c"
PANEL = "#1a1d29"
PANEL_ALT = "#242838"
BORDER = "#2f3448"
TEXT = "#e8e9f0"
TEXT_DIM = "#8a8fa3"
ACCENT = "#6c5ce7"
TEAL = "#00d9c0"
SUCCESS = "#2ecc71"
DANGER = "#e74c3c"
WARNING = "#f5b942"
CANVAS_BG = "#ffffff"

FONT_FAMILY = "Segoe UI"

PALETTE_COLORS = [
    "#111111", "#7f8c8d", "#e74c3c", "#e84393",
    "#f5b942", "#e67e22", "#2ecc71", "#00d9c0",
    "#3498db", "#6c5ce7", "#8b4513", "#ffffff",
]

_stroke_ids = itertools.count(1)


def F(size, weight="normal"):
    return (FONT_FAMILY, size, weight)


def panel(parent, **kw):
    kw.setdefault("bg", PANEL)
    kw.setdefault("highlightbackground", BORDER)
    kw.setdefault("highlightthickness", 1)
    return tk.Frame(parent, **kw)


def button(parent, text, command, bg=ACCENT, fg="white", small=False, **kw):
    return tk.Button(
        parent, text=text, command=command, bg=bg, fg=fg,
        activebackground=bg, activeforeground=fg,
        font=F(10 if small else 11, "bold"), bd=0, relief="flat",
        cursor="hand2", padx=10 if small else 14, pady=4 if small else 8, **kw
    )


def entry(parent, textvariable=None, **kw):
    return tk.Entry(
        parent, textvariable=textvariable, font=kw.pop("font", F(12)),
        bg=PANEL_ALT, fg=TEXT, insertbackground=TEXT, relief="flat",
        highlightthickness=1, highlightbackground=BORDER,
        highlightcolor=ACCENT, **kw
    )


# --- Connect screen --------------------------------------------------------

class ConnectScreen(tk.Frame):
    def __init__(self, root, default_host, default_port, on_connect):
        super().__init__(root, bg=BG)
        self.root = root
        self.on_connect = on_connect

        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(wrap, text="🎨", font=F(40), bg=BG, fg=TEXT).pack()
        tk.Label(wrap, text="SketchMesh", font=F(24, "bold"), bg=BG, fg=TEXT).pack()
        tk.Label(wrap, text="Real-time multiplayer drawing game",
                 font=F(11), bg=BG, fg=TEXT_DIM).pack(pady=(0, 20))

        card = panel(wrap, padx=28, pady=22)
        card.pack()

        self.name_var = tk.StringVar(value="Player")
        self.host_var = tk.StringVar(value=default_host)
        self.port_var = tk.StringVar(value=str(default_port))
        self.room_var = tk.StringVar(value="")

        for label, var in (("Your name", self.name_var),
                            ("Server IP", self.host_var),
                            ("Port", self.port_var)):
            tk.Label(card, text=label, font=F(10), bg=PANEL, fg=TEXT_DIM,
                      anchor="w").pack(fill="x", pady=(6, 2))
            entry(card, var, width=26).pack(ipady=6)

        tk.Label(card, text="Room code  (blank = quick play)", font=F(10),
                 bg=PANEL, fg=TEXT_DIM, anchor="w").pack(fill="x", pady=(10, 2))
        entry(card, self.room_var, width=26, font=F(13, "bold")).pack(ipady=6)

        self.status = tk.Label(card, text="", font=F(10), bg=PANEL, fg=DANGER,
                                wraplength=240, justify="left")
        self.status.pack(anchor="w", pady=(8, 4))

        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", pady=(4, 0))
        self.join_btn = button(row, "Join", lambda: self._go(create=False))
        self.join_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.create_btn = button(row, "New room", lambda: self._go(create=True),
                                  bg=PANEL_ALT, fg=TEXT)
        self.create_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        root.bind("<Return>", lambda _e: self._go(create=False))

    def _go(self, create: bool):
        name = self.name_var.get().strip() or "Player"
        host = self.host_var.get().strip()
        room = self.room_var.get().strip().upper()

        if not host:
            return self._err("Enter the server's IP address.")
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            return self._err("Port must be a number.")

        self._err("Connecting...", TEXT_DIM)
        for b in (self.join_btn, self.create_btn):
            b.config(state="disabled")
        self.root.update_idletasks()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(config.CONNECT_TIMEOUT_SECONDS)
            sock.connect((host, port))
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            for b in (self.join_btn, self.create_btn):
                b.config(state="normal")
            return self._err(f"Couldn't connect: {e}")

        self.root.unbind("<Return>")
        self.on_connect(sock, name, room, create)

    def _err(self, text, color=DANGER):
        self.status.config(text=text, fg=color)


# --- Lobby screen ----------------------------------------------------------

class LobbyScreen(tk.Frame):
    def __init__(self, root, on_start, on_leave):
        super().__init__(root, bg=BG)
        self.on_start = on_start
        self.is_host = False

        wrap = tk.Frame(self, bg=BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(wrap, text="Waiting room", font=F(20, "bold"),
                 bg=BG, fg=TEXT).pack()

        code_row = tk.Frame(wrap, bg=BG)
        code_row.pack(pady=(10, 4))
        tk.Label(code_row, text="Room code", font=F(10), bg=BG,
                 fg=TEXT_DIM).pack()
        self.code_label = tk.Label(code_row, text="----", font=F(30, "bold"),
                                    bg=BG, fg=TEAL)
        self.code_label.pack()
        tk.Label(wrap, text="Share this code so others can join",
                 font=F(9), bg=BG, fg=TEXT_DIM).pack(pady=(0, 14))

        self.list_panel = panel(wrap, padx=20, pady=16)
        self.list_panel.pack(fill="x")
        self.players_box = tk.Frame(self.list_panel, bg=PANEL)
        self.players_box.pack(fill="x")

        self.hint = tk.Label(wrap, text="", font=F(10), bg=BG, fg=TEXT_DIM)
        self.hint.pack(pady=(12, 6))

        # Both buttons live in a fixed container so hiding/showing
        # "Start game" can't reorder it below "Leave" (pack() appends to
        # the end of the parent, which is what caused exactly that).
        btns = tk.Frame(wrap, bg=BG)
        btns.pack()
        self.start_btn = button(btns, "Start game", self.on_start)
        self.start_btn.grid(row=0, column=0, pady=(0, 8))
        button(btns, "Leave", on_leave, bg=PANEL_ALT, fg=TEXT,
               small=True).grid(row=1, column=0)
        self.start_btn.grid_remove()

    def update_state(self, msg, my_id):
        self.code_label.config(text=msg.get("room_code", self.code_label.cget("text")))
        players = msg.get("players", [])
        self.is_host = msg.get("host_id") == my_id

        for w in self.players_box.winfo_children():
            w.destroy()
        for p in players:
            row = tk.Frame(self.players_box, bg=PANEL)
            row.pack(fill="x", pady=3)
            is_host = p["player_id"] == msg.get("host_id")
            dot = TEAL if p["player_id"] == my_id else ACCENT
            tk.Frame(row, bg=dot, width=4, height=18).pack(side="left")
            label = p["name"] + ("  (host)" if is_host else "") + \
                    ("  ← you" if p["player_id"] == my_id else "")
            tk.Label(row, text="  " + label, font=F(11), bg=PANEL, fg=TEXT,
                      anchor="w").pack(side="left", fill="x", expand=True)

        n = len(players)
        need = msg.get("min_players", config.MIN_PLAYERS)
        cap = msg.get("max_players", config.MAX_PLAYERS)
        if n < need:
            self.hint.config(text=f"{n}/{cap} players — need at least {need} to start")
            self.start_btn.grid_remove()
        elif self.is_host:
            self.hint.config(text=f"{n}/{cap} players — ready when you are")
            self.start_btn.grid()
        else:
            self.hint.config(text=f"{n}/{cap} players — waiting for the host to start")
            self.start_btn.grid_remove()

    def set_code(self, code):
        self.code_label.config(text=code)


# --- Game screen -----------------------------------------------------------

class GameScreen(tk.Frame):
    def __init__(self, root, send, my_id):
        super().__init__(root, bg=BG)
        self.root = root
        self.send = send
        self.my_id = my_id

        self.color = PALETTE_COLORS[0]
        self.brush = tk.IntVar(value=4)
        self.drawing = False
        self.is_drawer = False
        self.sx = self.sy = None
        self.stroke_id = None
        self.my_strokes: list[int] = []
        self.round_duration = config.ROUND_TIME_SECONDS

        self._build()

    def _build(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 8))

        left = tk.Frame(head, bg=BG)
        left.pack(side="left")
        self.status = tk.Label(left, text="Get ready...", font=F(15, "bold"),
                                bg=BG, fg=TEXT)
        self.status.pack(anchor="w")
        self.sub = tk.Label(left, text="", font=F(9), bg=BG, fg=TEXT_DIM)
        self.sub.pack(anchor="w")

        mid = tk.Frame(head, bg=BG)
        mid.place(relx=0.5, rely=0.5, anchor="center")
        self.round_label = tk.Label(mid, text="", font=F(11, "bold"),
                                     bg=BG, fg=TEXT_DIM)
        self.round_label.pack()

        right = tk.Frame(head, bg=BG)
        right.pack(side="right")
        self.timer_num = tk.Label(right, text="—", font=F(20, "bold"), bg=BG, fg=TEXT)
        self.timer_num.pack(side="right", padx=(8, 0))
        track = tk.Frame(right, bg=BORDER, width=130, height=8)
        track.pack(side="right", pady=(6, 0))
        track.pack_propagate(False)
        self.timer_bar = tk.Frame(track, bg=TEAL)
        self.timer_bar.place(x=0, y=0, relheight=1, relwidth=1)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self._build_tools(body)
        self._build_waiting_panel(body)

        center = tk.Frame(body, bg=BG)
        self.center = center
        center.pack(side="left", fill="both", expand=True, padx=12)
        cwrap = panel(center, padx=2, pady=2)
        cwrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cwrap, bg=CANVAS_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self.guess_row = tk.Frame(center, bg=BG)
        self.guess_entry = entry(self.guess_row, font=F(13))
        self.guess_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self.guess_entry.bind("<Return>", lambda _e: self.submit_guess())
        button(self.guess_row, "Guess", self.submit_guess).pack(side="left")
        self.guess_row.pack(fill="x", pady=(10, 0))

        self._build_side(body)

    def _build_tools(self, parent):
        # Kept as an attribute so it can be hidden for guessers -- showing
        # drawing tools to someone who can't draw is just confusing, and
        # the server rejects their strokes anyway.
        side = panel(parent, padx=12, pady=14, width=150)
        self.tools_panel = side
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        tk.Label(side, text="COLOR", font=F(9, "bold"), bg=PANEL,
                 fg=TEXT_DIM, anchor="w").pack(fill="x", pady=(0, 6))
        grid = tk.Frame(side, bg=PANEL)
        grid.pack(fill="x")
        self.swatches = []
        for i, c in enumerate(PALETTE_COLORS):
            b = tk.Frame(grid, bg=c, width=25, height=25,
                          highlightthickness=2, highlightbackground=PANEL)
            b.grid(row=i // 4, column=i % 4, padx=3, pady=3)
            b.bind("<Button-1>", lambda _e, col=c: self.set_color(col))
            self.swatches.append((b, c))
        self.set_color(self.color)

        button(side, "🎨 Custom", self.choose_color, bg=PANEL_ALT, fg=TEXT,
               small=True).pack(fill="x", pady=(10, 3))
        button(side, "🧼 Eraser", lambda: self.set_color(CANVAS_BG),
               bg=PANEL_ALT, fg=TEXT, small=True).pack(fill="x", pady=3)

        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", pady=10)
        tk.Label(side, text="BRUSH", font=F(9, "bold"), bg=PANEL,
                 fg=TEXT_DIM, anchor="w").pack(fill="x")
        tk.Scale(side, from_=1, to=24, orient="horizontal", variable=self.brush,
                 bg=PANEL, fg=TEXT, troughcolor=PANEL_ALT, highlightthickness=0,
                 font=F(8), sliderrelief="flat", activebackground=ACCENT).pack(fill="x")

        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", pady=10)
        button(side, "↩ Undo", self.undo, bg=PANEL_ALT, fg=TEXT,
               small=True).pack(fill="x", pady=3)
        button(side, "🗑 Clear", self.clear, bg=DANGER, small=True).pack(fill="x", pady=3)

    def _build_waiting_panel(self, parent):
        """Shown to guessers where the drawing tools sit for the drawer,
        so the layout doesn't jump when roles switch."""
        p = panel(parent, padx=12, pady=14, width=150)
        self.waiting_panel = p
        p.pack_propagate(False)
        tk.Label(p, text="✏️", font=F(28), bg=PANEL, fg=TEXT).pack(pady=(30, 6))
        tk.Label(p, text="Guessing", font=F(11, "bold"), bg=PANEL,
                 fg=TEXT).pack()
        tk.Label(p, text="Watch the canvas\nand type your guess",
                 font=F(9), bg=PANEL, fg=TEXT_DIM, justify="center").pack(pady=(4, 12))
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", pady=6)
        tk.Label(p, text="WORD", font=F(9, "bold"), bg=PANEL,
                 fg=TEXT_DIM).pack(pady=(6, 2))
        self.word_hint = tk.Label(p, text="", font=F(14, "bold"), bg=PANEL,
                                   fg=TEAL, wraplength=120)
        self.word_hint.pack()

    def _build_side(self, parent):
        side = tk.Frame(parent, bg=BG, width=210)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        sp = panel(side, padx=12, pady=12)
        sp.pack(fill="x")
        tk.Label(sp, text="SCOREBOARD", font=F(9, "bold"), bg=PANEL,
                 fg=TEXT_DIM, anchor="w").pack(fill="x", pady=(0, 6))
        self.score_box = tk.Frame(sp, bg=PANEL)
        self.score_box.pack(fill="x")

        fp = panel(side, padx=12, pady=12)
        fp.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(fp, text="ACTIVITY", font=F(9, "bold"), bg=PANEL,
                 fg=TEXT_DIM, anchor="w").pack(fill="x", pady=(0, 6))
        self.feed = tk.Text(fp, bg=PANEL, fg=TEXT, font=F(9), relief="flat",
                             highlightthickness=0, wrap="word", state="disabled",
                             cursor="arrow")
        self.feed.pack(fill="both", expand=True)
        for tag, col in (("ok", SUCCESS), ("bad", DANGER),
                          ("info", TEXT_DIM), ("warn", WARNING)):
            self.feed.tag_config(tag, foreground=col)

    # -- feed --

    def log(self, text, tag="info"):
        self.feed.config(state="normal")
        self.feed.insert("end", text + "\n", tag)
        self.feed.see("end")
        self.feed.config(state="disabled")

    # -- tools --

    def set_color(self, c):
        self.color = c
        for f, col in self.swatches:
            f.config(highlightbackground=(TEXT if col == c else PANEL))

    def choose_color(self):
        picked = colorchooser.askcolor(self.color)[1]
        if picked:
            self.color = picked
            for f, _ in self.swatches:
                f.config(highlightbackground=PANEL)

    def clear(self):
        if not self.is_drawer:
            return
        self.canvas.delete("all")
        self.my_strokes.clear()
        self.send({"type": "clear"})

    def undo(self):
        if not self.is_drawer or not self.my_strokes:
            return
        sid = self.my_strokes.pop()
        self.canvas.delete(f"s{sid}")
        self.send({"type": "undo", "stroke_id": sid})

    def on_press(self, e):
        if not self.is_drawer:
            return
        self.drawing = True
        self.sx, self.sy = e.x, e.y
        self.stroke_id = next(_stroke_ids)
        self.my_strokes.append(self.stroke_id)

    def on_release(self, _e):
        self.drawing = False

    def on_motion(self, e):
        if not (self.is_drawer and self.drawing):
            return
        w = self.brush.get()
        self.canvas.create_line(self.sx, self.sy, e.x, e.y, fill=self.color,
                                 width=w, capstyle=tk.ROUND,
                                 tags=(f"s{self.stroke_id}",))
        self.send({"type": "draw", "x1": self.sx, "y1": self.sy,
                    "x2": e.x, "y2": e.y, "color": self.color,
                    "width": w, "stroke_id": self.stroke_id})
        self.sx, self.sy = e.x, e.y

    def submit_guess(self):
        text = self.guess_entry.get().strip()
        if not text:
            return
        self.guess_entry.delete(0, tk.END)
        self.send({"type": "guess", "guess": text})

    # -- server messages --

    def on_round_start(self, msg):
        self.is_drawer = msg.get("role") == "drawer"
        self.round_duration = msg.get("duration", config.ROUND_TIME_SECONDS)
        self.canvas.delete("all")
        self.my_strokes.clear()
        self.round_label.config(
            text=f"Round {msg.get('round')} / {msg.get('total_rounds')}")

        if self.is_drawer:
            self.status.config(text=f"🎯 Draw:  {msg['word']}", fg=SUCCESS)
            self.sub.config(text="You're the drawer this round")
            self.guess_row.pack_forget()
            self.show_tools(True)
            self.canvas.config(cursor="pencil")
            self.log(f"You are drawing: {msg['word']}", "ok")
        else:
            hint = "_ " * msg.get("word_length", 0)
            self.word_hint.config(text=hint.strip())
            self.status.config(text="🤔 Guess the drawing!", fg=TEAL)
            self.sub.config(text=f"{msg.get('drawer_name')} is drawing")
            self.guess_row.pack(fill="x", pady=(10, 0))
            self.show_tools(False)
            self.canvas.config(cursor="arrow")
            self.guess_entry.focus_set()
            self.log(f"{msg.get('drawer_name')} is drawing "
                      f"({msg.get('word_length')} letters)", "info")

    def show_tools(self, visible: bool):
        """Drawing tools are only meaningful for the current drawer."""
        if visible:
            self.waiting_panel.pack_forget()
            self.tools_panel.pack(side="left", fill="y", before=self.center)
        else:
            self.tools_panel.pack_forget()
            self.waiting_panel.pack(side="left", fill="y", before=self.center)

    def on_round_end(self, msg):
        word = msg.get("word", "")
        reason = msg.get("reason", "")
        note = {"all_guessed": "Everyone got it!",
                 "timeout": "Time's up!",
                 "drawer_left": "The drawer left."}.get(reason, "Round over.")
        self.status.config(text=f"{note}  The word was: {word}", fg=WARNING)
        self.sub.config(text="Next round starting...")
        self.guess_row.pack_forget()
        self.log(f"— {note} Word: {word}", "warn")
        self.timer_bar.place(relwidth=0)
        self.timer_num.config(text="—")

    def on_scores(self, scores):
        for w in self.score_box.winfo_children():
            w.destroy()
        for i, s in enumerate(scores):
            row = tk.Frame(self.score_box, bg=PANEL)
            row.pack(fill="x", pady=2)
            mine = s.get("player_id") == self.my_id
            col = TEAL if mine else ACCENT
            tk.Frame(row, bg=col, width=4, height=16).pack(side="left")
            name = f"  {i + 1}. {s['name']}" + ("  ← you" if mine else "")
            tk.Label(row, text=name, font=F(10, "bold" if mine else "normal"),
                      bg=PANEL, fg=TEXT, anchor="w").pack(side="left",
                                                            fill="x", expand=True)
            tk.Label(row, text=str(s["score"]), font=F(11, "bold"),
                      bg=PANEL, fg=col).pack(side="right")

    def on_timer(self, left):
        self.timer_num.config(text=str(left))
        frac = max(0.0, min(1.0, left / self.round_duration)) if self.round_duration else 0
        col = TEAL if left > 10 else (WARNING if left > 5 else DANGER)
        self.timer_bar.config(bg=col)
        self.timer_bar.place(relwidth=frac)

    def on_game_over(self, msg):
        scores = msg.get("scores", [])
        self.on_scores(scores)
        self.guess_row.pack_forget()
        self.round_label.config(text="Game over")
        self.timer_num.config(text="—")
        self.timer_bar.place(relwidth=0)
        if scores:
            win = scores[0]
            if win.get("player_id") == self.my_id:
                self.status.config(text="🏆 You win!", fg=SUCCESS)
            else:
                self.status.config(text=f"🏁 {win['name']} wins!", fg=ACCENT)
            self.sub.config(text=msg.get("reason", ""))
            self.log(f"Final: {win['name']} wins with {win['score']}", "ok")


# --- App shell -------------------------------------------------------------

class App:
    def __init__(self, root, host, port):
        self.root = root
        root.configure(bg=BG)
        root.geometry("1080x740")
        root.minsize(900, 640)
        root.title("SketchMesh")

        global FONT_FAMILY
        if FONT_FAMILY not in tkfont.families():
            FONT_FAMILY = "Helvetica"

        self.sock = None
        self.my_id = None
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.screen = None

        # --- session state, kept so a dropped socket can be resumed ---
        self.host = host
        self.port = port
        self.token = None        # issued by the server on first join
        self.room_code = None
        self.reconnecting = False
        self.reconnect_attempt = 0
        self.closing = False     # set on deliberate quit; suppresses retry
        self.recv_generation = 0  # invalidates stale receive threads

        self.connect_screen = ConnectScreen(root, host, port, self._connected)
        self.connect_screen.pack(fill="both", expand=True)
        self.lobby = None
        self.game = None

    # -- lifecycle --

    def _connected(self, sock, name, room_code, create):
        self.sock = sock
        self.host = self.connect_screen.host_var.get().strip() or self.host
        try:
            self.port = int(self.connect_screen.port_var.get().strip())
        except ValueError:
            pass
        self.connect_screen.destroy()

        self.lobby = LobbyScreen(self.root, self._start_game, self._quit)
        self.lobby.pack(fill="both", expand=True)

        join = {"type": "join", "name": name}
        if room_code:
            join["room_code"] = room_code
        if create:
            join["create"] = True
        self._send(join)

        self._start_recv_thread()
        self.root.after(40, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _send(self, msg):
        try:
            send_message(self.sock, msg)
        except OSError:
            pass

    def _start_recv_thread(self):
        """Each socket gets its own receive thread tagged with a
        generation number. When we reconnect, the generation bumps and any
        message still arriving from the old thread is discarded -- without
        this, a late '_lost' from the dead socket would immediately knock
        the freshly restored connection back offline."""
        self.recv_generation += 1
        gen = self.recv_generation
        sock = self.sock

        def loop():
            try:
                for msg in receive_messages(sock, config.RECV_BUFFER_SIZE):
                    if gen != self.recv_generation:
                        return
                    self.inbox.put(msg)
            except (ConnectionClosed, OSError):
                if gen == self.recv_generation:
                    self.inbox.put({"type": "_lost"})

        threading.Thread(target=loop, daemon=True).start()

    def _drain(self):
        try:
            while True:
                self._handle(self.inbox.get_nowait())
        except queue.Empty:
            pass
        self.root.after(40, self._drain)

    # -- reconnection --

    def _on_connection_lost(self):
        if self.closing or self.reconnecting:
            return
        # Without a token there's no session to resume (we never completed
        # a join), so retrying would just re-fail.
        if not self.token or not self.room_code:
            self._show_status("⚠️ Lost connection to server.", fatal=True)
            return
        self.reconnecting = True
        self.reconnect_attempt = 0
        self._attempt_reconnect()

    def _attempt_reconnect(self):
        if self.closing or not self.reconnecting:
            return
        self.reconnect_attempt += 1
        if self.reconnect_attempt > config.CLIENT_RECONNECT_ATTEMPTS:
            self.reconnecting = False
            self._show_status("⚠️ Could not reconnect. The session has ended.",
                               fatal=True)
            return

        self._show_status(
            f"🔄 Reconnecting… (attempt {self.reconnect_attempt}"
            f"/{config.CLIENT_RECONNECT_ATTEMPTS})")

        def try_once():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(config.CONNECT_TIMEOUT_SECONDS)
                sock.connect((self.host, self.port))
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                send_message(sock, {
                    "type": "rejoin",
                    "room_code": self.room_code,
                    "token": self.token,
                })
            except OSError:
                self.inbox.put({"type": "_reconnect_failed"})
                return
            self.inbox.put({"type": "_reconnect_socket", "sock": sock})

        threading.Thread(target=try_once, daemon=True).start()

    def _adopt_socket(self, sock):
        """Swap in a freshly connected socket on the GUI thread."""
        old = self.sock
        self.sock = sock
        self._start_recv_thread()  # bumps generation, retiring the old thread
        if old is not None and old is not sock:
            try:
                old.close()
            except OSError:
                pass

    def _schedule_retry(self):
        # Linear backoff: brief pauses are fine for a LAN game, and an
        # exponential curve would leave the player staring at a frozen
        # board for tens of seconds after a momentary blip.
        delay = min(config.CLIENT_RECONNECT_MAX_DELAY_MS,
                     config.CLIENT_RECONNECT_BASE_DELAY_MS * self.reconnect_attempt)
        self.root.after(delay, self._attempt_reconnect)

    def _show_status(self, text, fatal=False):
        if self.game:
            self.game.status.config(text=text, fg=DANGER if fatal else WARNING)
            if fatal:
                self.game.guess_row.pack_forget()
        elif self.lobby:
            self.lobby.hint.config(text=text, fg=DANGER if fatal else WARNING)

    def _start_game(self):
        self._send({"type": "start_game"})

    def _quit(self):
        self.closing = True
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.root.destroy()

    def _to_game(self):
        if self.game is None:
            if self.lobby:
                self.lobby.destroy()
                self.lobby = None
            self.game = GameScreen(self.root, self._send, self.my_id)
            self.game.pack(fill="both", expand=True)

    # -- dispatch --

    def _handle(self, msg):
        t = msg.get("type")

        if t == "joined":
            self.my_id = msg["player_id"]
            self.room_code = msg.get("room_code", self.room_code)
            # The token is what makes this session resumable; without it a
            # dropped socket means a lost seat.
            if msg.get("token"):
                self.token = msg["token"]
            if self.lobby:
                self.lobby.set_code(msg["room_code"])
            if msg.get("resumed"):
                self.reconnecting = False
                self.reconnect_attempt = 0

        elif t == "lobby_update":
            if self.lobby:
                self.lobby.update_state(msg, self.my_id)
            elif self.game:
                self.game.on_scores([
                    {"player_id": p["player_id"], "name": p["name"],
                     "score": p["score"]}
                    for p in sorted(msg.get("players", []),
                                     key=lambda x: x["score"], reverse=True)
                ])

        elif t == "round_start":
            self._to_game()
            self.game.on_round_start(msg)

        elif t == "round_end":
            if self.game:
                self.game.on_round_end(msg)

        elif t == "draw_data":
            if self.game and all(k in msg for k in ("x1", "y1", "x2", "y2")):
                tags = (f"s{msg['stroke_id']}",) if "stroke_id" in msg else ()
                self.game.canvas.create_line(
                    msg["x1"], msg["y1"], msg["x2"], msg["y2"],
                    fill=msg.get("color", "#000"), width=msg.get("width", 3),
                    capstyle=tk.ROUND, tags=tags)

        elif t == "undo_stroke":
            if self.game and msg.get("stroke_id") is not None:
                self.game.canvas.delete(f"s{msg['stroke_id']}")

        elif t == "clear_canvas":
            if self.game:
                self.game.canvas.delete("all")

        elif t == "guess_result":
            if not self.game:
                return
            if msg.get("result") == "correct":
                self.game.status.config(
                    text=f"✅ Correct!  +{msg.get('gained', 0)}", fg=SUCCESS)
                self.game.log(f"You guessed it! +{msg.get('gained', 0)}", "ok")
                self.game.guess_row.pack_forget()
            else:
                self.game.log(f"✗ {msg.get('guess', '')}", "bad")

        elif t == "player_guessed":
            if self.game:
                self.game.log(f"✓ {msg['name']} guessed it "
                               f"(+{msg.get('gained', 0)})", "ok")

        elif t == "score_update":
            if self.game:
                self.game.on_scores(msg.get("scores", []))

        elif t == "timer_update":
            if self.game:
                self.game.on_timer(msg.get("time_left", 0))

        elif t == "chat":
            if self.game:
                self.game.log(f"{msg['name']}: {msg['text']}", "info")

        elif t == "player_left":
            if self.game:
                self.game.log(f"{msg['name']} left", "warn")

        elif t == "game_over":
            self._to_game()
            self.game.on_game_over(msg)

        elif t == "error":
            text = msg.get("message", "Something went wrong.")
            if self.lobby:
                self.lobby.hint.config(text=text, fg=DANGER)
            elif self.game:
                self.game.log(text, "bad")

        elif t == "state_sync":
            self._apply_snapshot(msg)

        elif t == "player_reconnected":
            if self.game:
                self.game.log(f"{msg['name']} reconnected", "ok")

        elif t == "_reconnect_socket":
            self._adopt_socket(msg["sock"])

        elif t == "_reconnect_failed":
            self._schedule_retry()

        elif t == "_lost":
            self._on_connection_lost()

    def _apply_snapshot(self, snap):
        """Rebuild the whole client view from a server state snapshot.

        Used both when reconnecting and when joining a game already in
        progress -- both cases are "this client knows nothing and needs
        catching up", so they share one code path.
        """
        self.my_id = snap.get("your_id", self.my_id)
        self.room_code = snap.get("room_code", self.room_code)

        if snap.get("state") != "playing":
            # Back in the lobby (or the game finished while we were away).
            if self.lobby:
                self.lobby.update_state({
                    "room_code": self.room_code,
                    "host_id": snap.get("host_id"),
                    "players": [
                        {"player_id": s["player_id"], "name": s["name"],
                         "score": s["score"], "connected": True}
                        for s in snap.get("scores", [])
                    ],
                }, self.my_id)
            return

        self._to_game()
        g = self.game
        g.my_id = self.my_id

        # Rebuild the round exactly as round_start would have set it up.
        g.on_round_start({
            "role": snap.get("role", "guesser"),
            "word": snap.get("word"),
            "word_length": snap.get("word_length", 0),
            "round": snap.get("round"),
            "total_rounds": snap.get("total_rounds"),
            "drawer_name": snap.get("drawer_name", ""),
            "duration": snap.get("duration", config.ROUND_TIME_SECONDS),
        })

        # Replay every stroke drawn while we were away.
        for s in snap.get("strokes", []):
            if all(k in s for k in ("x1", "y1", "x2", "y2")):
                tags = (f"s{s['stroke_id']}",) if "stroke_id" in s else ()
                g.canvas.create_line(
                    s["x1"], s["y1"], s["x2"], s["y2"],
                    fill=s.get("color", "#000"), width=s.get("width", 3),
                    capstyle=tk.ROUND, tags=tags)

        g.on_scores(snap.get("scores", []))
        g.on_timer(snap.get("time_left", 0))

        if snap.get("already_guessed"):
            # They'd already solved it before dropping; don't offer the
            # guess box again or show it as still unsolved.
            g.guess_row.pack_forget()
            g.status.config(text=f"✅ You already guessed: {snap.get('word', '')}",
                             fg=SUCCESS)

        g.log("Reconnected — game state restored", "ok")


def main():
    p = argparse.ArgumentParser(description="SketchMesh client")
    p.add_argument("--host", default=config.CLIENT_CONNECT_IP)
    p.add_argument("--port", type=int, default=config.CLIENT_CONNECT_PORT)
    args = p.parse_args()

    root = tk.Tk()
    App(root, args.host, args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
