# Andonstar Wi-Fi microscope (PC client)

Python client + local web UI for Andonstar scopes that expose the Novatek-style
HTTP API (`/?custom=1&cmd=…`) and multipart MJPEG on TCP **8192**.

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

## Web UI (recommended)

```bash
python andonstar.py serve
# open http://127.0.0.1:23903/
python andonstar.py serve --host 0.0.0.0 --port 23903 --ip 192.168.1.254
```

The UI can:

- show a live MJPEG preview (proxied through the local server)
- enable / disable the stream (video mode vs playback mode)
- switch photo / video / playback
- set photo & video resolutions (SD capture — live Wi‑Fi preview stays ~640p)
- exposure, brightness, flip/rotate, HDR, watermark, loop record
- photo snap + start/stop SD recording
- send raw cmds (dangerous ones blocked)

## CLI

```bash
python andonstar.py status
python andonstar.py snap -o board.jpg
python andonstar.py view
python andonstar.py keepalive     # hold stream open for OBS/VLC
python andonstar.py cmd --code 3029
```

## OBS (native Media Source)

1. Join the scope Wi‑Fi (second adapter is fine).
2. Enable stream from the web UI, or run `python andonstar.py keepalive`.
3. In OBS → **Sources** → **+** → **Media Source**
   - Uncheck **Local File**
   - **Input:** `http://192.168.1.254:8192/` (or `http://127.0.0.1:23903/stream` via the proxy)
   - **Input Format:** `mpjpeg`
   - Uncheck **Close file when inactive**
4. Click OK.

Live Wi‑Fi preview is firmware-capped (~640×380 / ~640×480). Full resolution needs HDMI capture or USB / SD stills.
