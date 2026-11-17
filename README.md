# SubLive

Multilingual live subtitle operator. Prepare cues in up to three languages, fire them from a browser, and send them to separate display screens, YouTube Live captions, and an SRT recording, all in sync.

Runs in Docker. The operator UI and display screens are plain web pages.

## Features

- **Cue table**: one row per cue, one column per language, inline editing, free-text cue numbers (`1`, `1.1`, `10a`)
- **Three outputs**: L1 (also sent to YouTube), L2 and L3 on separate display windows, or L2 + L3 stacked on one screen
- **Groups**: collapsible folders; collapsed groups are skipped during playback
- **Multi-select**: Shift+click range, Cmd/Ctrl+A, batch disable / enable / delete / group, drag to reorder
- **Show mode**: locks the sidebar and text editing, disables multi-select and drag, keeps the active cue centred. Record and transport stay available
- **SRT recording**: records real in/out times per cue, exports one `.srt` per language
- **LTC timecode**: decodes SMPTE LTC from an audio input in the browser (24 / 25 / 29.97 / 30 fps), with signal meter and SYNCED / FREEWHEEL indicator. Experimental, see [Known limitations](#known-limitations)
- **YouTube live captions**: posts L1 to a YouTube caption ingestion URL, with configurable delay and clock-drift correction
- **OSC control**: trigger next / previous / start / blackout from QLab, TouchOSC, Lemur or any OSC device on the network
- **Display formatting**: font (sans-serif, serif, monospace), size, bold, italic and colour per language
- **SRT import**: load an SRT file into any language column
- **Projects**: save and open `.sublive` files from your machine
- **Built-in help**: `/help` opens full documentation in a new tab

## Quick start

Requires [Docker](https://www.docker.com/products/docker-desktop/).

```bash
git clone <your-repo-url>
cd sublive
docker compose up --build
```

Then open:

| URL | Purpose |
|-----|---------|
| `http://localhost:8000` | Operator UI |
| `http://localhost:8000/display/0` | Screen 1, Language 1 |
| `http://localhost:8000/display/1` | Screen 2, Language 2 (or L2 + L3 combined) |
| `http://localhost:8000/display/2` | Screen 3, Language 3 |
| `http://localhost:8000/help` | Help |

The **▣ Scr1 / Scr2 / Scr3** buttons in the operator UI open each display in its own window. Drag each window to its monitor and go fullscreen.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` / `→` | Next cue |
| `←` | Previous cue |
| `B` | Toggle blackout |
| `Cmd/Ctrl + A` | Select all cues |
| `Cmd/Ctrl + S` | Download project file |

Shortcuts are ignored while typing in a text field (except in show mode, where editing is locked).

## Typical show workflow

1. Build and proof the cue list, set display formats, enter the YouTube URL if needed
2. Click **▶ Show mode**
3. Click **⏺ Record**
4. Fire cues (click, Space, or OSC)
5. Click **⏹ Stop rec**, then download **↓ L1 / L2 / L3**

## OSC control

SubLive listens for OSC over UDP on port **53002** by default (changeable in the sidebar). A small green dot in the top bar flashes whenever a message arrives.

| Address | Argument | Action |
|---------|----------|--------|
| `/sublive/next` | none | Next cue |
| `/sublive/prev` | none | Previous cue |
| `/sublive/start` | none | First cue |
| `/sublive/blackout` | none | Blackout on |
| `/sublive/clear` | none | Blackout off (restores the current cue) |
| `/sublive/go` | int N | Fire cue at index N (0-based) |
| `/sublive/cue/N` | none | Fire cue at index N (0-based) |

Index N counts only enabled cues in visible (non-collapsed) groups.

**QLab 5**: Workspace Settings → Network → add a patch (host = SubLive machine IP, port = `53002`, UDP). Create a Network cue, choose the patch, Custom OSC message `/sublive/next`.

**TouchOSC / Lemur**: set host to the SubLive machine IP, send port `53002`, and map a button to `/sublive/next`.

If you change the port, update the UDP mapping in `docker-compose.yml` and restart the container.

## YouTube live captions

1. YouTube Studio → Go Live → Edit stream → Advanced settings → Closed captions → **POST captions to URL**
2. Copy the ingestion URL into the SubLive sidebar and tick **Send L1 to YouTube**
3. Use **Test connection** to verify

Each cue is sent as a POST to `<url>&seq=N` with the body:

```
<UTC ISO timestamp>
<caption text>
```

`seq` increments with every POST and resets when the container restarts. The top bar shows `YT ✓ #seq` plus the measured clock drift. The **Caption delay** field (-3 to +5 s) shifts the timestamp to compensate for encoder latency.

The ingestion URL is unique to each broadcast, so paste a fresh one every time you go live.

## LTC timecode

1. Connect an LTC source to an audio input (line level)
2. Tick **Lock to LTC**, allow microphone access, choose the audio device and frame rate
3. Watch the meter and aim for a clear signal without clipping

The decoder runs entirely in the browser using the Web Audio API. When locked, SRT timestamps use LTC time instead of wall-clock time.

Browsers only allow audio capture on `localhost` or HTTPS pages. If you open the operator UI from another machine over plain `http://`, the LTC option will not get microphone access.

## Project files

**↓ Project** saves everything (cues, language names, formats, YouTube and OSC settings) as a `.sublive` JSON file to your machine. **↑ Project** loads one back.

## Project structure

```
sublive/
├── main.py              FastAPI backend, WebSocket hub, OSC UDP server, YouTube poster, SRT builder
├── templates/
│   ├── operator.html    Operator UI
│   ├── display.html     Display screen page
│   └── help.html        Help page
├── Dockerfile
├── docker-compose.yml   Ports 8000 (web) and 53002/udp (OSC)
└── requirements.txt
```

**Stack**: Python, FastAPI, uvicorn, httpx, plain HTML / JavaScript (no build step). OSC is parsed directly from UDP packets, with no external OSC library.

## Ports

| Port | Protocol | Use |
|------|----------|-----|
| 8000 | TCP | Web UI and WebSocket |
| 53002 | UDP | OSC input |

## Known limitations

- **LTC decoding is experimental.** It has been tested against a 25 fps / 48 kHz test file and a live BlackHole input, but behaviour with other sources has not been fully verified. Check the signal meter and the timecode shown before relying on it for a show.
- **YouTube caption overlap.** When cues are fired in very quick succession, YouTube may display parts of more than one cue at once. This is caused by how YouTube buffers incoming captions. Leaving a few seconds between cues avoids it.
- Each text field is limited to 58 characters.
- State is held in server memory. Restarting the container clears the live cue list, so save your project file before shutting down.
- There is no authentication. Run it on a trusted network only.

## License

Add your license here.
