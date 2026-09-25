// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * DCF-Text L2 framing (Documentation/DCF_TEXT_SPEC.md), a port of the canonical
 * python/MCP/textlab_core.py and byte-certified against Documentation/text_vectors.json.
 *
 * <pre>
 * seq (u16)      = packet_id[15:10] (6 bits) | frag_idx[9:0] (10 bits)
 * frag_idx == 0  descriptor : payload = [len_hi, len_lo, flags, 0]
 * frag_idx 1..N  data       : payload = bytes[(k-1)*4 .. +4]   (last frame zero-padded)
 * frame type     = DATA (0); ts_us identical across a message's frames
 * </pre>
 *
 * Text and game both ride DATA(0) with different seq splits and no in-band tag: never
 * multiplex them on one dst channel.
 */
public final class Text {
    public static final int FDATA = 0;
    public static final int FRAG_BITS = 10;
    public static final int FRAG_MASK = (1 << FRAG_BITS) - 1;   // 0x3FF
    public static final int MAX_FRAGS = FRAG_MASK;              // 1023
    public static final int MAX_PAYLOAD = MAX_FRAGS * 4;        // 4092
    public static final int MAX_PACKET_ID = (1 << (16 - FRAG_BITS)) - 1;  // 63
    public static final int BROADCAST = 0xFFFF;

    public static final int FLAG_AGENT = 0x01;
    public static final int FLAG_MORE = 0x02;
    public static final int FLAG_RELIABLE = 0x04;

    private Text() {
    }

    /** Channel name -> 16-bit rendezvous dst (CRC-16/CCITT-FALSE of the UTF-8 name). */
    public static int channelId(String name) {
        if (name == null || name.isEmpty()) {
            return BROADCAST;
        }
        return Frame.crc16(name.getBytes(StandardCharsets.UTF_8));
    }

    public static List<byte[]> packetize(String text, int packetId, int tsUs, int src, int dst, int flags) {
        return packetize(text.getBytes(StandardCharsets.UTF_8), packetId, tsUs, src, dst, flags);
    }

    /** One message (<= 4092 bytes) -> [descriptor, data...] encoded 17-byte frames. */
    public static List<byte[]> packetize(byte[] payload, int packetId, int tsUs, int src, int dst, int flags) {
        if (flags < 0 || flags > 255) {
            throw new IllegalArgumentException("flags must be u8");
        }
        if (packetId < 0 || packetId > MAX_PACKET_ID) {
            throw new IllegalArgumentException("packet_id must be 0.." + MAX_PACKET_ID);
        }
        if (payload.length > MAX_PAYLOAD) {
            throw new IllegalArgumentException("message exceeds " + MAX_PAYLOAD + " B cap");
        }
        int fragTotal = (payload.length + 3) / 4;
        List<byte[]> out = new ArrayList<>(1 + fragTotal);
        byte[] desc = {(byte) (payload.length >> 8), (byte) payload.length, (byte) flags, 0};
        out.add(frame(packetId << FRAG_BITS, src, dst, desc, tsUs).encode());
        for (int k = 1; k <= fragTotal; k++) {
            byte[] chunk = new byte[4];
            int off = (k - 1) * 4;
            System.arraycopy(payload, off, chunk, 0, Math.min(4, payload.length - off));
            out.add(frame((packetId << FRAG_BITS) | k, src, dst, chunk, tsUs).encode());
        }
        return out;
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

    /** A reassembled message. */
    public static final class Message {
        public final int packetId;
        public final int tsUs;
        public final int src;
        public final int dst;
        public final byte[] payload;
        public final int flags;

        Message(int packetId, int tsUs, int src, int dst, byte[] payload, int flags) {
            this.packetId = packetId;
            this.tsUs = tsUs;
            this.src = src;
            this.dst = dst;
            this.payload = payload;
            this.flags = flags;
        }

        /** UTF-8 decode, malformed input replaced (like Python's errors="replace"). */
        public String text() {
            return new String(payload, StandardCharsets.UTF_8);
        }
    }

    /** Stateful reassembler; {@code acceptDst < 0} accepts every channel. */
    public static final class Reassembler {
        private static final class Entry {
            int len = -1;
            int flags;
            int fragTotal;
            int ts;
            int src;
            int dst;
            final Map<Integer, byte[]> frags = new LinkedHashMap<>();
        }

        private final int acceptDst;
        private final Map<Integer, Entry> pkts = new TreeMap<>();

        public Reassembler() {
            this(-1);
        }

        public Reassembler(int acceptDst) {
            this.acceptDst = acceptDst;
        }

        public List<Message> push(byte[] frame) {
            List<Message> out = new ArrayList<>(1);
            Frame d;
            try {
                d = Frame.decode(frame);
            } catch (IllegalArgumentException e) {
                return out;
            }
            if (d.type != FDATA) {
                return out;
            }
            if (acceptDst >= 0 && d.dst != acceptDst && d.dst != BROADCAST) {
                return out;
            }
            int packetId = d.seq >> FRAG_BITS;
            int fragIdx = d.seq & FRAG_MASK;
            Entry e = pkts.computeIfAbsent(packetId, k -> new Entry());
            e.ts = d.tsUs;
            e.src = d.src;
            e.dst = d.dst;
            if (fragIdx == 0) {
                if (e.len < 0) {
                    e.len = ((d.payload[0] & 0xFF) << 8) | (d.payload[1] & 0xFF);
                    e.flags = d.payload[2] & 0xFF;
                    e.fragTotal = (e.len + 3) / 4;
                }
            } else {
                e.frags.putIfAbsent(fragIdx, d.payload);
            }
            Message m = tryEmit(packetId);
            if (m != null) {
                out.add(m);
            }
            return out;
        }

        private Message tryEmit(int packetId) {
            Entry e = pkts.get(packetId);
            if (e == null || e.len < 0) {
                return null;
            }
            for (int k = 1; k <= e.fragTotal; k++) {
                if (!e.frags.containsKey(k)) {
                    return null;
                }
            }
            byte[] raw = new byte[e.fragTotal * 4];
            for (int k = 1; k <= e.fragTotal; k++) {
                System.arraycopy(e.frags.get(k), 0, raw, (k - 1) * 4, 4);
            }
            byte[] payload = new byte[Math.min(e.len, raw.length)];
            System.arraycopy(raw, 0, payload, 0, payload.length);
            pkts.remove(packetId);
            return new Message(packetId, e.ts, e.src, e.dst, payload, e.flags);
        }

        /** Packet ids still incomplete (ascending); the state is cleared. */
        public List<Integer> finalizeLost() {
            List<Integer> lost = new ArrayList<>(pkts.keySet());
            pkts.clear();
            return lost;
        }
    }
}
