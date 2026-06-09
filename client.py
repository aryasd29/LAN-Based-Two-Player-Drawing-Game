import socket
import threading
import tkinter as tk
from tkinter import colorchooser
import json

SERVER_IP = "192.168.1.21"
SERVER_PORT = 12345

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.connect((SERVER_IP, SERVER_PORT))

root = tk.Tk()
root.title("🎨 Drawing Game")
root.configure(bg="#f0f0f0")
root.geometry("850x700")

canvas = tk.Canvas(root, width=800, height=500, bg="white", bd=2, relief="sunken", highlightthickness=0)
canvas.pack(pady=(20, 10))

word_label = tk.Label(root, text="", font=("Helvetica", 18, "bold"), fg="#333", bg="#f0f0f0")
word_label.pack(pady=5)

timer_label = tk.Label(root, text="Time: ", font=("Helvetica", 14), fg="#d62728", bg="#f0f0f0")
timer_label.pack()

guess_frame = tk.Frame(root, bg="#f0f0f0")
guess_entry = tk.Entry(guess_frame, font=("Helvetica", 14), width=30)
guess_button = tk.Button(guess_frame, text="Guess", font=("Helvetica", 12, "bold"),
                         bg="#1f77b4", fg="white", padx=10, command=lambda: submit_guess())
guess_entry.pack(side=tk.LEFT, padx=5)
guess_button.pack(side=tk.LEFT)
guess_frame.pack(pady=10)

color = "black"
brush_size = 3
is_drawing = False
is_drawer = False
start_x, start_y = None, None

def choose_color():
    global color
    color = colorchooser.askcolor(color)[1]

def set_eraser():
    global color
    color = "white"

def clear_canvas():
    canvas.delete("all")
    if is_drawer:  
        send({"clear": True})

def on_press(event):
    global is_drawing, start_x, start_y
    if is_drawer:
        is_drawing = True
        start_x, start_y = event.x, event.y

def on_release(event):
    global is_drawing
    is_drawing = False

def on_motion(event):
    global start_x, start_y
    if is_drawer and is_drawing:
        canvas.create_line(start_x, start_y, event.x, event.y, fill=color, width=brush_size, capstyle=tk.ROUND)
        send({
            "x1": start_x, "y1": start_y,
            "x2": event.x, "y2": event.y,
            "color": color, "width": brush_size
        })
        start_x, start_y = event.x, event.y

def send(data):
    client.sendall((json.dumps(data) + "\n").encode())

def submit_guess():
    guess = guess_entry.get()
    if guess.strip():
        send({"guess": guess})
        guess_entry.delete(0, tk.END)

canvas.bind("<ButtonPress-1>", on_press)
canvas.bind("<B1-Motion>", on_motion)
canvas.bind("<ButtonRelease-1>", on_release)

btn_frame = tk.Frame(root, bg="#f0f0f0")
btn_frame.pack(pady=10)

tk.Button(btn_frame, text="🎨 Color", font=("Helvetica", 11), bg="#2ca02c", fg="white",
          command=choose_color, width=10).pack(side=tk.LEFT, padx=6)

tk.Button(btn_frame, text="🧼 Eraser", font=("Helvetica", 11), bg="#ff7f0e", fg="white",
          command=set_eraser, width=10).pack(side=tk.LEFT, padx=6)

tk.Button(btn_frame, text="🗑 Clear", font=("Helvetica", 11), bg="#9467bd", fg="white",
          command=clear_canvas, width=10).pack(side=tk.LEFT, padx=6)

def receive():
    buffer = ""
    while True:
        try:
            data = client.recv(1024).decode()
            if not data:
                break
            buffer += data
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                msg = json.loads(line)
                handle_message(msg)
        except Exception as e:
            print("Connection error:", e)
            break

def handle_message(msg):
    global is_drawer
    msg_type = msg.get("type")

    if msg_type == "your_turn":
        is_drawer = True
        word_label.config(text=f"🎯 Draw: {msg['word']}", fg="#2ca02c")
        guess_frame.pack_forget()
        canvas.config(cursor="pencil")

    elif msg_type == "opponent_turn":
        is_drawer = False
        word_label.config(text="🤔 Guess the drawing!", fg="#1f77b4")
        guess_frame.pack(pady=10)
        canvas.config(cursor="arrow")

    elif msg_type == "draw_data":
        canvas.create_line(msg["x1"], msg["y1"], msg["x2"], msg["y2"],
                           fill=msg["color"], width=msg["width"], capstyle=tk.ROUND)

    elif msg_type == "guess_result":
        result = msg.get("result")
        word = msg.get("word", "")
        if result == "correct":
            word_label.config(text=f"✅ Correct! The word was: {word}", fg="#2ca02c")
        elif result == "timeout":
            word_label.config(text=f"⌛ Time's up! Word was: {word}", fg="#ff7f0e")
        else:
            word_label.config(text=f"❌ Wrong! Try again.", fg="#d62728")

    elif msg_type == "clear_canvas":
        canvas.delete("all")

    elif msg_type == "timer_update":
        time_left = msg.get("time_left", 0)
        timer_label.config(text=f"⏰ Time: {time_left}")

    elif msg_type == "game_over":
        p1, p2 = msg["scores"]
        word_label.config(text=f"🏁 Game Over!\nPlayer 1: {p1} | Player 2: {p2}", fg="#9467bd")
        guess_frame.pack_forget()
        canvas.config(cursor="arrow")

threading.Thread(target=receive, daemon=True).start()
root.mainloop()