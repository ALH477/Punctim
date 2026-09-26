# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 DeMoD LLC.
"""DCF transport layer — carry the universal unit (the 17-byte DeModFrame) over any
medium behind one interface, so a node can speak UDP, audio (PipeWire/JACK/WAV), SDR
(.cf32/SoapySDR) or a file spool, and a Bridge can relay frames between them
(see ``dcf.bridge`` and Documentation/DCF_FIELD_USE.md).

The pieces that differ wildly between media are **bandwidth and baud** — UDP is ~Mbps,
SDR ~kbps, a handheld-acoustic link ~300 baud (~37 B/s). So every transport owns a
**bounded outbound queue with a sender thread that drains at the link's own pace**: the
buffer decouples a fast source from a slow sink, ``send()`` never blocks the caller, and
on overflow a drop policy sheds bulk while keeping priority (control/mesh) frames. That
buffer is the heart of making heterogeneous shapes work.

Every medium's byte/symbol representation is the certified DCF-Medium codec
(python/MCP/mediumlab_core.py, Documentation/DCF_MEDIUM_SPEC.md); ``dcf.medium`` names
the media with URIs (``udp:dialect=bare,peer=...``, ``file:path=...``, ``hex:``, ...) and
``punctim io`` drives them through ``send_now`` (synchronous, never drops). numpy is only
needed by the AFSK/SDR modems and is imported lazily (``_load_modems``).
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import deque

# MCP is a sibling from source (../MCP) and nested under dcf once packaged (./MCP).
for _mcp in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MCP"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "MCP")):
    if os.path.isdir(_mcp):
        sys.path.insert(0, _mcp)
import wirelab_core as wire  # noqa: E402
import superpack  # noqa: E402
import mediumlab_core as medium_codec  # noqa: E402  (certified DCF-Medium codecs)

from .proto import MSG_FRAME, now_micros  # noqa: F401  (MSG_FRAME re-exported)

FRAME_LEN = 17


class MediumUnsupported(RuntimeError):
    """This medium, or one of its options, is not available in this build/environment
    (missing numpy, HydraModem tools, janus-c, a tool without a flag, ...). The `punctim`
    CLI maps it to exit code 3 (Documentation/DCF_MEDIUM_SPEC.md)."""


def frame_key(frame):
    """Dedup/identity key for loop-free forwarding: (src, dst, seq, crc) of a frame."""
    try:
        d = wire.decode(bytes(frame))
        return (d["src"], d["dst"], d["seq"], d["crc"])
    except Exception:
        return (bytes(frame),)


# ── the per-transport buffer (rate decoupling) ────────────────────────────────
class OutboundQueue:
    """Bounded two-tier FIFO. Priority items (control/mesh) are drained first and are the
    last to be dropped; on overflow bulk is shed oldest-first. Pure + unit-testable."""

    def __init__(self, maxlen=256):
        self.maxlen = maxlen
        self._hi = deque()
        self._lo = deque()
        self._lock = threading.Lock()
        self.dropped = 0

    def put(self, item, priority=False):
        with self._lock:
            (self._hi if priority else self._lo).append(item)
            while len(self._hi) + len(self._lo) > self.maxlen:
                # shed bulk first; only drop priority if nothing else remains
                (self._lo if self._lo else self._hi).popleft()
                self.dropped += 1

    def get(self):
        """Pop the next item (priority first), or None if empty. Non-blocking."""
        with self._lock:
            if self._hi:
                return self._hi.popleft()
            if self._lo:
                return self._lo.popleft()
            return None

    def __len__(self):
        with self._lock:
            return len(self._hi) + len(self._lo)


# ── base transport ────────────────────────────────────────────────────────────
class Transport(ABC):
    """One medium. Subclasses implement `_transmit` (send one frame) and, if they receive,
    push frames by calling `self._deliver(frame, meta)`."""

    def __init__(self, name, rate_bps=10_000_000, queue_max=256, pace=False):
        self.name = name
        self.rate_bps = rate_bps
        self.pace = pace                    # simulate link time (file/loopback transports)
        self._q = OutboundQueue(queue_max)
        self._on_frame = None
        self._running = False
        self._sender = None
        self.sent = 0
        self.recv = 0

    # public API ----------------------------------------------------------------
    def start(self, on_frame):
        self._on_frame = on_frame
        self._running = True
        self._sender = threading.Thread(target=self._sender_loop, name=f"tx-{self.name}",
                                        daemon=True)
        self._sender.start()
        self._start_recv()

    def send(self, frame, dest=None, priority=False):
        """Enqueue a frame for transmission. Never blocks; sheds by policy if the link is
        saturated. Returns False if the queue dropped something to make room."""
        before = self._q.dropped
        self._q.put((bytes(frame), dest), priority=priority)
        return self._q.dropped == before

    def send_now(self, frame, dest=None):
        """Transmit synchronously on the caller's thread, bypassing the outbound queue
        (never drops, never reorders). Used by `punctim io`, whose single-threaded ordered
        pipeline must not shed frames of a finite input."""
        self._transmit(bytes(frame), dest)
        self.sent += 1

    def flush(self):
        """Emit anything the medium is holding back (e.g. a lone frame waiting for a
        SuperPack partner). No-op for unbatched media."""

    def stop(self):
        self._running = False
        if self._sender:
            self._sender.join(timeout=2.0)
        try:
            self.flush()
        except Exception as e:  # pragma: no cover - link errors are per-medium
            sys.stderr.write(f"[transport {self.name}] flush error: {e}\n")
        self._stop_recv()

    @property
    def backlog(self):
        return len(self._q)

    @property
    def dropped(self):
        return self._q.dropped

    # internals -----------------------------------------------------------------
    def _deliver(self, frame, meta=None):
        self.recv += 1
        if self._on_frame:
            self._on_frame(bytes(frame), {"transport": self.name, **(meta or {})})

    def _sender_loop(self):
        while self._running:
            item = self._q.get()
            if item is None:
                self._idle()
                time.sleep(0.002)
                continue
            frame, dest = item
            t0 = time.monotonic()
            try:
                self._transmit(frame, dest)
                self.sent += 1
            except Exception as e:  # pragma: no cover - link errors are per-medium
                sys.stderr.write(f"[transport {self.name}] tx error: {e}\n")
            if self.pace and self.rate_bps > 0:
                want = len(frame) * 8 / self.rate_bps
                slack = want - (time.monotonic() - t0)
                if slack > 0:
                    time.sleep(slack)

    @abstractmethod
    def _transmit(self, frame, dest):
        ...

    def _idle(self):
        """Called by the sender loop (and `punctim io`) when nothing is queued — the hook
        for time-based flushes (the bare-UDP lone-frame timer)."""

    def _start_recv(self):
        ...

    def _stop_recv(self):
        ...


# ── loopback / in-process medium (for tests + bridging in one process) ────────
class LoopbackMedium:
    """A shared in-memory medium: every LoopbackTransport attached delivers transmitted
    frames to all *other* attached transports. Models a broadcast wire."""

    def __init__(self):
        self.ports = []
        self._lock = threading.Lock()

    def attach(self, t):
        with self._lock:
            if t not in self.ports:
                self.ports.append(t)

    def detach(self, t):
        with self._lock:
            if t in self.ports:
                self.ports.remove(t)

    def broadcast(self, sender, frame):
        with self._lock:
            ports = list(self.ports)
        for p in ports:
            if p is not sender:
                p._deliver(frame, {"via": "loopback"})


_LOOP_MEDIA = {}
_LOOP_LOCK = threading.Lock()


def loop_medium(medium_id="default"):
    """The process-wide named LoopbackMedium behind the `loop:id=<id>` URI (created on
    first use), so independently built transports with the same id share one wire."""
    with _LOOP_LOCK:
        m = _LOOP_MEDIA.get(medium_id)
        if m is None:
            m = _LOOP_MEDIA[medium_id] = LoopbackMedium()
        return m


class LoopbackTransport(Transport):
    def __init__(self, name, medium, **kw):
        super().__init__(name, **kw)
        self._medium = medium
        medium.attach(self)

    def _transmit(self, frame, dest):
        self._medium.broadcast(self, frame)

    def _start_recv(self):
        self._medium.attach(self)

    def _stop_recv(self):
        self._medium.detach(self)


# ── file spool (store-and-forward / offline bridging) ─────────────────────────
class FileTransport(Transport):
    """Append frames to a .dcf spool (tx) and/or read one (rx). The rx side decodes the
    DCF-Medium `stream` representation with byte-wise resync (StreamScanner), so a spool
    with garbage, a torn write or a mid-frame start still yields every intact frame.

    follow=True (the dcf-bridge default) tails the file for growth forever; follow=False
    reads it once to EOF (``eof`` is set then). append=True (default) appends to an
    existing spool; append=False truncates it on the first write."""

    def __init__(self, name, out_path=None, in_path=None, poll=0.2, follow=True,
                 append=True, **kw):
        super().__init__(name, **kw)
        self._out = out_path
        self._in = in_path
        self._poll = poll
        self._follow = follow
        self._append = append
        self._fh = None
        self._rx_running = False
        self._rx_thread = None
        self.scanner = medium_codec.StreamScanner()
        self.eof = threading.Event()

    @property
    def skipped_bytes(self):
        return self.scanner.skipped_bytes

    def _transmit(self, frame, dest):
        if not self._out:
            return
        if self._fh is None:
            self._fh = open(self._out, "ab" if self._append else "wb")
        self._fh.write(frame)
        self._fh.flush()                  # a tailing reader sees whole frames promptly

    def _start_recv(self):
        if not self._in:
            return
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._tail, name=f"rx-{self.name}",
                                          daemon=True)
        self._rx_thread.start()

    def _tail(self):
        pos = 0
        while self._rx_running:
            try:
                with open(self._in, "rb") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
                for fr in self.scanner.feed(data):
                    self._deliver(fr)
            except FileNotFoundError:
                pass
            if not self._follow:
                break
            time.sleep(self._poll)
        self.eof.set()

    def _stop_recv(self):
        self._rx_running = False
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


class StdioTransport(Transport):
    """The DCF-Medium `stream` representation on the process's stdin (rx, byte-wise
    resync) and stdout (tx, raw 17-byte frames) — the `stdio:` medium."""

    def __init__(self, name="stdio", stdin=None, stdout=None, read=True, write=True, **kw):
        super().__init__(name, **kw)
        self._in = (stdin if stdin is not None else sys.stdin.buffer) if read else None
        self._outf = (stdout if stdout is not None else sys.stdout.buffer) if write else None
        self.scanner = medium_codec.StreamScanner()
        self.eof = threading.Event()
        self._rx_thread = None

    @property
    def skipped_bytes(self):
        return self.scanner.skipped_bytes

    def _transmit(self, frame, dest):
        if self._outf is not None:
            self._outf.write(frame)
            self._outf.flush()

    def _start_recv(self):
        if self._in is None:
            return
        self._rx_thread = threading.Thread(target=self._reader, name=f"rx-{self.name}",
                                          daemon=True)
        self._rx_thread.start()

    def _reader(self):
        read = getattr(self._in, "read1", self._in.read)
        try:
            while self._running:
                chunk = read(65536)
                if not chunk:
                    break
                for fr in self.scanner.feed(chunk):
                    self._deliver(fr)
        except (OSError, ValueError):
            pass
        self.eof.set()


class HexTransport(Transport):
    """The DCF-Medium `hex` representation: one frame per line, 34 lowercase hex chars.
    rx parses lines (CRLF/uppercase/comments/blank accepted, bad lines counted); tx writes
    lines. With path=None rx is stdin and tx is stdout (the `hex:` medium); with a path,
    rx reads that file (tailing it when follow=True) and tx writes it (append or truncate).
    mode picks the directions: "r", "w" or "rw"."""

    def __init__(self, name="hex", path=None, mode="rw", follow=False, append=False,
                 poll=0.2, **kw):
        super().__init__(name, **kw)
        self._path = path
        self._read = "r" in mode
        self._write = "w" in mode
        self._follow = follow
        self._append = append
        self._poll = poll
        self._fh = None
        self._rx_thread = None
        self.bad_lines = 0
        self.eof = threading.Event()

    def _transmit(self, frame, dest):
        if not self._write:
            return
        line = (bytes(frame).hex() + "\n").encode("ascii")
        if self._path is None:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            return
        if self._fh is None:
            self._fh = open(self._path, "ab" if self._append else "wb")
        self._fh.write(line)
        self._fh.flush()

    def _start_recv(self):
        if not self._read:
            return
        self._rx_thread = threading.Thread(target=self._reader, name=f"rx-{self.name}",
                                          daemon=True)
        self._rx_thread.start()

    def _lines(self, raw):
        frames, bad = medium_codec.hex_decode(raw.decode("latin-1"))
        self.bad_lines += bad
        for fr in frames:
            self._deliver(fr)

    def _reader(self):
        try:
            if self._path is None:
                for raw in sys.stdin.buffer:
                    if not self._running:
                        break
                    self._lines(raw)
            else:
                pos, carry = 0, b""
                while self._running:
                    try:
                        with open(self._path, "rb") as f:
                            f.seek(pos)
                            data = f.read()
                            pos = f.tell()
                    except FileNotFoundError:
                        data = b""
                    buf = carry + data
                    cut = buf.rfind(b"\n") + 1
                    if cut:
                        self._lines(buf[:cut])
                    carry = buf[cut:]
                    if not self._follow:
                        if carry:
                            self._lines(carry)
                        break
                    time.sleep(self._poll)
        except (OSError, ValueError):
            pass
        self.eof.set()

    def _stop_recv(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# ── UDP: dialect "proto" (ProtoMessage MSG_FRAME) or "bare" (frame / SuperPack) ─
class UdpTransport(Transport):
    """Carry frames over UDP in one of the two DCF-Medium datagram dialects:

    * ``dialect="proto"`` (default): each frame is a 34-byte ProtoMessage(MSG_FRAME=12,
      seq, ts, len 17, frame) — the binary envelope of the Go/C/Rust/Python mesh nodes.
      ``seq`` starts at ``seq_start`` (1) and increments per datagram; ``ts="0"`` (default,
      deterministic) or ``"now"`` (microseconds since the epoch). rx accepts msg_type 12
      with payload_len 17; other types (adapter envelopes) are ignored.
    * ``dialect="bare"``: the Node/WASM/matrix-bridge dialect. With ``pair=True`` two
      consecutive frames travel as one 32-byte SuperPack; a lone frame is flushed raw
      (17 B) after ``flush_ms`` or at stop/flush. rx unpacks SuperPacks and accepts 17-B
      frames.

    Undecodable datagrams are counted in ``invalid_datagrams``."""

    def __init__(self, name="udp", bind=("0.0.0.0", 0), peers=(), rate_bps=10_000_000,
                 dialect="proto", pair=True, flush_ms=20, ts="0", seq_start=1, **kw):
        super().__init__(name, rate_bps=rate_bps, **kw)
        if dialect not in ("proto", "bare"):
            raise ValueError(f"udp dialect must be proto|bare, got {dialect!r}")
        if str(ts) not in ("0", "now"):
            raise ValueError(f"udp ts policy must be 0|now, got {ts!r}")
        self._bind = bind
        self._peers = [tuple(p) for p in peers]          # [(host, port), ...]
        self._dialect = dialect
        self._pair = bool(pair)
        self._flush_s = max(0.0, float(flush_ms)) / 1000.0
        self._ts_now = str(ts) == "now"
        self._seq = int(seq_start) & 0xFFFFFFFF          # next ProtoMessage sequence
        self._pending = None                             # bare: (frame, dest, t_monotonic)
        self._plock = threading.Lock()
        self._sock = None
        self._rx_running = False
        self._rx_thread = None
        self.invalid_datagrams = 0
        self.datagrams_sent = 0

    @property
    def dialect(self):
        return self._dialect

    @property
    def port(self):
        return self._sock.getsockname()[1] if self._sock else None

    def add_peer(self, host, port):
        self._peers.append((host, int(port)))

    def _ensure_sock(self):
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(self._bind)
            self._sock.settimeout(0.3)

    def _send_datagram(self, dg, dest):
        targets = [dest] if dest else self._peers
        for host, port in targets:
            self._sock.sendto(dg, (host, int(port)))
        self.datagrams_sent += 1

    def _transmit(self, frame, dest):
        self._ensure_sock()
        if self._dialect == "proto":
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            ts = now_micros() if self._ts_now else 0
            self._send_datagram(medium_codec.proto_frame_encode(frame, seq, ts), dest)
            return
        # bare dialect: pair consecutive valid frames into one SuperPack datagram
        if not self._pair or not medium_codec.gate(frame):
            self.flush()                     # keep order: a held frame goes first
            self._send_datagram(bytes(frame), dest)
            return
        with self._plock:
            held = self._pending
            if held is None:
                self._pending = (bytes(frame), dest, time.monotonic())
                return
            self._pending = None
        if held[1] != dest:                  # different destination: no pairing
            self._send_datagram(held[0], held[1])
            with self._plock:
                self._pending = (bytes(frame), dest, time.monotonic())
            return
        self._send_datagram(superpack.pack(held[0], frame), dest)

    def flush(self):
        with self._plock:
            held, self._pending = self._pending, None
        if held is not None:
            self._ensure_sock()
            self._send_datagram(held[0], held[1])

    def _idle(self):
        held = self._pending
        if held is not None and time.monotonic() - held[2] >= self._flush_s:
            self.flush()

    def _start_recv(self):
        self._ensure_sock()
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name=f"rx-{self.name}",
                                          daemon=True)
        self._rx_thread.start()

    def _rx_loop(self):
        while self._rx_running:
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if self._dialect == "proto":
                try:
                    msg_type, _, _, payload = medium_codec.proto_decode(data)
                except ValueError:
                    self.invalid_datagrams += 1
                    continue
                if msg_type != MSG_FRAME:
                    continue                 # an adapter envelope, not a frame on this medium
                if len(payload) != FRAME_LEN:
                    self.invalid_datagrams += 1
                    continue
                self._deliver(payload, {"addr": addr})
            else:
                frames = medium_codec.bare_decode(data)
                if not frames:
                    self.invalid_datagrams += 1
                    continue
                for fr in frames:
                    self._deliver(fr, {"addr": addr})

    def _stop_recv(self):
        self._rx_running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ── the modems (audio + SDR) need numpy: imported lazily, on first use ────────
_acoustic = None
_iqmod = None


def _load_modems():
    """Import python/modem acoustic.py + iq.py (numpy) on first use, tolerant of the
    source/packaged layouts. Raises MediumUnsupported without numpy, so importing this
    module (for the UDP/file/hex/stdio/loop/hydra media) never needs numpy."""
    global _acoustic, _iqmod
    if _acoustic is not None:
        return _acoustic, _iqmod
    try:
        import numpy  # noqa: F401
    except ImportError as e:
        raise MediumUnsupported("the afsk/audio and sdr media need numpy "
                                "(pip install numpy)") from e
    try:
        from dcf.modem import acoustic as a, iq as q
    except Exception:  # pragma: no cover - source layout
        mdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modem")
        if mdir not in sys.path:
            sys.path.insert(0, mdir)
        import acoustic as a
        import iq as q
    _acoustic, _iqmod = a, q
    return a, q


class _DirMedium(Transport):
    """Base for the file-medium modems: tx writes one encoded file per frame into
    `out_dir`; rx tails `in_dir` for new files and decodes them. Two nodes share a pair of
    dirs (A.out == B.in) to form a link — the same model as piping WAV/.cf32 through
    pw-play/pw-record or hackrf_transfer. `out_dir` may instead be a command sink later."""

    EXT = ""

    # A medium that carries TWO frames per file (the HydraModem duet) sets PAIRS = True
    # and implements _encode_pair_file(a, b_or_None, path); _decode_file may then
    # return a list of frames. Pairing is positional, like udp:dialect=bare's
    # SuperPacks: consecutive frames pair in order, and a lone frame goes alone after
    # flush_ms, at flush() or at stop().
    PAIRS = False

    def __init__(self, name, out_dir=None, in_dir=None, poll=0.1, rate_bps=4000,
                 flush_ms=20, **kw):
        super().__init__(name, rate_bps=rate_bps, **kw)
        self._out, self._in, self._poll = out_dir, in_dir, poll
        self._flush_s = max(0.0, float(flush_ms)) / 1000.0
        self._pending = None                  # PAIRS: (frame, t_monotonic)
        self._plock = threading.Lock()
        self._n = 0
        self._seen = set()
        self._rx_running = False
        self._rx_thread = None
        for d in (out_dir, in_dir):
            if d:
                os.makedirs(d, exist_ok=True)

    def _publish(self, encode):
        self._n += 1
        tmp = os.path.join(self._out, f".{self.name}-{self._n}{self.EXT}.tmp")
        final = os.path.join(self._out, f"{self.name}-{self._n:08d}{self.EXT}")
        encode(tmp)
        os.replace(tmp, final)            # atomic publish so the reader never sees a partial

    def _transmit(self, frame, dest):
        if not self._out:
            return
        if not self.PAIRS:
            self._publish(lambda path: self._encode_file(frame, path))
            return
        with self._plock:
            held, self._pending = self._pending, None
            if held is None:
                self._pending = (bytes(frame), time.monotonic())
                return
        self._publish(lambda path: self._encode_pair_file(held[0], bytes(frame), path))

    def flush(self):
        if not self.PAIRS:
            return
        with self._plock:
            held, self._pending = self._pending, None
        if held is not None and self._out:
            self._publish(lambda path: self._encode_pair_file(held[0], None, path))

    def _idle(self):
        held = self._pending
        if held is not None and time.monotonic() - held[1] >= self._flush_s:
            self.flush()

    def _start_recv(self):
        if not self._in:
            return
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._tail, name=f"rx-{self.name}", daemon=True)
        self._rx_thread.start()

    def _tail(self):
        while self._rx_running:
            try:
                files = sorted(f for f in os.listdir(self._in)
                               if f.endswith(self.EXT) and not f.startswith("."))
            except FileNotFoundError:
                files = []
            for f in files:
                if f in self._seen:
                    continue
                self._seen.add(f)
                path = os.path.join(self._in, f)
                try:
                    frame = self._decode_file(path)
                except Exception:
                    frame = None
                frames = frame if isinstance(frame, (list, tuple)) else [frame]
                for voice, fr in enumerate(frames):
                    if fr is not None and len(fr) == FRAME_LEN:
                        self._deliver(fr, {"file": f, "voice": voice} if len(frames) > 1
                                      else {"file": f})
            time.sleep(self._poll)

    def _stop_recv(self):
        self._rx_running = False

    def _encode_file(self, frame, path): ...
    def _decode_file(self, path): ...
    def _encode_pair_file(self, a, b, path): ...


class AudioTransport(_DirMedium):
    """Carry frames as voice-band AFSK WAVs (PipeWire/JACK/ALSA via pw-play/pw-record, or
    a shared dir for loopback). Reuses python/modem/acoustic.py."""

    EXT = ".wav"

    def __init__(self, name="audio", profile="handheld", fec=False, **kw):
        _load_modems()                    # numpy (MediumUnsupported without it)
        super().__init__(name, **kw)
        self._profile, self._fec = profile, fec

    def _encode_file(self, frame, path):
        audio, _, _ = _acoustic.frame_to_audio(frame, profile=self._profile, fec=self._fec)
        _acoustic.write_wav(path, audio)

    def _decode_file(self, path):
        audio, fs = _acoustic.read_wav(path)
        r = _acoustic.decode_audio(audio, profile=self._profile, fs=fs, fec=self._fec)
        return r[0] if r else None


class SdrTransport(_DirMedium):
    """Carry frames as complex-baseband IQ (.cf32 → SoapySDR / GNU Radio / a shared dir).
    Reuses python/modem/iq.py."""

    EXT = ".cf32"

    def __init__(self, name="sdr", mod="gfsk", sps=8, **kw):
        _load_modems()                    # numpy (MediumUnsupported without it)
        super().__init__(name, **kw)
        self._mod, self._sps = mod, sps

    def _encode_file(self, frame, path):
        sig = _iqmod.frame_to_iq(frame, mod=self._mod, sps=self._sps)
        _iqmod.write_cf32(path, sig)

    def _decode_file(self, path):
        sig = _iqmod.read_cf32(path)
        r = _iqmod.iq_to_frame(sig, mod=self._mod, sps=self._sps)
        return r[0] if r else None


# ── JANUS (NATO STANAG 4748) acoustic transport ───────────────────────────────
# The 17-byte DeModFrame rides as JANUS *cargo*, hex-encoded so arbitrary binary
# survives a CLI argument. We shell out to the GPL-3.0 janus-c reference
# (janus-tx/janus-rx) as a SEPARATE PROCESS — never linked — so this LGPL library
# is unaffected (mere aggregation, like the existing pw-play/ffmpeg calls). The
# reference encoder/decoder make the waveform STANAG-4748-compliant by
# construction. See Documentation/DCF_JANUS_SPEC.md and LICENSING.md.

# "Cargo (ASCII)" prints our hex string in quotes; "Payload" prints it bare.
_JANUS_CARGO_RE = re.compile(r'Cargo \(ASCII\)\s*:\s*"([0-9a-fA-F]*)"')
_JANUS_PAYLOAD_RE = re.compile(r'Payload\s*:\s*([0-9a-fA-F]+)')


def janus_available():
    """True iff the GPL janus-c binaries are reachable (PATH or $JANUS_TX/$JANUS_RX)."""
    tx = os.environ.get("JANUS_TX") or shutil.which("janus-tx")
    rx = os.environ.get("JANUS_RX") or shutil.which("janus-rx")
    return bool(tx and rx)


def _janus_share(tx_bin, *parts):
    """A path under the reference's installed share dir (../share/janus/... from bin)."""
    base = os.path.dirname(os.path.dirname(os.path.realpath(tx_bin)))
    return os.path.join(base, "share", "janus", *parts)


def _janus_default_pset(tx_bin):
    """The reference installs parameter_sets.csv next to its bin (../share/janus/etc)."""
    cand = _janus_share(tx_bin, "etc", "parameter_sets.csv")
    return cand if os.path.isfile(cand) else None


def _janus_env(tx_bin, plugins_dir=None):
    """Cargo (the codec plugins) is loaded by janus-c via dlopen on a bare name, so the
    plugins dir must be on LD_LIBRARY_PATH. Default to ../share/janus/plugins from the bin."""
    pdir = plugins_dir or os.environ.get("JANUS_PLUGINS") or _janus_share(tx_bin, "plugins")
    env = dict(os.environ)
    if os.path.isdir(pdir):
        env["LD_LIBRARY_PATH"] = pdir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def _janus_parse_cargo(stdout):
    m = _JANUS_CARGO_RE.search(stdout) or _JANUS_PAYLOAD_RE.search(stdout)
    if not m:
        return None
    h = m.group(1).strip()[: FRAME_LEN * 2]   # cargo is null-padded to a multiple of 8
    try:
        b = bytes.fromhex(h)
    except ValueError:
        return None
    return b if len(b) == FRAME_LEN else None


class JanusTransport(_DirMedium):
    """Carry frames over a STANAG-4748 (JANUS) acoustic link via the GPL janus-c
    reference. Default profile: parameter set 1 (Initial JANUS band, 11520 Hz
    center / 4160 Hz BW) rendered at 48 kHz. Raises at construction if janus-c is
    not installed (it is an optional GPL dependency — `nix build .#janus-c`)."""

    EXT = ".wav"

    def __init__(self, name="janus", pset_id=1, fs=48000, pset_file=None,
                 tx_bin=None, rx_bin=None, class_id=None, app_type=None,
                 rate_bps=80, **kw):
        super().__init__(name, rate_bps=rate_bps, **kw)
        self._tx = tx_bin or os.environ.get("JANUS_TX") or shutil.which("janus-tx")
        self._rx = rx_bin or os.environ.get("JANUS_RX") or shutil.which("janus-rx")
        if not self._tx or not self._rx:
            raise MediumUnsupported(
                "janus-c not found: need janus-tx/janus-rx on PATH or $JANUS_TX/$JANUS_RX. "
                "Install the GPL-3.0 reference (e.g. `nix build .#janus-c` or "
                "`nix develop .#janus`). See Documentation/DCF_JANUS_SPEC.md")
        self._pset_id, self._fs = str(pset_id), str(fs)
        self._pset = pset_file or os.environ.get("JANUS_PSET") or _janus_default_pset(self._tx)
        self._class_id, self._app_type = class_id, app_type
        self._env = _janus_env(self._tx)   # plugins dir on LD_LIBRARY_PATH (cargo codec)

    def _common(self):
        a = ["--pset-id", self._pset_id, "--stream-fs", self._fs,
             "--stream-driver", "wav"]
        if self._pset:
            a += ["--pset-file", self._pset]
        return a

    def _encode_file(self, frame, path):
        cmd = [self._tx, *self._common(), "--stream-driver-args", path,
               "--packet-cargo", bytes(frame).hex()]
        if self._class_id is not None:
            cmd += ["--packet-class-id", str(self._class_id)]
        if self._app_type is not None:
            cmd += ["--packet-app-type", str(self._app_type)]
        subprocess.run(cmd, check=True, env=self._env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)

    def _decode_file(self, path):
        # --verbose makes janus-rx print the recovered cargo (on stderr).
        cmd = [self._rx, *self._common(), "--stream-driver-args", path, "--verbose", "1"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 env=self._env, timeout=60)
        except subprocess.TimeoutExpired:
            return None
        return _janus_parse_cargo(res.stdout + res.stderr)


# ── HydraModem acoustic/line transport ────────────────────────────────────────
# Carries the 17-byte frame over HydraModem (continuous-phase M-FSK + soft-Viterbi
# FEC), the modem we hardware-verified at 0% PER over a guitar cable. The transport
# shells out to the single-frame `frame_tx`/`frame_rx` tools
# (hydramodem/dcf-tools/build.sh) — a subprocess PHY, like the audio/janus transports.

def hydramodem_available():
    """True iff the HydraModem frame tools are reachable (PATH or $HYDRA_TX/$HYDRA_RX)."""
    tx = os.environ.get("HYDRA_TX") or shutil.which("frame_tx")
    rx = os.environ.get("HYDRA_RX") or shutil.which("frame_rx")
    return bool(tx and rx)


_HYDRA_CAPS = {}


def hydra_tool_caps(tool):
    """The optional flags a frame_tx/frame_rx build understands (from its usage text):
    a subset of {"--profile", "--interleave", "--preamble"}. Older builds (before the
    DCF-Medium tool flags) have none of them; the result is cached per binary path."""
    if tool in _HYDRA_CAPS:
        return _HYDRA_CAPS[tool]
    try:
        res = subprocess.run([tool], capture_output=True, text=True, timeout=10)
        usage = res.stdout + res.stderr
    except (OSError, subprocess.TimeoutExpired):
        usage = ""
    caps = {f for f in ("--profile", "--interleave", "--preamble") if f in usage}
    _HYDRA_CAPS[tool] = caps
    return caps


# The musical tone-table HydraModem profiles (hydra_profile_music presets): M-FSK on a
# just-intonation scale built from the baud's harmonic series. See hydramodem/docs/MUSIC.md.
HYDRA_MUSIC_PROFILES = ("melody", "chime", "nocturne", "bass")
# The polyphonic duet (hydra_profile_duet): TWO frames per WAV, the melody voice
# carrying the first and the bass voice the second. Needs poly_tx/poly_rx (tool) or
# libhydramodem's hydra_modem_tx_poly (cffi).
HYDRA_POLY_PROFILES = ("duet",)
HYDRA_PROFILES = ("default", "aux") + HYDRA_MUSIC_PROFILES + HYDRA_POLY_PROFILES

# The `aux` HydraModem profile (hydra_profile_aux_cable): 1200 baud, tones 1200/2400 Hz,
# 16-symbol preamble, conv + interleave. Applied as explicit tool flags when the tool has
# no --profile switch.
HYDRA_AUX_FLAGS = ["--base-freq", "1200", "--tone-spacing", "1200", "--baud", "1200"]


class HydraTransport(_DirMedium):
    """Carry frames over HydraModem via the `frame_tx`/`frame_rx` tools. Raises
    MediumUnsupported at construction if they aren't built/on PATH (run
    hydramodem/dcf-tools/build.sh, then set $HYDRA_TX/$HYDRA_RX or put build/ on PATH).

    profile="default"|"aux" picks hydra_profile_default / hydra_profile_aux_cable;
    "melody"|"chime"|"nocturne" pick the musical tone-table profiles (need a
    frame_tx/frame_rx with --profile);
    interleave=0 disables the coded-bit interleaver (needs a tool with --interleave,
    else MediumUnsupported); base_freq/tone_spacing/baud/n_tones override the profile's
    tone plan (FDMA channels)."""

    EXT = ".wav"

    def __init__(self, name="hydra", fec="conv", tx_bin=None, rx_bin=None,
                 base_freq=None, tone_spacing=None, baud=None, n_tones=None,
                 profile="default", interleave=None, rate_bps=8000, **kw):
        if profile not in HYDRA_PROFILES:
            raise ValueError(f"hydra profile must be {'|'.join(HYDRA_PROFILES)}, got {profile!r}")
        if fec not in ("none", "rep3", "conv"):
            raise ValueError(f"hydra fec must be none|rep3|conv, got {fec!r}")
        if profile == "duet":
            if any(x is not None for x in (base_freq, tone_spacing, baud, n_tones, interleave)):
                raise ValueError("hydra: profile=duet fixes its tone plan and FEC; "
                                 "base_freq/tone_spacing/baud/n_tones/interleave do not apply")
            self._init_duet(name, tx_bin, rx_bin, rate_bps, kw)
            return
        tx = tx_bin or os.environ.get("HYDRA_TX") or shutil.which("frame_tx")
        rx = rx_bin or os.environ.get("HYDRA_RX") or shutil.which("frame_rx")
        if not tx or not rx:
            raise MediumUnsupported(
                "HydraModem tools not found: build hydramodem/dcf-tools (build.sh) and put "
                "frame_tx/frame_rx on PATH or in $HYDRA_TX/$HYDRA_RX. See "
                "Documentation/DCF_SENSE_SPEC.md")
        caps = hydra_tool_caps(tx) & hydra_tool_caps(rx)
        prof = []
        if profile in HYDRA_MUSIC_PROFILES:
            if "--profile" not in caps:
                raise MediumUnsupported(
                    f"hydra: profile={profile} needs frame_tx/frame_rx with --profile "
                    "(rebuild hydramodem/dcf-tools)")
            prof += ["--profile", profile]
        if profile == "aux":
            if "--profile" in caps:
                prof += ["--profile", "aux"]
            else:
                prof += list(HYDRA_AUX_FLAGS)
                if "--preamble" in caps:
                    prof += ["--preamble", "16"]
        if interleave is not None and not int(interleave):
            if "--interleave" not in caps:
                raise MediumUnsupported(
                    "hydra: interleave=0 needs frame_tx/frame_rx with --interleave "
                    "(rebuild hydramodem/dcf-tools)")
            prof += ["--interleave", "0"]
        # optional FDMA tone-channel profile (per-node distinct frequency band)
        for flag, val in (("--base-freq", base_freq), ("--tone-spacing", tone_spacing),
                          ("--baud", baud), ("--n-tones", n_tones)):
            if val is not None:
                prof += [flag, str(val)]
        super().__init__(name, rate_bps=rate_bps, **kw)
        self._tx, self._rx = tx, rx
        self._fec = "--" + fec
        self._prof = prof

    def _init_duet(self, name, tx_bin, rx_bin, rate_bps, kw):
        """profile=duet: two frames per WAV through poly_tx/poly_rx. They are found as
        $HYDRA_POLY_TX/$HYDRA_POLY_RX, next to the resolved frame_tx/frame_rx (the
        tx=/rx= options or $HYDRA_TX/$HYDRA_RX), or on PATH. The duet has no FEC,
        interleave or tone-plan options: hydra_profile_duet fixes them."""
        def sibling(env, explicit, ref_env, ref_name, name_):
            if os.environ.get(env):
                return os.environ[env]
            ref = explicit or os.environ.get(ref_env) or shutil.which(ref_name)
            if ref:
                cand = os.path.join(os.path.dirname(os.path.abspath(ref)), name_)
                if os.access(cand, os.X_OK):
                    return cand
            return shutil.which(name_)
        tx = sibling("HYDRA_POLY_TX", tx_bin, "HYDRA_TX", "frame_tx", "poly_tx")
        rx = sibling("HYDRA_POLY_RX", rx_bin, "HYDRA_RX", "frame_rx", "poly_rx")
        if not tx or not rx:
            raise MediumUnsupported(
                "hydra: profile=duet needs poly_tx/poly_rx: build hydramodem/dcf-tools "
                "(build.sh) and put them next to frame_tx/frame_rx, on PATH, or in "
                "$HYDRA_POLY_TX/$HYDRA_POLY_RX")
        _DirMedium.__init__(self, name, rate_bps=rate_bps, **kw)
        self.PAIRS = True
        self._tx, self._rx = tx, rx

    def _encode_pair_file(self, a, b, path):
        args = [self._tx, bytes(a).hex()] + ([bytes(b).hex()] if b is not None else [])
        subprocess.run(args + [path], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)

    def _encode_file(self, frame, path):
        subprocess.run([self._tx, bytes(frame).hex(), path, self._fec, *self._prof],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)

    def _decode_file(self, path):
        if self.PAIRS:
            try:
                res = subprocess.run([self._rx, path], capture_output=True, text=True,
                                     timeout=120)
            except subprocess.TimeoutExpired:
                return None
            out = []
            for line in res.stdout.split():
                try:
                    b = bytes.fromhex(line)
                except ValueError:
                    b = None                   # "-": that voice did not decode
                out.append(b if b is not None and len(b) == FRAME_LEN else None)
            return out
        try:
            res = subprocess.run([self._rx, path, self._fec, *self._prof],
                                 capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return None
        if res.returncode != 0:
            return None
        h = res.stdout.strip()
        try:
            b = bytes.fromhex(h)
        except ValueError:
            return None
        return b if len(b) == FRAME_LEN else None


def hydramodem_cffi_available():
    """True iff the in-process ctypes HydraModem binding can load libhydramodem."""
    try:
        from .hydramodem_cffi import available
        return available()
    except Exception:
        return False


class HydraCffiTransport(_DirMedium):
    """HydraModem via the in-process ctypes binding (no subprocess) — faster than
    HydraTransport, same WAV-file medium. Accepts the same profile/interleave/FDMA kwargs.
    See dcf.hydramodem_cffi (needs libhydramodem.so; $HYDRAMODEM_LIB or hydramodem/build);
    raises MediumUnsupported when the library cannot be loaded."""

    EXT = ".wav"

    def __init__(self, name="hydra", fec="conv", base_freq=None, tone_spacing=None,
                 baud=None, n_tones=None, profile="default", interleave=None,
                 rate_bps=8000, **kw):
        from .hydramodem_cffi import HydraDuet, HydraModem, available
        if not available():
            raise MediumUnsupported("libhydramodem not loadable (build hydramodem/ or set "
                                    "$HYDRAMODEM_LIB)")
        if profile == "duet":
            codec = HydraDuet()
        else:
            codec = HydraModem(fec=fec, base_freq=base_freq, tone_spacing=tone_spacing,
                               baud=baud, n_tones=n_tones, profile=profile,
                               interleave=interleave)
        super().__init__(name, rate_bps=rate_bps, **kw)
        self.PAIRS = profile == "duet"
        self._codec = codec

    def _encode_pair_file(self, a, b, path):
        self._codec.encode_pair_wav(a, b, path)

    def _encode_file(self, frame, path):
        self._codec.encode_wav(frame, path)

    def _decode_file(self, path):
        if self.PAIRS:
            return list(self._codec.decode_pair_wav(path))
        return self._codec.decode_wav(path)
