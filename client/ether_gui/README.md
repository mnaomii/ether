# ether_gui — "Network map" client

PyQt6 GUI for Ether.

Run everything from `client/`:

```
pip install -r requirements.txt
python -m ether_gui --demo          # mock backend, no server needed (password "wrong" shows the login error)
python -m ether_gui                 # uses ether_core.net if importable, else falls back to demo
python -m ether_gui --theme dark
```

Place `ether_gui/` next to `ether_core/` in `client/`. No networking code lives here.

## Files

| File | Role |
|---|---|
| `__main__.py` | argparse, `QApplication`, `qasync.QEventLoop` |
| `app.py` | startup flow: discovery dialog → `client.connect` → main window; retries on login failure |
| `discovery_dialog.py` | re-broadcasts every ~1 s, lists servers live, manual IP/port, nickname byte counter (≤31 B), password (never saved) |
| `main_window.py` | top bar (server chip, ping bars, theme toggle), splitter (map / chat), status bar; routes core events |
| `map_view.py` | `QGraphicsView` network map: server node, user ring, typing rings, message pulses, ghost servers, transfers overlay |
| `chat_panel.py` | room tabs + Rooms menu (join/leave), `QTextBrowser` log, typing line, composer |
| `theme.py` | `LIGHT` / `DARK` palettes + one QSS template. Toggle: button or Ctrl+Shift+L, saved in `QSettings` |
| `contract.py` | re-exports `ether_core.events`; GUI-only `ConnectTarget`, `ERROR_TEXT` |
| `mock_client.py` | scripted fake core (typing, joins, file share, pings) |
| `util.py` | `spawn()` for fire-and-forget coroutines from slots, limits, formatters |

## Contract with `ether_core`

`MainWindow.handle_event` dispatches on the **event class name**. The dataclasses live in `ether_core/events.py` (single source of truth, no networking):

`Welcome(session_id, rooms[(id,name)])`, `MsgReceived(room_id, unix_ms, sender, text)`, `History(room_id, messages[MsgReceived])`, `Users(room_id, nicks)`, `UserEvt(room_id, joined, nick)`, `TypingSeen(room_id, nick)`, `FileEvt(room_id, file_id, filename, size, sender)`, `Pong(rtt_ms)`, `Error(code, message)`, `Disconnected(reason)`.

Client API used: `EtherClient(on_event=...)`, `await connect(host, port, nick, password)` (raises `LoginError` on login ERROR; a new nickname creates the account), `join`, `leave`, `say`, `typing`, `upload(room_id, path, progress=cb(done,total))`, `download(file_id, dest, progress=cb)`, `close()`, attributes `udp_port`, `session_id`. `discover(timeout=1.0)` returns a list of objects with `name, host, tcp_port, users_online`.

## Notes for integration

- Everything runs on the qasync loop; slots call coroutines through `util.spawn`. Never block in a slot.
- `_join` creates the room tab *before* awaiting `client.join`, so `History`/`Users` arriving during the await have somewhere to land.
- Map pulses are cosmetic but honest: TCP messages animate sender → server → room members; typing animates in UDP amber toward you only.
- Only your own link shows a ping label: the protocol measures only your RTT.
- Download filenames go through `os.path.basename` before building a save path.

## TODO (not built yet)

- Reconnect after `Disconnected`. Today: status goes red, input disables.
- Optional "Wire" dock showing raw frames, if the core exposes a frame-log hook.
- Unit tests for `ChatPanel` HTML rendering and `DiscoveryDialog` validation (pytest-qt).
- App icon, Windows AppUserModelID, packaging (PyInstaller).
- Not run against a real server yet. Smoke-test `--demo` on Windows first.
