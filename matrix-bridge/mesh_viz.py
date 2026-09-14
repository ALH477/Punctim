#!/usr/bin/env python3
"""mesh_viz.py — a host that visualizes the agents on the DCF mesh, live.

Runs a UDP mesh endpoint + a web dashboard (no external deps — stdlib + a
self-contained HTML/JS page). Every DeModFrame it sees is decoded with the
certified codec (wirelab_core.decode), reassembled into messages (dcf_text), and
streamed to the browser over SSE, where a force-directed graph shows each agent,
the channels in play, and animated pulses on every message.

Two roles:
  --mode hub      (default) agents point their DCF_PEERS at this host; it RELAYS
                  each datagram to the other peers it has learned AND visualizes.
                  Gives full visibility and turns 2-agent direct peering into an
                  N-agent hub.
  --mode monitor  observe-only: no relay. Agents must send copies here (add it as
                  an extra peer), or point a tap at this port.

Run:
  python3 matrix-bridge/mesh_viz.py                       # hub on :7800, web on :8088
  python3 matrix-bridge/mesh_viz.py --mode monitor --port 7802
  python3 matrix-bridge/mesh_viz.py --names 0x00a1=Hermes,0x00b2=Punctim \
      --channels duet,agent --peers 127.0.0.1:7801,127.0.0.1:7802
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "python", "MCP"))
import dcf_text
import superpack
from wirelab_core import decode

FRAME_TYPES = {0: "DATA", 1: "ACK", 2: "HEARTBEAT", 3: "CTRL"}


# ── shared live state ─────────────────────────────────────────────────────────
class MeshState:
    def __init__(self, names, channel_names):
        self.lock = threading.Lock()
        self.names = names                  # src id -> label
        self.channel_names = channel_names  # dst id -> label
        self.nodes = {}                     # src -> dict
        self.channels = {}                  # dst -> count
        self.members = {}                   # dst -> set(src)
        self.edges = {}                     # "a|b" -> weight
        self.frames = 0
        self.superpacks = 0
        self.messages = 0
        self.recent = []                    # ring buffer of events (for /state)
        self.subs = []                      # list of Queue for SSE clients
        self.started = time.time()

    def label(self, src):
        return self.names.get(src, f"{src:#06x}")

    def chan_label(self, dst):
        return self.channel_names.get(dst, f"{dst:#06x}")

    def subscribe(self):
        q = Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def _publish(self, ev):
        # caller holds self.lock
        self.recent.append(ev)
        if len(self.recent) > 200:
            self.recent = self.recent[-200:]
        for q in list(self.subs):
            try:
                q.put_nowait(ev)
            except Exception:
                pass

    def on_frame(self, src, dst, ftype, nbytes, was_superpack):
        now = time.time()
        with self.lock:
            self.frames += 1
            if was_superpack:
                self.superpacks += 1
            n = self.nodes.get(src)
            new_node = n is None
            if new_node:
                n = {"id": src, "label": self.label(src), "first": now,
                     "frames": 0, "bytes": 0, "channels": []}
                self.nodes[src] = n
            n["last"] = now
            n["frames"] += 1
            n["bytes"] += nbytes
            self.channels[dst] = self.channels.get(dst, 0) + 1
            mem = self.members.setdefault(dst, set())
            if src not in mem:
                mem.add(src)
            if dst not in n["channels"] and dst != dcf_text.BROADCAST:
                n["channels"].append(dst)
            if new_node:
                self._publish({"t": "node", "id": f"{src:#06x}",
                               "label": self.label(src)})
            # pulse from src to each other agent sharing this channel
            peers = [m for m in mem if m != src]
            for m in peers:
                key = "|".join(f"{x:#06x}" for x in sorted((src, m)))
                self.edges[key] = self.edges.get(key, 0) + 1
            self._publish({"t": "frame", "src": f"{src:#06x}",
                           "dst": f"{dst:#06x}", "chan": self.chan_label(dst),
                           "ftype": FRAME_TYPES.get(ftype, str(ftype)),
                           "peers": [f"{m:#06x}" for m in peers]})

    def on_message(self, src, dst, text):
        with self.lock:
            self.messages += 1
            preview = text if len(text) <= 160 else text[:157] + "…"
            self._publish({"t": "message", "src": f"{src:#06x}",
                           "label": self.label(src), "dst": f"{dst:#06x}",
                           "chan": self.chan_label(dst), "text": preview,
                           "ts": time.strftime("%H:%M:%S")})

    def snapshot(self):
        with self.lock:
            return {
                "mode": MODE,
                "uptime": int(time.time() - self.started),
                "stats": {"nodes": len(self.nodes), "frames": self.frames,
                          "messages": self.messages,
                          "superpack_pct": round(100 * self.superpacks /
                                                 self.frames, 1) if self.frames else 0},
                "nodes": [{"id": f"{s:#06x}", "label": n["label"],
                           "frames": n["frames"], "bytes": n["bytes"],
                           "channels": [self.chan_label(c) for c in n["channels"]],
                           "idle": round(time.time() - n["last"], 1)}
                          for s, n in self.nodes.items()],
                "edges": [{"pair": k.split("|"), "w": w}
                          for k, w in self.edges.items()],
                "channels": [{"id": f"{d:#06x}", "label": self.chan_label(d),
                              "count": c} for d, c in self.channels.items()],
                "recent": self.recent[-60:],
            }


# ── mesh thread: receive, decode, (relay), feed state ─────────────────────────
def mesh_loop(state, sock, mode, seed_peers):
    peers = dict(seed_peers)             # addr tuple -> last_seen
    rx = dcf_text.TextReassembler(accept_dst=None)   # accept every channel
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            break
        peers[addr] = time.time()
        # hub: relay the raw datagram (frames preserved) to every other peer
        if mode == "hub":
            for paddr in list(peers):
                if paddr != addr:
                    try:
                        sock.sendto(data, paddr)
                    except OSError:
                        pass
        was_sp = superpack.is_superpack(data)
        if was_sp:
            try:
                frames = superpack.unpack(data)
            except ValueError:
                continue
        elif len(data) == 17:
            frames = (data,)
        else:
            continue
        for f in frames:
            try:
                d = decode(f)
            except ValueError:
                continue
            state.on_frame(d["src"], d["dst"], d["frame_type"], len(f), was_sp)
            for ev in rx.push(f):
                _, _pid, _ts, src, dst, text, _flags = ev
                state.on_message(src, dst, text)


# ── HTTP / SSE ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE)
        elif self.path == "/state":
            self._send(200, json.dumps(STATE.snapshot()),
                       "application/json; charset=utf-8")
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = STATE.subscribe()
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    except Empty:
                        self.wfile.write(b": ping\n\n")     # heartbeat
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                STATE.unsubscribe(q)
        else:
            self._send(404, "not found", "text/plain")


# ── the page (self-contained; pulls data from /state + /events) ───────────────
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCF Mesh — live agents</title>
<style>
  :root{--ink:#0b1020;--cyan:#34d6e8;--amber:#f4a43a;--panel:#0e1531;--line:rgba(120,160,220,.18);--dim:#aebbd6}
  *{box-sizing:border-box}
  body{margin:0;background:linear-gradient(180deg,#0b1020,#0e1531);color:#e8eefc;
    font-family:"Space Grotesk",system-ui,-apple-system,sans-serif;height:100vh;overflow:hidden}
  header{display:flex;align-items:center;gap:1rem;padding:.7rem 1.1rem;border-bottom:1px solid var(--line)}
  header .dot{width:10px;height:10px;border-radius:50%;background:var(--cyan);box-shadow:0 0 12px var(--cyan)}
  header h1{font-size:1rem;margin:0;letter-spacing:-.01em}
  header .eyebrow{font-family:ui-monospace,"JetBrains Mono",monospace;text-transform:uppercase;
    letter-spacing:.26em;font-size:.62rem;color:var(--cyan)}
  #stats{margin-left:auto;display:flex;gap:1.3rem;font-family:ui-monospace,monospace;font-size:.78rem;color:var(--dim)}
  #stats b{color:#fff;font-weight:600}
  main{display:grid;grid-template-columns:1fr 360px;height:calc(100vh - 50px)}
  canvas{width:100%;height:100%;display:block}
  #side{border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
  #side h2{font-size:.7rem;text-transform:uppercase;letter-spacing:.2em;color:var(--cyan);
    margin:0;padding:.7rem 1rem .4rem;font-family:ui-monospace,monospace}
  #feed{overflow-y:auto;padding:0 1rem 1rem;flex:1;min-height:0}
  .ev{padding:.4rem 0;border-bottom:1px solid var(--line);font-size:.82rem;line-height:1.35}
  .ev .meta{font-family:ui-monospace,monospace;font-size:.68rem;color:var(--dim)}
  .ev .who{color:var(--cyan);font-weight:600}
  .ev .chan{color:var(--amber)}
  .empty{color:var(--dim);font-size:.8rem;padding:.5rem 0}
</style></head><body>
<header>
  <span class="dot"></span>
  <div><div class="eyebrow">DeMoD Communication Framework</div><h1>Mesh — live agents</h1></div>
  <div id="stats"></div>
</header>
<main>
  <canvas id="cv"></canvas>
  <div id="side"><h2>Live traffic</h2><div id="feed"><div class="empty">waiting for mesh traffic…</div></div></div>
</main>
<script>
const cv=document.getElementById('cv'),ctx=cv.getContext('2d'),feed=document.getElementById('feed'),statsEl=document.getElementById('stats');
let W,H; function size(){const r=cv.getBoundingClientRect();W=cv.width=r.width*devicePixelRatio;H=cv.height=r.height*devicePixelRatio;}
addEventListener('resize',size);size();
const nodes=new Map(),edges=new Map(),pulses=[];let stats={nodes:0,frames:0,messages:0,superpack_pct:0,mode:'',uptime:0};
const CYAN='#34d6e8',AMBER='#f4a43a';
function ensure(id,label){let n=nodes.get(id);if(!n){n={id,label:label||id,x:W/2+(Math.random()-.5)*200,y:H/2+(Math.random()-.5)*200,vx:0,vy:0,last:performance.now(),frames:0};nodes.set(id,n);}if(label)n.label=label;return n;}
function ekey(a,b){return [a,b].sort().join('|');}
function addEdge(a,b){const k=ekey(a,b);edges.set(k,(edges.get(k)||0)+1);}
function pulse(a,b){const na=nodes.get(a),nb=nodes.get(b);if(na&&nb)pulses.push({a:na,b:nb,t:performance.now()});}
function renderStats(){statsEl.innerHTML=`<span>mode <b>${stats.mode}</b></span><span>agents <b>${stats.nodes}</b></span><span>frames <b>${stats.frames}</b></span><span>msgs <b>${stats.messages}</b></span><span>superpack <b>${stats.superpack_pct}%</b></span>`;}
function addFeed(ev){const e=document.querySelector('.empty');if(e)e.remove();const d=document.createElement('div');d.className='ev';
  d.innerHTML=`<div class="meta">${ev.ts||''} · <span class="chan">${ev.chan}</span></div><span class="who">${ev.label||ev.src}</span> ${escapeHtml(ev.text)}`;
  feed.prepend(d);while(feed.children.length>120)feed.lastChild.remove();}
function escapeHtml(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function physics(){const cx=W/2,cy=H/2,arr=[...nodes.values()];
  for(const a of arr){a.vx+=(cx-a.x)*0.0008;a.vy+=(cy-a.y)*0.0008;
    for(const b of arr){if(a===b)continue;let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+0.01,d=Math.sqrt(d2);const f=9000/d2;a.vx+=dx/d*f;a.vy+=dy/d*f;}}
  for(const k of edges.keys()){const[i,j]=k.split('|');const a=nodes.get(i),b=nodes.get(j);if(!a||!b)continue;let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-180*devicePixelRatio)*0.006;a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;}
  for(const a of arr){a.vx*=0.86;a.vy*=0.86;a.x+=a.vx;a.y+=a.vy;a.x=Math.max(40,Math.min(W-40,a.x));a.y=Math.max(40,Math.min(H-40,a.y));}}
function draw(){ctx.clearRect(0,0,W,H);
  ctx.lineWidth=1.2*devicePixelRatio;
  for(const k of edges.keys()){const[i,j]=k.split('|');const a=nodes.get(i),b=nodes.get(j);if(!a||!b)continue;ctx.strokeStyle='rgba(120,160,220,.22)';ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
  const now=performance.now();
  for(let p=pulses.length-1;p>=0;p--){const pu=pulses[p],age=(now-pu.t)/700;if(age>=1){pulses.splice(p,1);continue;}const x=pu.a.x+(pu.b.x-pu.a.x)*age,y=pu.a.y+(pu.b.y-pu.a.y)*age;ctx.fillStyle=CYAN;ctx.globalAlpha=1-age;ctx.beginPath();ctx.arc(x,y,4.5*devicePixelRatio,0,7);ctx.fill();ctx.globalAlpha=1;}
  for(const n of nodes.values()){const idle=(now-n.last)/1000,r=(13+Math.min(14,n.frames*0.15))*devicePixelRatio,alpha=Math.max(.4,1-idle/60);
    ctx.globalAlpha=alpha;const g=ctx.createRadialGradient(n.x,n.y,2,n.x,n.y,r);g.addColorStop(0,'#1b2b50');g.addColorStop(1,'#0e1531');ctx.fillStyle=g;ctx.beginPath();ctx.arc(n.x,n.y,r,0,7);ctx.fill();
    ctx.strokeStyle=CYAN;ctx.lineWidth=2*devicePixelRatio;ctx.stroke();ctx.globalAlpha=1;
    ctx.fillStyle='#e8eefc';ctx.font=`${13*devicePixelRatio}px "Space Grotesk",system-ui`;ctx.textAlign='center';ctx.fillText(n.label,n.x,n.y-r-6*devicePixelRatio);}
  physics();requestAnimationFrame(draw);}
requestAnimationFrame(draw);
function apply(ev){if(ev.t==='node'){ensure(ev.id,ev.label);}else if(ev.t==='frame'){const s=ensure(ev.src);s.last=performance.now();s.frames++;(ev.peers||[]).forEach(p=>{ensure(p);addEdge(ev.src,p);pulse(ev.src,p);});}else if(ev.t==='message'){addFeed(ev);}}
fetch('/state').then(r=>r.json()).then(s=>{stats=Object.assign(stats,s.stats,{mode:s.mode});renderStats();
  (s.nodes||[]).forEach(n=>ensure(n.id,n.label));(s.edges||[]).forEach(e=>edges.set(e.pair.sort().join('|'),e.w));
  (s.recent||[]).filter(e=>e.t==='message').slice(-30).reverse().forEach(addFeed);});
const es=new EventSource('/events');es.onmessage=e=>{const ev=JSON.parse(e.data);apply(ev);
  if(ev.t==='frame'){stats.frames++;}if(ev.t==='message'){stats.messages++;}stats.nodes=nodes.size;renderStats();};
setInterval(()=>fetch('/state').then(r=>r.json()).then(s=>{stats=Object.assign(stats,s.stats,{mode:s.mode});renderStats();}),5000);
</script></body></html>"""


# ── wiring ────────────────────────────────────────────────────────────────────
def parse_names(spec):
    out = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            out[int(k, 0)] = v.strip()
    return out


def parse_peers(spec):
    out = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if ":" in item:
            host, _, port = item.rpartition(":")
            if host and port.isdigit():
                out[(host, int(port))] = 0.0
    return out


STATE = None
MODE = "hub"


def main(argv=None):
    global STATE, MODE
    p = argparse.ArgumentParser(description="Visualize the agents on the DCF mesh.")
    p.add_argument("--mode", choices=["hub", "monitor"], default="hub")
    p.add_argument("--port", type=int, default=7800, help="UDP mesh port to bind")
    p.add_argument("--http", type=int, default=8088, help="web dashboard port")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--names", default=os.environ.get("DCF_NAMES",
                   "0x00a1=Hermes,0x00b2=Punctim"))
    p.add_argument("--channels", default=os.environ.get("DCF_CHANNELS", "duet"),
                   help="comma-separated channel names to label (crc16 mapped)")
    p.add_argument("--peers", default=os.environ.get("DCF_PEERS", ""),
                   help="hub: seed peer host:port list to relay to")
    args = p.parse_args(argv)
    MODE = args.mode

    chan_names = {dcf_text.channel_id(c.strip()): c.strip()
                  for c in args.channels.split(",") if c.strip()}
    STATE = MeshState(parse_names(args.names), chan_names)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    threading.Thread(target=mesh_loop,
                     args=(STATE, sock, args.mode, parse_peers(args.peers)),
                     daemon=True).start()

    httpd = ThreadingHTTPServer((args.bind, args.http), Handler)
    print(f"mesh_viz: {args.mode} on udp/{args.port}, dashboard -> "
          f"http://127.0.0.1:{args.http}/  (channels: {', '.join(chan_names.values())})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nmesh_viz: stopped")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
