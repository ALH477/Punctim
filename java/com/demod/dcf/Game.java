// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * DCF-Game L2 framing (Documentation/DCF_GAME_SPEC.md), a port of the canonical
 * python/MCP/gamelab_core.py and byte-certified against Documentation/game_vectors.json.
 *
 * <pre>
 * seq (u16)      = packet_id[15:5] (11 bits) | frag_idx[4:0] (5 bits)
 * frag_idx == 0  descriptor : payload = [payload_len, frag_total, msg_type_id, flags]
 * frag_idx 1..N  data       : payload = bytes[(k-1)*4 .. +4]   (last frame zero-padded)
 * frame type     = DATA (0); ts_us identical across a message's frames
 * </pre>
 *
 * msg_type_id is opaque to L2. SNAPSHOT/INPUT/JOIN bodies are certified; EVENT is opaque
 * (DCF-Minecraft pins its own EVENT sub-types in {@link McEvent}).
 */
public final class Game {
    public static final int FDATA = 0;
    public static final int FRAG_BITS = 5;
    public static final int FRAG_MASK = (1 << FRAG_BITS) - 1;   // 0x1F
    public static final int MAX_FRAGS = FRAG_MASK;              // 31
    public static final int MAX_PAYLOAD = MAX_FRAGS * 4;        // 124
    public static final int MAX_PACKET_ID = (1 << (16 - FRAG_BITS)) - 1;  // 2047

    public static final int GMSG_SNAPSHOT = 0;
    public static final int GMSG_INPUT = 1;
    public static final int GMSG_EVENT = 2;
    public static final int GMSG_JOIN = 3;

    public static final int FLAG_RELIABLE = 0x01;
    public static final int FLAG_ORDERED = 0x02;
    public static final int FLAG_END_TICK = 0x04;

    public static final int SNAPSHOT_LEN = 14;
    public static final int INPUT_LEN = 6;

    private Game() {
    }

    private static Frame frame(int seq, int src, int dst, byte[] payload, int tsUs) {
        Frame f = new Frame();
        f.type = FDATA;
        f.seq = seq;
        f.src = src;
        f.dst = dst;
        f.payload = payload;
        f.tsUs = tsUs;
        return f;
    }

    /** One game message (0..124 bytes) -> [descriptor, data...] encoded 17-byte frames. */
    public static List<byte[]> packetize(int msgTypeId, byte[] payload, int packetId, int tsUs,
                                         int src, int dst, int flags) {
        if (msgTypeId < 0 || msgTypeId > 255) {
            throw new IllegalArgumentException("msg_type_id must be u8");
        }
        if (flags < 0 || flags > 255) {
            throw new IllegalArgumentException("flags must be u8");
        }
        if (packetId < 0 || packetId > MAX_PACKET_ID) {
            throw new IllegalArgumentException("packet_id must be 0.." + MAX_PACKET_ID);
        }
        if (payload.length > MAX_PAYLOAD) {
            throw new IllegalArgumentException("payload exceeds " + MAX_PAYLOAD + " B/message");
        }
        int fragTotal = (payload.length + 3) / 4;
        List<byte[]> out = new ArrayList<>(1 + fragTotal);
        byte[] desc = {(byte) payload.length, (byte) fragTotal, (byte) msgTypeId, (byte) flags};
        out.add(frame(packetId << FRAG_BITS, src, dst, desc, tsUs).encode());
        for (int k = 1; k <= fragTotal; k++) {
            byte[] chunk = new byte[4];
            int off = (k - 1) * 4;
            System.arraycopy(payload, off, chunk, 0, Math.min(4, payload.length - off));
            out.add(frame((packetId << FRAG_BITS) | k, src, dst, chunk, tsUs).encode());
        }
        return out;
    }

    /** A reassembled message. */
    public static final class Packet {
        public final int packetId;
        public final int tsUs;
        public final int msgTypeId;
        public final byte[] payload;
        public final int flags;

        Packet(int packetId, int tsUs, int msgTypeId, byte[] payload, int flags) {
            this.packetId = packetId;
            this.tsUs = tsUs;
            this.msgTypeId = msgTypeId;
            this.payload = payload;
            this.flags = flags;
        }
    }

    /**
     * Stateful reassembler: {@link #push} emits a message as soon as its descriptor and every
     * data fragment have arrived (any order, duplicates ignored); {@link #finalizeLost} reports
     * the packet ids still incomplete, in ascending order, and clears them.
     */
    public static final class Reassembler {
        private static final class Entry {
            int[] desc;                 // {payload_len, frag_total, msg_type_id, flags}
            int ts;
            final Map<Integer, byte[]> frags = new LinkedHashMap<>();
        }

        private final Map<Integer, Entry> pkts = new TreeMap<>();

        public List<Packet> push(byte[] frame) {
            List<Packet> out = new ArrayList<>(1);
            Frame d;
            try {
                d = Frame.decode(frame);
            } catch (IllegalArgumentException e) {
                return out;
            }
            if (d.type != FDATA) {
                return out;
            }
            int packetId = d.seq >> FRAG_BITS;
            int fragIdx = d.seq & FRAG_MASK;
            Entry e = pkts.computeIfAbsent(packetId, k -> new Entry());
            e.ts = d.tsUs;
            if (fragIdx == 0) {
                if (e.desc == null) {
                    e.desc = new int[] {d.payload[0] & 0xFF, d.payload[1] & 0xFF,
                                        d.payload[2] & 0xFF, d.payload[3] & 0xFF};
                }
            } else {
                e.frags.putIfAbsent(fragIdx, d.payload);
            }
            Packet p = tryEmit(packetId);
            if (p != null) {
                out.add(p);
            }
            return out;
        }

        private Packet tryEmit(int packetId) {
            Entry e = pkts.get(packetId);
            if (e == null || e.desc == null) {
                return null;
            }
            int len = e.desc[0];
            int total = e.desc[1];
            for (int k = 1; k <= total; k++) {
                if (!e.frags.containsKey(k)) {
                    return null;
                }
            }
            byte[] raw = new byte[total * 4];
            for (int k = 1; k <= total; k++) {
                System.arraycopy(e.frags.get(k), 0, raw, (k - 1) * 4, 4);
            }
            byte[] payload = new byte[Math.min(len, raw.length)];
            System.arraycopy(raw, 0, payload, 0, payload.length);
            pkts.remove(packetId);
            return new Packet(packetId, e.ts, e.desc[2], payload, e.desc[3]);
        }

        /** Packet ids still incomplete (ascending); the state is cleared. */
        public List<Integer> finalizeLost() {
            List<Integer> lost = new ArrayList<>(pkts.keySet());
            pkts.clear();
            return lost;
        }
    }

    // ── SNAPSHOT (msg_type 0): 14 bytes, Q8.8 ─────────────────────────────────
    private static int q88(double v) {
        long q = Math.round(v * 256.0);
        if (q > 32767) {
            q = 32767;
        } else if (q < -32768) {
            q = -32768;
        }
        return (int) q & 0xFFFF;
    }

    private static double unq88(int u) {
        int s = (u & 0x8000) != 0 ? u - 0x10000 : u;
        return s / 256.0;
    }

    /** {x,y,z,vx,vy,vz} in metres / metres-per-tick, yaw u16 (0..65535 = 0..2π). */
    public static byte[] snapshotPack(double[] xyzv, int yaw) {
        byte[] b = new byte[SNAPSHOT_LEN];
        for (int i = 0; i < 6; i++) {
            int q = q88(xyzv[i]);
            b[2 * i] = (byte) (q >> 8);
            b[2 * i + 1] = (byte) q;
        }
        b[12] = (byte) (yaw >> 8);
        b[13] = (byte) yaw;
        return b;
    }

    /** Returns {x,y,z,vx,vy,vz,yaw} (yaw as an exact integer in a double). */
    public static double[] snapshotUnpack(byte[] b) {
        if (b.length != SNAPSHOT_LEN) {
            throw new IllegalArgumentException("SNAPSHOT is " + SNAPSHOT_LEN + " bytes");
        }
        double[] out = new double[7];
        for (int i = 0; i < 6; i++) {
            out[i] = unq88(((b[2 * i] & 0xFF) << 8) | (b[2 * i + 1] & 0xFF));
        }
        out[6] = ((b[12] & 0xFF) << 8) | (b[13] & 0xFF);
        return out;
    }

    // ── INPUT (msg_type 1): tick u32 + buttons u16 ────────────────────────────
    public static byte[] inputPack(long tick, int buttons) {
        return new byte[] {(byte) (tick >> 24), (byte) (tick >> 16), (byte) (tick >> 8), (byte) tick,
                           (byte) (buttons >> 8), (byte) buttons};
    }

    /** Returns {tick, buttons}. */
    public static long[] inputUnpack(byte[] b) {
        if (b.length != INPUT_LEN) {
            throw new IllegalArgumentException("INPUT is " + INPUT_LEN + " bytes");
        }
        long tick = ((long) (b[0] & 0xFF) << 24) | ((b[1] & 0xFF) << 16) | ((b[2] & 0xFF) << 8) | (b[3] & 0xFF);
        return new long[] {tick, ((b[4] & 0xFF) << 8) | (b[5] & 0xFF)};
    }

    // ── JOIN (msg_type 3): player_id u16 + len-prefixed UTF-8 name ────────────
    public static byte[] joinPack(int playerId, String name) {
        byte[] n = name.getBytes(StandardCharsets.UTF_8);
        int len = Math.min(n.length, MAX_PAYLOAD - 3);
        byte[] out = new byte[3 + len];
        out[0] = (byte) (playerId >> 8);
        out[1] = (byte) playerId;
        out[2] = (byte) len;
        System.arraycopy(n, 0, out, 3, len);
        return out;
    }

    /** Returns the player id; the name is {@link #joinName}. */
    public static int joinPlayerId(byte[] b) {
        return ((b[0] & 0xFF) << 8) | (b[1] & 0xFF);
    }

    public static String joinName(byte[] b) {
        int len = b[2] & 0xFF;
        return new String(b, 3, len, StandardCharsets.UTF_8);
    }
}
