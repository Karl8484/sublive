"""
SubLive — main.py
FastAPI + WebSocket + raw asyncio UDP OSC.

YouTube live caption POST (confirmed working format):
  POST <url>&seq=N
  Content-Type: text/plain
  Body: <ISO-UTC-timestamp>\n<caption text>\n
  (timestamp in the BODY, no X-Timestamp header)

LTC: decoded in browser via Web Audio API, ticks pushed here via WS.
OSC: raw asyncio DatagramProtocol, default port 53002.
"""
from __future__ import annotations
import json, time, asyncio, struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, Response

app = FastAPI()
PROJECTS_DIR = Path("/app/projects"); PROJECTS_DIR.mkdir(exist_ok=True)

# ── State ─────────────────────────────────────────────────────────────────────
state: dict = {
    "cues": [],
    "current_index": -1,
    "blackout": False,
    "recording": False,
    "record_start": None,
    "ltc_enabled": False,
    "ltc_timecode": 0.0,
    "combine_screens": False,
    "lang_names": ["English", "French", "Dutch"],
    "lang_format": {
        "0": {"size":52,"bold":True,"italic":False,"color":"#ffffff","font":"sans-serif"},
        "1": {"size":52,"bold":True,"italic":False,"color":"#ffffff","font":"sans-serif"},
        "2": {"size":52,"bold":True,"italic":False,"color":"#ffffff","font":"sans-serif"},
    },
    "yt_url": "",
    "yt_enabled": False,
    "yt_delay": 0.0,
    "osc_port": 53002,
    "show_mode": False,
}

srt_log: list[dict] = []
srt_current: Optional[dict] = None
_yt_seq: int = 0
_osc_transport = None


def _now() -> float:
    if state["ltc_enabled"]: return state["ltc_timecode"]
    if state["record_start"] is None: return 0.0
    return time.time() - state["record_start"]

def _safe_state() -> dict:
    return {k: v for k, v in state.items() if k != "record_start"}

# ── WebSocket manager ─────────────────────────────────────────────────────────
class WsManager:
    def __init__(self):
        self.clients: dict[str, list[WebSocket]] = {
            "operator":[], "display0":[], "display1":[], "display2":[]
        }
    async def connect(self, ws: WebSocket, ctype: str):
        await ws.accept()
        self.clients.setdefault(ctype, []).append(ws)
        await self._send(ws, {"type":"INIT","state":_safe_state()})
    def disconnect(self, ws: WebSocket, ctype: str):
        lst = self.clients.get(ctype, [])
        if ws in lst: lst.remove(ws)
    async def broadcast(self, msg: dict, ctype: str | None = None):
        raw = json.dumps(msg)
        targets = list(self.clients.get(ctype,[])) if ctype else \
                  [ws for lst in self.clients.values() for ws in lst]
        dead = []
        for ws in targets:
            try: await ws.send_text(raw)
            except: dead.append(ws)
        for ws in dead: self.disconnect(ws, ctype or "operator")
    async def _send(self, ws, msg):
        try: await ws.send_text(json.dumps(msg))
        except: pass

mgr = WsManager()

@app.websocket("/ws/{ctype}")
async def ws_ep(ws: WebSocket, ctype: str):
    await mgr.connect(ws, ctype)
    try:
        while True:
            await handle(json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        mgr.disconnect(ws, ctype)

# ── Message handler ───────────────────────────────────────────────────────────
async def handle(msg: dict):
    t = msg.get("type")
    if t == "SEND_CUE":   await do_send_cue(msg["index"])
    elif t == "BLACKOUT": await do_blackout(msg.get("on", True))
    elif t == "UPDATE_CUES":
        state["cues"] = msg["cues"]
        await mgr.broadcast({"type":"CUES","cues":state["cues"]})
    elif t == "UPDATE_STATE":
        for k in ("lang_names","lang_format","yt_url","yt_enabled","yt_delay",
                  "combine_screens","osc_port","show_mode"):
            if k in msg: state[k] = msg[k]
        patch = {k: state[k] for k in
                 ("lang_names","lang_format","yt_url","yt_enabled",
                  "combine_screens","osc_port","show_mode")}
        await mgr.broadcast({"type":"STATE_PATCH","data":patch})
        await mgr.broadcast({"type":"COMBINE","on":state["combine_screens"]}, "display1")
    elif t == "RECORD_START":
        state["recording"] = True; state["record_start"] = time.time()
        srt_log.clear()
        await mgr.broadcast({"type":"RECORDING","on":True})
    elif t == "RECORD_STOP":
        state["recording"] = False; _srt_close()
        await mgr.broadcast({"type":"RECORDING","on":False})
    elif t == "LTC_TICK":
        state["ltc_timecode"] = msg.get("seconds", 0.0)
        state["ltc_enabled"]  = msg.get("enabled", False)
    elif t == "FORMAT":
        li = str(msg["lang"]); state["lang_format"][li] = msg["fmt"]
        for d in ("display0","display1","display2","operator"):
            await mgr.broadcast({"type":"FORMAT","lang":li,"fmt":msg["fmt"]}, d)

# ── Core actions ──────────────────────────────────────────────────────────────
async def do_send_cue(idx: int):
    state["current_index"] = idx; state["blackout"] = False
    cue = _get_cue(idx)
    if not cue: return
    texts = list((cue.get("texts") or ["","",""])[:3])
    while len(texts) < 3: texts.append("")
    _srt_stamp(idx, texts)
    combine = state.get("combine_screens", False)
    await mgr.broadcast({"type":"TEXT","texts":texts,"blackout":False,"combine":False}, "display0")
    await mgr.broadcast({"type":"TEXT","texts":texts,"blackout":False,"combine":combine}, "display1")
    await mgr.broadcast({"type":"TEXT","texts":texts,"blackout":False,"combine":False}, "display2")
    await mgr.broadcast({"type":"ACTIVE","index":idx})
    if state["yt_enabled"] and state["yt_url"] and texts[0].strip():
        asyncio.create_task(_post_yt(texts[0], state["yt_url"]))

async def do_blackout(on: bool):
    state["blackout"] = on
    if on:
        _srt_close()
        empty = {"type":"TEXT","texts":["","",""],"blackout":True,"combine":False}
        for d in ("display0","display1","display2"): await mgr.broadcast(empty, d)
    else:
        # Restore current cue texts on all displays
        cue = _get_cue(state["current_index"])
        texts = list((cue.get("texts") or ["","",""])[:3]) if cue else ["","",""]
        while len(texts) < 3: texts.append("")
        combine = state.get("combine_screens", False)
        await mgr.broadcast({"type":"TEXT","texts":texts,"blackout":False,"combine":False}, "display0")
        await mgr.broadcast({"type":"TEXT","texts":texts,"blackout":False,"combine":combine}, "display1")
        await mgr.broadcast({"type":"TEXT","texts":texts,"blackout":False,"combine":False}, "display2")
    await mgr.broadcast({"type":"BLACKOUT","on":on})

async def do_next():
    flat = _flatten(state["cues"]); nxt = state["current_index"] + 1
    if nxt < len(flat): await do_send_cue(nxt)

async def do_prev():
    prev = state["current_index"] - 1
    if prev >= 0: await do_send_cue(prev)

async def do_start():
    if _flatten(state["cues"]): await do_send_cue(0)

# ── SRT ───────────────────────────────────────────────────────────────────────
def _srt_stamp(idx, texts):
    global srt_current
    if not state["recording"]: return
    _srt_close()
    srt_current = {"index":idx, "t_in":_now(), "texts":texts}

def _srt_close():
    global srt_current
    if srt_current and state["recording"]:
        t_out = _now()
        if t_out - srt_current["t_in"] < 0.5: t_out = srt_current["t_in"] + 0.5
        srt_current["t_out"] = t_out
        srt_log.append(dict(srt_current))
    srt_current = None

def _tc(s):
    s = max(0.0, s); h=int(s//3600); m=int((s%3600)//60); sec=int(s%60)
    ms=min(999,int(round((s%1)*1000)))
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"

def _build_srt(li):
    out, seq = [], 1
    for e in srt_log:
        txt = e["texts"][li] if li < len(e["texts"]) else ""
        if not txt.strip(): continue
        out.append(f"{seq}\n{_tc(e['t_in'])} --> {_tc(e['t_out'])}\n{txt}\n")
        seq += 1
    return "\n".join(out)

# ── YouTube ───────────────────────────────────────────────────────────────────
# POST: <url>&seq=N  Content-Type: text/plain
# Body: <ISO-UTC-timestamp + drift correction + delay>\n<text>\n
# Clock sync: YouTube returns a server timestamp in the response body.
# We compute drift = server_time - our_send_time, then apply it to future POSTs.
# Configurable delay (seconds) adjusts for encoding/network latency.

_yt_drift: float = 0.0   # seconds, updated from YouTube server responses

def _yt_ts(offset_secs: float = 0.0) -> str:
    """UTC timestamp adjusted for measured drift and configured delay."""
    now = datetime.now(timezone.utc).timestamp() + _yt_drift + offset_secs
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

async def _post_yt(text: str, url: str):
    global _yt_seq, _yt_drift
    _yt_seq += 1
    sep = "&" if "?" in url else "?"
    delay = float(state.get("yt_delay", 0.0))
    body = f"{_yt_ts(delay)}\n{text.strip()}\n"
    t_sent = datetime.now(timezone.utc).timestamp()
    try:
        async with httpx.AsyncClient(timeout=4, http2=False) as c:
            r = await c.post(f"{url}{sep}seq={_yt_seq}",
                             content=body.encode("utf-8"),
                             headers={"Content-Type": "text/plain"})
        # Parse server timestamp from response body for clock sync
        try:
            resp_ts_str = r.text.strip().rstrip("Z").replace("T"," ")
            server_time = datetime.fromisoformat(resp_ts_str).replace(
                tzinfo=timezone.utc).timestamp()
            t_recv = datetime.now(timezone.utc).timestamp()
            rtt = t_recv - t_sent
            # server_time should equal our send time; drift = server - sent - rtt/2
            new_drift = server_time - t_sent - rtt / 2
            # Smooth update (exponential moving average, α=0.2)
            _yt_drift = _yt_drift * 0.8 + new_drift * 0.2
        except Exception:
            pass
        await mgr.broadcast({"type":"YT_STATUS","ok":r.status_code<300,
                              "code":r.status_code,"seq":_yt_seq,
                              "drift":round(_yt_drift*1000)})
    except Exception as e:
        await mgr.broadcast({"type":"YT_STATUS","ok":False,"error":str(e)[:80]})

# ── OSC — raw asyncio UDP ─────────────────────────────────────────────────────
def _pad4(n): return (n + 3) & ~3

def _parse_osc(data: bytes):
    def read_str(buf, pos):
        end = buf.index(b'\x00', pos)
        s = buf[pos:end].decode("utf-8", errors="replace")
        return s, _pad4(end + 1)
    try:
        if data[:8] == b'#bundle\x00':
            pos = 16
            results = []
            while pos + 4 <= len(data):
                sz = struct.unpack_from(">I", data, pos)[0]; pos += 4
                try: results.append(_parse_osc(data[pos:pos+sz]))
                except: pass
                pos += sz
            return results[0] if results else ("", [])
        addr, pos = read_str(data, 0)
        if pos >= len(data): return addr, []
        type_str, pos = read_str(data, pos)
        args = []
        for tc in type_str.lstrip(","):
            if tc == "i": args.append(struct.unpack_from(">i",data,pos)[0]); pos+=4
            elif tc == "f": args.append(struct.unpack_from(">f",data,pos)[0]); pos+=4
            elif tc == "s": s,pos=read_str(data,pos); args.append(s)
            elif tc == "T": args.append(True)
            elif tc == "F": args.append(False)
        return addr, args
    except: return "", []

class OSCProtocol(asyncio.DatagramProtocol):
    def __init__(self, loop): self.loop = loop
    def connection_made(self, transport): pass
    def datagram_received(self, data: bytes, addr):
        try:
            address, args = _parse_osc(data)
            if address:
                asyncio.run_coroutine_threadsafe(self._dispatch(address,args), self.loop)
        except: pass

    async def _dispatch(self, address: str, args: list):
        await mgr.broadcast({"type":"OSC_RX","address":address}, "operator")
        a = address.lower().rstrip("/")
        if   a == "/sublive/next":     await do_next()
        elif a == "/sublive/prev":     await do_prev()
        elif a == "/sublive/start":    await do_start()
        elif a == "/sublive/blackout": await do_blackout(True)
        elif a == "/sublive/clear":    await do_blackout(False)
        elif a == "/sublive/go":
            if args:
                try: await do_send_cue(int(float(args[0])))
                except: pass
        elif "/sublive/cue/" in a:
            try: await do_send_cue(int(a.split("/sublive/cue/")[-1]))
            except: pass

_osc_transport_inst = None

async def _start_osc(port: int):
    global _osc_transport_inst
    loop = asyncio.get_event_loop()
    if _osc_transport_inst:
        try: _osc_transport_inst.close()
        except: pass
        _osc_transport_inst = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: OSCProtocol(loop),
            local_addr=("0.0.0.0", port)
        )
        _osc_transport_inst = transport
        await mgr.broadcast({"type":"OSC_STATUS","ok":True,"port":port})
    except Exception as e:
        await mgr.broadcast({"type":"OSC_STATUS","ok":False,"error":str(e)})

@app.on_event("startup")
async def startup():
    asyncio.create_task(_start_osc(state["osc_port"]))

# ── REST ──────────────────────────────────────────────────────────────────────
@app.get("/api/export/srt/{li}")
def export_srt(li: int):
    if not srt_log: raise HTTPException(404, "No recording data")
    srt = _build_srt(li)
    if not srt.strip(): raise HTTPException(404, f"No text for language {li+1}")
    name = state["lang_names"][li].replace(" ","_") if li < 3 else f"L{li+1}"
    return Response(srt, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition":f'attachment; filename="{name}.srt"'})

@app.get("/api/projects")
def list_projects(): return [f.stem for f in sorted(PROJECTS_DIR.glob("*.sublive"))]

@app.get("/api/projects/{name}")
def load_project(name: str):
    p = PROJECTS_DIR / f"{name}.sublive"
    if not p.exists(): raise HTTPException(404)
    data = json.loads(p.read_text())
    for k in ("cues","lang_names","lang_format","yt_url","yt_enabled",
              "combine_screens","osc_port"):
        if k in data: state[k] = data[k]
    return data

@app.post("/api/projects/{name}")
async def save_project(name: str, body: dict):
    (PROJECTS_DIR/f"{name}.sublive").write_text(
        json.dumps(body, indent=2, ensure_ascii=False))
    return {"ok": True}

@app.delete("/api/projects/{name}")
async def del_project(name: str):
    p = PROJECTS_DIR/f"{name}.sublive"
    if p.exists(): p.unlink()
    return {"ok": True}

@app.post("/api/import/srt")
async def import_srt(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8", errors="replace")
    # Robust SRT parser: split on blank lines, skip sequence number and timecode
    blocks = [b.strip() for b in content.strip().split("\n\n") if b.strip()]
    texts = []
    for block in blocks:
        lines = block.split("\n")
        # Skip leading line if it's just a number (sequence)
        idx = 0
        if lines and lines[0].strip().isdigit(): idx = 1
        # Skip timecode line (contains -->)
        if idx < len(lines) and "-->" in lines[idx]: idx += 1
        text = " ".join(l.strip() for l in lines[idx:] if l.strip())
        if text: texts.append(text)
    return {"texts": texts}

@app.post("/api/youtube/test")
async def test_yt(body: dict):
    url = body.get("url","")
    if not url: return {"ok":False,"error":"No URL"}
    await _post_yt("SubLive connection test", url)
    return {"ok": True}

@app.post("/api/osc/restart")
async def restart_osc(body: dict):
    port = int(body.get("port", 53002))
    state["osc_port"] = port
    asyncio.create_task(_start_osc(port))
    return {"ok":True,"port":port}

# ── Pages ─────────────────────────────────────────────────────────────────────
def _read(n): return (Path("/app/templates")/n).read_text()

@app.get("/", response_class=HTMLResponse)
def operator(): return _read("operator.html")

@app.get("/display/{li}", response_class=HTMLResponse)
def display(li: int): return _read("display.html").replace("__LANG__",str(li))

@app.get("/help", response_class=HTMLResponse)
def help_pg(): return _read("help.html")

# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_cue(fi: int):
    flat = _flatten(state["cues"])
    return flat[fi] if 0 <= fi < len(flat) else None

def _flatten(items: list) -> list:
    r = []
    for it in items:
        if it.get("type") == "group":
            if not it.get("collapsed"): r += _flatten(it.get("children",[]))
        elif not it.get("disabled"): r.append(it)
    return r
