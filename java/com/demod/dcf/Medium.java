// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * DCF-Medium — the digital medium codecs (Tier B: stream, hex, udp_proto, udp_bare,
 * l2eth), byte-identical to the canonical Python reference
 * python/MCP/mediumlab_core.py and certified against Documentation/medium_vectors.json
 * (spec: Documentation/DCF_MEDIUM_SPEC.md).
 *
 * <p>A medium codec is a deterministic pair {@code encode: [frame] -> representation},
 * {@code decode: representation -> ([frame], diagnostics)} that carries the 17-byte
 * DeModFrame quantum over one medium WITHOUT parsing it beyond the frame gate (sync 0xD3
 * + version nibble 1 + CRC-16/CCITT-FALSE). Media are transports <em>beneath</em> the
 * quantum, so the 246-vector wire certificate is untouched.
 *
 * <pre>
 *   stream     .dcf file / stdio: concatenated frames; decode is a byte-wise resync scan
 *   hex        34 lowercase hex chars + "\n" per frame; tolerant line parser
 *   udp_proto  ProtoMessage [type u8][seq u32][ts u64][len u32][payload] (big-endian);
 *              one frame = msg_type FRAME (12), len 17 -&gt; a 34-byte datagram
 *   udp_bare   consecutive pairs -&gt; one 32-byte SuperPack, lone trailing frame raw 17 B
 *   l2eth      [n_frames u16 BE][SuperPack * ceil(n/2)], odd tail paired with the filler
 * </pre>
 *
 * <p>Frames are {@code byte[17]}; u32 sequence numbers are carried in a {@code long}
 * (0..2^32-1) and u64 timestamps in a {@code long} holding the raw 64 bits (use
 * {@link Long#toUnsignedString(long)} / {@link Long#parseUnsignedLong(String, int)}).
 */
public final class Medium {

    public static final int FRAME_LEN = Frame.FRAME_SIZE;

    // ── udp dialect "proto": the ProtoMessage msg_type registry ──────────────────────
    public static final int PROTO_HEADER_LEN = 17;
    public static final int MSG_POSITION = 1;
    public static final int MSG_AUDIO = 2;
    public static final int MSG_GAME_EVENT = 3;
    public static final int MSG_STATE_SYNC = 4;
    public static final int MSG_RELIABLE = 5;
    public static final int MSG_ACK = 6;
    public static final int MSG_PING = 7;
    public static final int MSG_PONG = 8;
    public static final int MSG_GAME_DCF = 9;
    public static final int MSG_TEXT_DCF = 10;
    public static final int MSG_MESH = 11;
    public static final int MSG_FRAME = 12;

    /** Name -&gt; msg_type, in registry order (mirrors mediumlab_core.MSG_TYPES). */
    public static final Map<String, Integer> MSG_TYPES;

    static {
        Map<String, Integer> m = new LinkedHashMap<>();
        m.put("POSITION", MSG_POSITION);
        m.put("AUDIO", MSG_AUDIO);
        m.put("GAME_EVENT", MSG_GAME_EVENT);
        m.put("STATE_SYNC", MSG_STATE_SYNC);
        m.put("RELIABLE", MSG_RELIABLE);
        m.put("ACK", MSG_ACK);
        m.put("PING", MSG_PING);
        m.put("PONG", MSG_PONG);
        m.put("GAME_DCF", MSG_GAME_DCF);
        m.put("TEXT_DCF", MSG_TEXT_DCF);
        m.put("MESH", MSG_MESH);
        m.put("FRAME", MSG_FRAME);
        MSG_TYPES = Collections.unmodifiableMap(m);
    }

    // ── l2eth ─────────────────────────────────────────────────────────────────────────
    public static final int L2_HDR = 2; // the n_frames u16

    private Medium() {}

    // ══ the frame gate (the existing validity rule; media never parse further) ═══════
    /** True iff w is a valid DeModFrame: 17 bytes, sync 0xD3, version nibble 1, CRC. */
    public static boolean gate(byte[] w) {
        return w != null && w.length == FRAME_LEN && gateAt(w, 0);
    }

    private static boolean gateAt(byte[] b, int i) {
        if ((b[i] & 0xFF) != Frame.SYNC || ((b[i + 1] & 0xFF) >>> 4) != Frame.VERSION) {
            return false;
        }
        return Frame.syndrome(take(b, i, FRAME_LEN)) == 0;
    }

    private static byte[] take(byte[] b, int off, int len) {
        byte[] out = new byte[len];
        System.arraycopy(b, off, out, 0, len);
        return out;
    }

    private static void need17(byte[] f) {
        if (f == null || f.length != FRAME_LEN) {
            throw new IllegalArgumentException("need 17-byte frames, got "
                    + (f == null ? "null" : Integer.toString(f.length)));
        }
    }

    // ══ stream (.dcf file, stdio) ═════════════════════════════════════════════════════
    /** Concatenate 17-byte frames (the .dcf / stdio representation). */
    public static byte[] streamEncode(List<byte[]> frames) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(frames.size() * FRAME_LEN);
        for (byte[] f : frames) {
            need17(f);
            out.write(f, 0, FRAME_LEN);
        }
        return out.toByteArray();
    }

    /**
     * Byte-wise resync scan of buf[0..n) from offset i: at offset i, if the 17-byte window
     * passes the gate, emit it and i += 17; else i += 1, skipped += 1. Returns
     * {newOffset, skippedDelta}; n - newOffset &lt; 17 on return.
     */
    private static long[] scan(byte[] buf, int n, int i, List<byte[]> out) {
        long skipped = 0;
        while (n - i >= FRAME_LEN) {
            if (gateAt(buf, i)) {
                out.add(take(buf, i, FRAME_LEN));
                i += FRAME_LEN;
            } else {
                i++;
                skipped++;
            }
        }
        return new long[] {i, skipped};
    }

    /** Result of {@link #streamDecode(byte[])}. */
    public static final class StreamResult {
        public final List<byte[]> frames;
        /** Offsets rejected by the gate. */
        public final long skippedBytes;
        /** Trailing &lt; 17 bytes that can never hold a frame (not counted as skipped). */
        public final int tailBytes;

        StreamResult(List<byte[]> frames, long skippedBytes, int tailBytes) {
            this.frames = frames;
            this.skippedBytes = skippedBytes;
            this.tailBytes = tailBytes;
        }
    }

    /** Byte-wise resync decode of a whole stream. */
    public static StreamResult streamDecode(byte[] buf) {
        List<byte[]> frames = new ArrayList<>();
        long[] r = scan(buf, buf.length, 0, frames);
        return new StreamResult(frames, r[1], buf.length - (int) r[0]);
    }

    /**
     * Incremental stream decoder. {@link #feed} returns the frames completed so far and
     * keeps a carry of at most 16 bytes, so any chunking yields exactly
     * {@link #streamDecode}'s frames and skipped bytes; {@link #flush} returns (and
     * clears) the final tail bytes.
     */
    public static final class StreamScanner {
        private byte[] carry = new byte[0];
        private long skippedBytes;
        private long framesOut;

        public List<byte[]> feed(byte[] chunk) {
            return feed(chunk, 0, chunk.length);
        }

        public List<byte[]> feed(byte[] chunk, int off, int len) {
            byte[] buf = new byte[carry.length + len];
            System.arraycopy(carry, 0, buf, 0, carry.length);
            System.arraycopy(chunk, off, buf, carry.length, len);
            List<byte[]> frames = new ArrayList<>();
            long[] r = scan(buf, buf.length, 0, frames);
            skippedBytes += r[1];
            carry = take(buf, (int) r[0], buf.length - (int) r[0]);
            framesOut += frames.size();
            return frames;
        }

        /** Return (and clear) the carried tail bytes. */
        public byte[] flush() {
            byte[] tail = carry;
            carry = new byte[0];
            return tail;
        }

        public long skippedBytes() {
            return skippedBytes;
        }

        public long framesOut() {
            return framesOut;
        }

        /** Bytes currently carried (a possible frame prefix), &lt;= 16. */
        public int pending() {
            return carry.length;
        }
    }

    // ══ hex (text lines) ══════════════════════════════════════════════════════════════
    /** One frame per line: 34 lowercase hex characters + "\n". */
    public static String hexEncode(List<byte[]> frames) {
        StringBuilder sb = new StringBuilder(frames.size() * (2 * FRAME_LEN + 1));
        for (byte[] f : frames) {
            need17(f);
            sb.append(Frame.hex(f)).append('\n');
        }
        return sb.toString();
    }

    /** Result of {@link #hexDecode(String)}. */
    public static final class HexResult {
        public final List<byte[]> frames;
        public final int badLines;

        HexResult(List<byte[]> frames, int badLines) {
            this.frames = frames;
            this.badLines = badLines;
        }
    }

    private static boolean isWs(char c) {
        return c == ' ' || c == '\t' || c == '\r' || c == 0x0B || c == '\f';
    }

    /**
     * Parse hex lines. Lines are split on "\n"; leading/trailing space, tab, CR, VT, FF
     * are stripped; blank lines and lines starting with "#" are skipped (not counted);
     * uppercase is accepted; any other line that is not exactly 34 hex digits counts as a
     * bad line. Frames are returned raw (NOT gated — the caller gates).
     */
    public static HexResult hexDecode(String text) {
        List<byte[]> frames = new ArrayList<>();
        int bad = 0;
        int pos = 0;
        while (true) {
            int nl = text.indexOf('\n', pos);
            int end = nl < 0 ? text.length() : nl;
            int a = pos;
            int b = end;
            while (a < b && isWs(text.charAt(a))) {
                a++;
            }
            while (b > a && isWs(text.charAt(b - 1))) {
                b--;
            }
            if (a < b && text.charAt(a) != '#') {
                byte[] f = (b - a == 2 * FRAME_LEN) ? new byte[FRAME_LEN] : null;
                for (int k = 0; f != null && k < FRAME_LEN; k++) {
                    int hi = hexVal(text.charAt(a + 2 * k));
                    int lo = hexVal(text.charAt(a + 2 * k + 1));
                    if (hi < 0 || lo < 0) {
                        f = null;
                    } else {
                        f[k] = (byte) ((hi << 4) | lo);
                    }
                }
                if (f != null) {
                    frames.add(f);
                } else {
                    bad++;
                }
            }
            if (nl < 0) {
                break;
            }
            pos = nl + 1;
        }
        return new HexResult(frames, bad);
    }

    /** ASCII hex digit value, or -1 (Character.digit would also accept non-ASCII digits). */
    private static int hexVal(char c) {
        if (c >= '0' && c <= '9') {
            return c - '0';
        }
        if (c >= 'a' && c <= 'f') {
            return c - 'a' + 10;
        }
        if (c >= 'A' && c <= 'F') {
            return c - 'A' + 10;
        }
        return -1;
    }

    // ══ udp dialect "proto" (ProtoMessage envelope) ═══════════════════════════════════
    /** A decoded ProtoMessage. {@code seq} is u32 (0..2^32-1); {@code ts} holds raw u64 bits. */
    public static final class ProtoMessage {
        public final int type;
        public final long seq;
        public final long ts;
        public final byte[] payload;

        public ProtoMessage(int type, long seq, long ts, byte[] payload) {
            this.type = type;
            this.seq = seq;
            this.ts = ts;
            this.payload = payload;
        }
    }

    /**
     * Serialize one ProtoMessage (17-byte big-endian header + payload). {@code seq} wraps
     * modulo 2^32; {@code ts} is written as its raw 64 bits (u64).
     */
    public static byte[] protoEncode(int type, long seq, long ts, byte[] payload) {
        if (type < 0 || type > 0xFF) {
            throw new IllegalArgumentException("msg_type must be u8");
        }
        byte[] out = new byte[PROTO_HEADER_LEN + payload.length];
        out[0] = (byte) type;
        long s = seq & 0xFFFFFFFFL;
        for (int k = 0; k < 4; k++) {
            out[1 + k] = (byte) (s >>> (8 * (3 - k)));
        }
        for (int k = 0; k < 8; k++) {
            out[5 + k] = (byte) (ts >>> (8 * (7 - k)));
        }
        long len = payload.length & 0xFFFFFFFFL;
        for (int k = 0; k < 4; k++) {
            out[13 + k] = (byte) (len >>> (8 * (3 - k)));
        }
        System.arraycopy(payload, 0, out, PROTO_HEADER_LEN, payload.length);
        return out;
    }

    /**
     * Parse one ProtoMessage. Throws IllegalArgumentException on a short header or a
     * payload_len that overruns the datagram. Bytes after payload_len are ignored.
     */
    public static ProtoMessage protoDecode(byte[] dg) {
        if (dg.length < PROTO_HEADER_LEN) {
            throw new IllegalArgumentException("message shorter than 17-byte header");
        }
        int type = dg[0] & 0xFF;
        long seq = 0;
        for (int k = 0; k < 4; k++) {
            seq = (seq << 8) | (dg[1 + k] & 0xFF);
        }
        long ts = 0;
        for (int k = 0; k < 8; k++) {
            ts = (ts << 8) | (dg[5 + k] & 0xFF);
        }
        long plen = 0;
        for (int k = 0; k < 4; k++) {
            plen = (plen << 8) | (dg[13 + k] & 0xFF);
        }
        if ((long) dg.length - PROTO_HEADER_LEN < plen) {
            throw new IllegalArgumentException("payload length exceeds message size");
        }
        return new ProtoMessage(type, seq, ts, take(dg, PROTO_HEADER_LEN, (int) plen));
    }

    /** One frame as a 34-byte ProtoMessage(MSG_FRAME, seq, ts, len 17, frame). */
    public static byte[] protoFrameEncode(byte[] frame, long seq, long ts) {
        need17(frame);
        return protoEncode(MSG_FRAME, seq, ts, frame);
    }

    /** {@link #protoFrameEncode(byte[], long, long)} with ts = 0 (the deterministic default). */
    public static byte[] protoFrameEncode(byte[] frame, long seq) {
        return protoFrameEncode(frame, seq, 0L);
    }

    /**
     * The carried frame, or null unless msg_type == 12 and payload_len == 17 (types 1..11
     * are adapter envelopes, not frames on this medium). Not gated — the caller gates.
     */
    public static byte[] protoFrameDecode(byte[] dg) {
        ProtoMessage m;
        try {
            m = protoDecode(dg);
        } catch (IllegalArgumentException e) {
            return null;
        }
        if (m.type != MSG_FRAME || m.payload.length != FRAME_LEN) {
            return null;
        }
        return m.payload;
    }

    // ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ═════════════════
    /**
     * Datagrams for a frame sequence: with {@code pair}, consecutive pairs become one
     * 32-byte SuperPack and a lone trailing frame goes raw (17 B); without, every frame
     * goes raw. Throws IllegalArgumentException if a frame to be paired is invalid.
     */
    public static List<byte[]> bareEncode(List<byte[]> frames, boolean pair) {
        List<byte[]> out = new ArrayList<>();
        int i = 0;
        if (pair) {
            for (; i + 1 < frames.size(); i += 2) {
                out.add(SuperPack.pack(frames.get(i), frames.get(i + 1)));
            }
        }
        for (; i < frames.size(); i++) {
            out.add(frames.get(i).clone());
        }
        return out;
    }

    /** {@link #bareEncode(List, boolean)} with pairing on (the default). */
    public static List<byte[]> bareEncode(List<byte[]> frames) {
        return bareEncode(frames, true);
    }

    /**
     * Frames carried by one bare datagram: a valid 32-byte SuperPack -&gt; its 2 frames; a
     * 17-byte datagram -&gt; [it] (not gated); anything else -&gt; [].
     */
    public static List<byte[]> bareDecode(byte[] dg) {
        List<byte[]> out = new ArrayList<>();
        if (dg.length == SuperPack.SUPER_LEN && SuperPack.isSuperPack(dg)) {
            try {
                byte[][] parts = SuperPack.unpack(dg);
                out.add(parts[0]);
                out.add(parts[1]);
            } catch (RuntimeException e) {
                out.clear();
            }
            return out;
        }
        if (dg.length == FRAME_LEN) {
            out.add(dg.clone());
        }
        return out;
    }

    // ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ═
    /**
     * The canonical zero filler frame (a valid DATA DeModFrame with all application fields
     * 0); pairs an odd trailing frame; the receiver discards it using n_frames.
     */
    public static byte[] l2Filler() {
        Frame z = new Frame();
        z.type = 0;
        return z.encode();
    }

    /** Number of DeModFrames that fit one Ethernet payload of the given MTU. */
    public static int l2Capacity(int mtu) {
        if (mtu < L2_HDR) {
            return 0;
        }
        return ((mtu - L2_HDR) / SuperPack.SUPER_LEN) * 2;
    }

    /**
     * Batch frames into one Ethernet payload: [n_frames u16 BE][SuperPack * ceil(n/2)], the
     * odd tail paired with {@link #l2Filler()}.
     */
    public static byte[] l2Batch(List<byte[]> frames) {
        int n = frames.size();
        if (n > 0xFFFF) {
            throw new IllegalArgumentException("too many frames for one batch");
        }
        byte[] filler = l2Filler();
        ByteArrayOutputStream out = new ByteArrayOutputStream(L2_HDR + ((n + 1) / 2) * SuperPack.SUPER_LEN);
        out.write((n >>> 8) & 0xFF);
        out.write(n & 0xFF);
        for (int i = 0; i < n; i += 2) {
            byte[] sp = SuperPack.pack(frames.get(i), i + 1 < n ? frames.get(i + 1) : filler);
            out.write(sp, 0, sp.length);
        }
        return out.toByteArray();
    }

    /**
     * Split an Ethernet payload back into its frames (bit-exact). Throws
     * IllegalArgumentException on a short/truncated batch or a corrupt SuperPack; trailing
     * bytes after the last SuperPack (Ethernet minimum-size padding) are ignored.
     */
    public static List<byte[]> l2Unbatch(byte[] buf) {
        if (buf.length < L2_HDR) {
            throw new IllegalArgumentException("short batch");
        }
        int n = ((buf[0] & 0xFF) << 8) | (buf[1] & 0xFF);
        int npairs = (n + 1) / 2;
        if (buf.length < L2_HDR + npairs * SuperPack.SUPER_LEN) {
            throw new IllegalArgumentException("truncated batch");
        }
        List<byte[]> frames = new ArrayList<>(n);
        int off = L2_HDR;
        for (int p = 0; p < npairs; p++, off += SuperPack.SUPER_LEN) {
            byte[][] parts = SuperPack.unpack(take(buf, off, SuperPack.SUPER_LEN));
            frames.add(parts[0]);
            if (frames.size() < n) {
                frames.add(parts[1]);
            }
        }
        return frames;
    }
}
