# HX Streamer Suite

This folder now contains both apps:

- `main.py`: sender (capture screen center, encode JPEG, send via TCP/UDP)
- `receiver.py`: receiver (listen on LAN TCP/UDP, decode JPEG frames, preview)

Both apps use a matching UI style (frameless window, dark/light theme, left controls + right preview).

## Requirements

Install Python 3.11+ and dependencies:

```powershell
pip install -r requirements.txt
```

## Run

Start sender:

```powershell
python main.py
```

Start receiver:

```powershell
python receiver.py
```

## Build EXE

Build sender:

```powershell
pyinstaller --clean -y HX_Streamer_Pro.spec
```

Build receiver:

```powershell
pyinstaller --clean -y HX_Receiver_Pro.spec
```

Generated files:

- `dist/HX_Streamer_Pro.exe`
- `dist/HX_Receiver_Pro.exe`

## Protocol Notes

TCP frame format:

1. 4-byte big-endian unsigned length
2. JPEG binary payload

UDP mode sends one JPEG per datagram.

## Config Paths

Sender config:

- `%APPDATA%\\HX Streamer Pro\\config.json`

Receiver config:

- `%APPDATA%\\HX Streamer Receiver\\config.json`
