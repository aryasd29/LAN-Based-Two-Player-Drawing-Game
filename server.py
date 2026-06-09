import socket
import threading
import json
import random
import time

HOST = "192.168.1.21"
PORT = 12345
ROUNDS_PER_PLAYER = 3
ROUND_TIME = 30

clients = []
roles = {}
scores = {}
words = ["fish","apple", "house", "tree", "car", "ball", "dog", "cat", "star", "book", "phone","hat", "shoe", "computer", "pencil", "cup", "table", "chair", "window", "door", "sun", "moon"]

current_round = 0
current_word = ""
timer_thread = None
stop_timer = threading.Event()

def broadcast(data, exclude=None):
    for client in clients:
        if client != exclude:
            try:
                client.sendall((json.dumps(data) + "\n").encode())
            except:
                continue

def handle_client(client, addr):
    global current_round, timer_thread
    
    print(f"[+] {addr} connected")
    if len(clients) == 2:
        start_game()

    buffer = ""
    while True:
        try:
            data = client.recv(1024).decode()
            if not data:
                break
            buffer += data
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                try:
                    msg = json.loads(line)
                    handle_message(client, msg)
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            print(f"[-] Connection error: {e}")
            break

    clients.remove(client)
    if len(clients) < 2 and timer_thread:
        stop_timer.set()

def handle_message(client, msg):
    if "guess" in msg:
        handle_guess(client, msg["guess"])
    elif all(k in msg for k in ("x1", "y1", "x2", "y2")):
        broadcast({"type": "draw_data", **msg}, exclude=client)
    elif "clear" in msg:
        if roles.get(client) == "drawer":  # Only drawer can clear
            broadcast({"type": "clear_canvas"})

def handle_guess(client, guess):
    global timer_thread
    
    if guess.lower() == current_word.lower() and not stop_timer.is_set():
        stop_timer.set()
        
        # Award points
        drawer = next(c for c in clients if roles.get(c) == "drawer")
        scores[client] = scores.get(client, 0) + 10
        scores[drawer] = scores.get(drawer, 0) + 5
        
        # Notify players
        result_msg = {
            "type": "guess_result",
            "result": "correct",
            "word": current_word
        }
        broadcast(result_msg)
        
        time.sleep(2)  # Brief pause before next round
        next_round()

def start_game():
    global current_round
    current_round = 0
    scores.clear()
    next_round()

def next_round():
    global current_round, current_word, timer_thread
    
    if current_round >= ROUNDS_PER_PLAYER * 2:
        broadcast({
            "type": "game_over",
            "scores": [scores.get(c, 0) for c in clients]
        })
        return

    # Reset timer flag
    stop_timer.clear()
    
    # Setup roles
    current_word = random.choice(words)
    drawer = clients[current_round % 2]
    guesser = clients[(current_round + 1) % 2]
    roles[drawer] = "drawer"
    roles[guesser] = "guesser"

    # Notify players
    drawer.sendall(json.dumps({
        "type": "your_turn",
        "word": current_word
    }).encode() + b"\n")

    guesser.sendall(json.dumps({
        "type": "opponent_turn"
    }).encode() + b"\n")

    broadcast({"type": "clear_canvas"})

    # Start timer
    timer_thread = threading.Thread(target=run_timer)
    timer_thread.start()
    
    current_round += 1

def run_timer():
    for t in range(ROUND_TIME, 0, -1):
        if stop_timer.is_set():
            return
            
        broadcast({
            "type": "timer_update",
            "time_left": t
        })
        time.sleep(1)
    
    if not stop_timer.is_set():
        stop_timer.set()
        broadcast({
            "type": "guess_result",
            "result": "timeout",
            "word": current_word
        })
        time.sleep(2)
        next_round()

if __name__ == "__main__":
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((HOST, PORT))
    server.listen()
    print(f"[*] Server ready on {HOST}:{PORT}")

    try:
        while True:
            client, addr = server.accept()
            clients.append(client)
            threading.Thread(target=handle_client, args=(client, addr)).start()
    finally:
        server.close()