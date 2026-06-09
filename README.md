# LAN-Based Two-Player Drawing & Guessing Game

A real-time multiplayer drawing game built using **Python TCP sockets**, **multithreading**, and a custom **Tkinter GUI**.  
Two players connect over a **local network (LAN)**. One player draws a word, and the other guesses it. The game includes scoring, round timer, automatic role switching, and synchronized drawing across devices.

---

## Features

- Fully Python-based – no browser required  
- Real-time drawing synchronization over raw TCP sockets  
- Automatic role switching (Drawer ↔ Guesser)  
- 30-second countdown timer per round  
- 3 rounds per player (total 6 rounds)  
- Drawing tools:
  - Pencil
  - Color picker
  - Eraser
  - Clear canvas button
- Scoreboard and game-over screen  
- Multithreaded client/server communication  

---

## Tech Stack

- Python  
- Tkinter (GUI)  
- Socket Programming (TCP)  
- Multithreading  
- JSON based messaging  

---

## Project Structure

```
project/
│
├── server.py        # runs once on the host device
├── client.py        # runs on each player’s device
├── README.md
```

---

## Requirements

Install Python 3.11 or higher.

No external libraries required except Tkinter (pre-installed on Windows).

---

## Setup Instructions

### 1. Connect both devices to the **same LAN network**

This game only works over LAN/Wi-Fi hotspots.

---

### 2. Find the server device IP

On Windows (server laptop):

```
ipconfig
```

Look for your IPv4 address, e.g.:

```
IPv4 Address. . . . . . . . . . . : 192.168.1.21
```

---

### 3. Update the IP in both files

In `server.py`:

```python
HOST = "192.168.1.21"
PORT = 12345
```

In `client.py`:

```python
SERVER_IP = "192.168.1.21"
SERVER_PORT = 12345
```

⚠ Use the same IP for both clients.

---

## How to Run the Game

### Step 1: Start the server (run once)

On the host machine:

```
python server.py
```

You should see:

```
[*] Server ready on 192.168.1.21:12345
```

---

### Step 2: Start the client (run once per device)

On **both** player devices:

```
python client.py
```

When both connect, the game starts automatically.

---

## Game Rules

- One player is assigned as **Drawer** and sees a secret word.  
- The other player is the **Guesser** and tries to guess it.  
- Correct guess:
  - Drawer gets **5 points**
  - Guesser gets **10 points**
- After each round, roles are swapped.
- Total rounds = 3 per player (6 rounds).
- Winner is shown at the end!

---

## How It Works Internally

### Server handles:

- Player connections  
- Round control  
- Word assignment  
- Timer thread  
- Score calculation  
- Broadcasting drawing + game events  

### Client handles:

- GUI + drawing  
- Sending strokes + guesses  
- Rendering opponent drawing  
- UI updates  

Communication uses simple JSON messages:

```json
{
  "type": "draw_data",
  "x1": 30, "y1": 50,
  "x2": 40, "y2": 60
}
```

---

## Troubleshooting

### ConnectionRefusedError

Make sure:

✔ Server is running  
✔ Correct IP is used  
✔ Both devices are on same Wi-Fi / Hotspot  
✔ Firewall isn't blocking Python  

---

## Future Enhancements (optional ideas)

- Chat feature  
- Multiple rounds menu  
- Word difficulty settings  
- Host-selectable custom words  

---

## Credits

Developed as a **Computer Networks course Mini-Project** using Python, Sockets, and Tkinter.

---

## Licence

This project is free to use for learning and academic purposes.
