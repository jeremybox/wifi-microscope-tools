# Andonstar Wi-Fi microscope (PC client)

Python client for Andonstar scopes that expose the Novatek-style HTTP API
(`/?custom=1&cmd=…`) and multipart MJPEG on TCP **8192**.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Join the scope Wi-Fi AP from your OS (SSID like `Andonstar-…`, password often `12345678`).
Default scope IP is `192.168.1.254`.

If your home LAN is also `192.168.1.0/24`, use a second Wi-Fi adapter (or USB dongle)
for the scope and add a host route to `192.168.1.254` via that interface.

## Usage

```bash
python andonstar.py status
python andonstar.py snap -o board.jpg
python andonstar.py view
python andonstar.py view --fullscreen --save ./frames
python andonstar.py keepalive     # hold stream open for OBS/VLC
python andonstar.py cmd --code 3029
```

Esc or Ctrl-C quits live view / keepalive.

## OBS (native Media Source)

1. Join the scope Wi‑Fi (second adapter is fine).
2. In a terminal: `python andonstar.py keepalive` (leaves the MJPEG port open).
3. In OBS → **Sources** → **+** → **Media Source**
   - Uncheck **Local File**
   - **Input:** `http://192.168.1.254:8192/`
   - **Input Format:** `mpjpeg`
   - Uncheck **Close file when inactive**
   - Optional: raise network buffer if it stutters
4. Click OK — you should see the live feed.

If Media Source stays black, try **Browser Source** with the same URL, or VLC as a Media Source via `http://192.168.1.254:8192/`.
