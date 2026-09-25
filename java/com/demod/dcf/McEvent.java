// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * DCF-Minecraft: the register model and the EVENT sub-type codec, a port of the canonical
 * python/MCP/mclab_core.py, byte-certified against Documentation/minecraft_vectors.json.
 *
 * <p>The register is a fixed bit-placement of the 17-byte DeModFrame inside a Minecraft world:
 * nibble i (high nibble first) is OUT lane i's comparator strength (set by a barrel's item
 * count) and IN lane i's redstone-wire power; words w0..w4 are 3-byte big-endian slices and
 * w5 the 2-byte CRC, all &lt; 2^31 so they fit scoreboard scores.
 *
 * <p>EVENT sub-types ride DCF-Game EVENT (msg_type 2) messages on the {@code mc-world}
 * channel with a tag byte at payload[0]; chat never rides EVENT (it is DCF-Text).
 */
public final class McEvent {
    public static final int FRAME_LEN = Frame.FRAME_SIZE;      // 17
    public static final int NIBBLES = 2 * FRAME_LEN;            // 34
    public static final int WORDS = 6;
    public static final int[] WORD_BYTES = {3, 3, 3, 3, 3, 2};
    public static final int BARREL_SLOTS = 27;
    public static final int BARREL_CAPACITY = BARREL_SLOTS * 64; // 1728
    public static final int LANE_PITCH = 2;
    public static final int MAX_EVENT = Game.MAX_PAYLOAD;       // 124

    public static final String OBJ_REG = "dcf_reg";
    public static final String OBJ_CTL = "dcf_ctl";

    public static final int CH_WORLD = Text.channelId("mc-world");   // 0xD952
    public static final int CH_CHAT = Text.channelId("mc-chat");     // 0xE624

    public static final int EVT_REDSTONE = 1;
    public static final int EVT_BLOCK_SET = 2;
    public static final int EVT_CMD_TRIGGER = 3;
    public static final int EVT_SCOREBOARD = 4;

    public static final int DIM_OVERWORLD = 0;
    public static final int DIM_NETHER = 1;
    public static final int DIM_END = 2;
    public static final int DIM_OTHER = 255;

    private McEvent() {
    }

    // ── nibbles / words / items ──────────────────────────────────────────────
    public static int[] frameToNibbles(byte[] frame) {
        if (frame.length != FRAME_LEN) {
            throw new IllegalArgumentException("a DeModFrame is 17 bytes");
        }
        int[] n = new int[NIBBLES];
        for (int i = 0; i < FRAME_LEN; i++) {
            n[2 * i] = (frame[i] >> 4) & 0xF;
            n[2 * i + 1] = frame[i] & 0xF;
        }
        return n;
    }

    public static byte[] nibblesToFrame(int[] nibbles) {
        if (nibbles.length != NIBBLES) {
            throw new IllegalArgumentException("a register holds 34 nibbles");
        }
        byte[] f = new byte[FRAME_LEN];
        for (int i = 0; i < FRAME_LEN; i++) {
            f[i] = (byte) (((nibbles[2 * i] & 0xF) << 4) | (nibbles[2 * i + 1] & 0xF));
        }
        return f;
    }

    public static int[] frameToWords(byte[] frame) {
        if (frame.length != FRAME_LEN) {
            throw new IllegalArgumentException("a DeModFrame is 17 bytes");
        }
        int[] w = new int[WORDS];
        int off = 0;
        for (int i = 0; i < WORDS; i++) {
            int v = 0;
            for (int k = 0; k < WORD_BYTES[i]; k++) {
                v = (v << 8) | (frame[off++] & 0xFF);
            }
            w[i] = v;
        }
        return w;
    }

    public static byte[] wordsToFrame(int[] words) {
        if (words.length != WORDS) {
            throw new IllegalArgumentException("a register holds 6 words");
        }
        byte[] f = new byte[FRAME_LEN];
        int off = 0;
        for (int i = 0; i < WORDS; i++) {
            int n = WORD_BYTES[i];
            if (words[i] < 0 || words[i] >= (1 << (8 * n))) {
                throw new IllegalArgumentException("word " + words[i] + " does not fit " + n + " bytes");
            }
            for (int k = n - 1; k >= 0; k--) {
                f[off + k] = (byte) (words[i] >> (8 * (n - 1 - k)));
            }
            off += n;
        }
        return f;
    }

    /** Minecraft's container law: signal = floor(items*14/capacity) + (items > 0 ? 1 : 0). */
    public static int itemsToSignal(int items) {
        if (items <= 0) {
            return 0;
        }
        if (items > BARREL_CAPACITY) {
            throw new IllegalArgumentException("items exceed capacity");
        }
        return (items * 14) / BARREL_CAPACITY + 1;
    }

    /** Smallest item count that reads exactly {@code signal} (0..15; 15 = a full barrel). */
    public static int signalToItems(int signal) {
        if (signal < 0 || signal > 15) {
            throw new IllegalArgumentException("signal strength must be 0..15");
        }
        if (signal == 0) {
            return 0;
        }
        if (signal == 1) {
            return 1;
        }
        return ((signal - 1) * BARREL_CAPACITY + 13) / 14;   // ceil((s-1)*cap/14)
    }

    public static int[] frameToItems(byte[] frame) {
        int[] n = frameToNibbles(frame);
        int[] out = new int[NIBBLES];
        for (int i = 0; i < NIBBLES; i++) {
            out[i] = signalToItems(n[i]);
        }
        return out;
    }

    /** Parse a "DCF TX w0 w1 w2 w3 w4 w5" chat/log line into a frame, or null. */
    public static byte[] parseChatTx(String line) {
        int at = line.indexOf("DCF TX ");
        if (at < 0) {
            return null;
        }
        String[] tok = line.substring(at + 7).trim().split("\\s+");
        if (tok.length < WORDS) {
            return null;
        }
        int[] w = new int[WORDS];
        try {
            for (int i = 0; i < WORDS; i++) {
                w[i] = Integer.parseInt(tok[i]);
            }
            return wordsToFrame(w);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    // ── geometry ────────────────────────────────────────────────────────────
    /** Lane cell of lane i: kind 0=out (barrel), 1=cmp (comparator), 2=mid, 3=in (wire). */
    public static int[] lanePos(int[] origin, int i, int kind) {
        if (i < 0 || i >= NIBBLES || kind < 0 || kind > 3) {
            throw new IllegalArgumentException("lane 0..33, kind 0..3");
        }
        return new int[] {origin[0] + LANE_PITCH * i, origin[1], origin[2] + kind};
    }

    public static int[] strobeIn(int[] o) {
        return new int[] {o[0] - 2, o[1], o[2] + 3};
    }

    public static int[] validOut(int[] o) {
        return new int[] {o[0] - 2, o[1], o[2]};
    }

    public static int[] ackOut(int[] o) {
        return new int[] {o[0] - 4, o[1], o[2]};
    }

    public static int[] nakOut(int[] o) {
        return new int[] {o[0] - 6, o[1], o[2]};
    }

    // ── EVENT bodies ────────────────────────────────────────────────────────
    /** A decoded EVENT; only the fields for its tag are meaningful. */
    public static final class Event {
        public int tag;
        public int dim;
        public int x;
        public int y;
        public int z;
        public int oldPower;
        public int newPower;
        public String id = "";        // BLOCK_SET block id
        public int cmdId;             // CMD_TRIGGER id
        public String arg = "";       // CMD_TRIGGER argument
        public String objective = ""; // SCOREBOARD
        public String holder = "";
        public int value;

        public static Event redstone(int dim, int x, int y, int z, int oldPower, int newPower) {
            Event e = new Event();
            e.tag = EVT_REDSTONE;
            e.dim = dim;
            e.x = x;
            e.y = y;
            e.z = z;
            e.oldPower = oldPower;
            e.newPower = newPower;
            return e;
        }

        public static Event blockSet(int dim, int x, int y, int z, String id) {
            Event e = new Event();
            e.tag = EVT_BLOCK_SET;
            e.dim = dim;
            e.x = x;
            e.y = y;
            e.z = z;
            e.id = id;
            return e;
        }

        public static Event cmdTrigger(int cmdId, String arg) {
            Event e = new Event();
            e.tag = EVT_CMD_TRIGGER;
            e.cmdId = cmdId;
            e.arg = arg;
            return e;
        }

        public static Event scoreboard(String objective, String holder, int value) {
            Event e = new Event();
            e.tag = EVT_SCOREBOARD;
            e.objective = objective;
            e.holder = holder;
            e.value = value;
            return e;
        }
    }

    private static void putPos(byte[] b, int off, Event e) {
        b[off] = (byte) e.dim;
        b[off + 1] = (byte) (e.x >> 24);
        b[off + 2] = (byte) (e.x >> 16);
        b[off + 3] = (byte) (e.x >> 8);
        b[off + 4] = (byte) e.x;
        b[off + 5] = (byte) (e.y >> 8);
        b[off + 6] = (byte) e.y;
        b[off + 7] = (byte) (e.z >> 24);
        b[off + 8] = (byte) (e.z >> 16);
        b[off + 9] = (byte) (e.z >> 8);
        b[off + 10] = (byte) e.z;
    }

    private static void getPos(byte[] b, int off, Event e) {
        e.dim = b[off] & 0xFF;
        e.x = ((b[off + 1] & 0xFF) << 24) | ((b[off + 2] & 0xFF) << 16) | ((b[off + 3] & 0xFF) << 8) | (b[off + 4] & 0xFF);
        e.y = (short) (((b[off + 5] & 0xFF) << 8) | (b[off + 6] & 0xFF));
        e.z = ((b[off + 7] & 0xFF) << 24) | ((b[off + 8] & 0xFF) << 16) | ((b[off + 9] & 0xFF) << 8) | (b[off + 10] & 0xFF);
    }

    private static byte[] lstr(String s, int limit) {
        byte[] raw = s.getBytes(StandardCharsets.UTF_8);
        if (raw.length > limit) {
            throw new IllegalArgumentException("string " + raw.length + "B exceeds " + limit + "B");
        }
        byte[] out = new byte[1 + raw.length];
        out[0] = (byte) raw.length;
        System.arraycopy(raw, 0, out, 1, raw.length);
        return out;
    }

    private static byte[] cat(byte[]... parts) {
        int n = 0;
        for (byte[] p : parts) {
            n += p.length;
        }
        byte[] out = new byte[n];
        int off = 0;
        for (byte[] p : parts) {
            System.arraycopy(p, 0, out, off, p.length);
            off += p.length;
        }
        return out;
    }

    /** Event -> body (tag + fields), at most 124 bytes. Byte-deterministic. */
    public static byte[] pack(Event e) {
        byte[] body;
        switch (e.tag) {
            case EVT_REDSTONE: {
                body = new byte[14];
                body[0] = EVT_REDSTONE;
                putPos(body, 1, e);
                body[12] = (byte) e.oldPower;
                body[13] = (byte) e.newPower;
                break;
            }
            case EVT_BLOCK_SET: {
                byte[] head = new byte[12];
                head[0] = EVT_BLOCK_SET;
                putPos(head, 1, e);
                body = cat(head, lstr(e.id, MAX_EVENT - 13));
                break;
            }
            case EVT_CMD_TRIGGER: {
                byte[] head = {EVT_CMD_TRIGGER, (byte) (e.cmdId >> 8), (byte) e.cmdId};
                body = cat(head, lstr(e.arg, MAX_EVENT - 4));
                break;
            }
            case EVT_SCOREBOARD: {
                byte[] val = {(byte) (e.value >> 24), (byte) (e.value >> 16), (byte) (e.value >> 8), (byte) e.value};
                body = cat(new byte[] {EVT_SCOREBOARD}, lstr(e.objective, 255), lstr(e.holder, 255), val);
                break;
            }
            default:
                throw new IllegalArgumentException("unknown EVENT tag " + e.tag);
        }
        if (body.length > MAX_EVENT) {
            throw new IllegalArgumentException("EVENT body " + body.length + "B exceeds " + MAX_EVENT + "B");
        }
        return body;
    }

    /** Body -> Event; throws IllegalArgumentException on a malformed body. */
    public static Event unpack(byte[] b) {
        if (b.length == 0) {
            throw new IllegalArgumentException("empty EVENT body");
        }
        Event e = new Event();
        e.tag = b[0] & 0xFF;
        switch (e.tag) {
            case EVT_REDSTONE:
                if (b.length != 14) {
                    throw new IllegalArgumentException("REDSTONE body is 14 bytes");
                }
                getPos(b, 1, e);
                e.oldPower = b[12] & 0xFF;
                e.newPower = b[13] & 0xFF;
                return e;
            case EVT_BLOCK_SET: {
                if (b.length < 13 || b.length != 13 + (b[12] & 0xFF)) {
                    throw new IllegalArgumentException("bad BLOCK_SET length");
                }
                getPos(b, 1, e);
                e.id = new String(b, 13, b[12] & 0xFF, StandardCharsets.UTF_8);
                return e;
            }
            case EVT_CMD_TRIGGER: {
                if (b.length < 4 || b.length != 4 + (b[3] & 0xFF)) {
                    throw new IllegalArgumentException("bad CMD_TRIGGER length");
                }
                e.cmdId = ((b[1] & 0xFF) << 8) | (b[2] & 0xFF);
                e.arg = new String(b, 4, b[3] & 0xFF, StandardCharsets.UTF_8);
                return e;
            }
            case EVT_SCOREBOARD: {
                if (b.length < 2) {
                    throw new IllegalArgumentException("bad SCOREBOARD length");
                }
                int ol = b[1] & 0xFF;
                int p = 2 + ol;
                if (b.length < p + 1) {
                    throw new IllegalArgumentException("bad SCOREBOARD length");
                }
                int hl = b[p] & 0xFF;
                int q = p + 1 + hl;
                if (b.length != q + 4) {
                    throw new IllegalArgumentException("bad SCOREBOARD length");
                }
                e.objective = new String(b, 2, ol, StandardCharsets.UTF_8);
                e.holder = new String(b, p + 1, hl, StandardCharsets.UTF_8);
                e.value = ((b[q] & 0xFF) << 24) | ((b[q + 1] & 0xFF) << 16) | ((b[q + 2] & 0xFF) << 8) | (b[q + 3] & 0xFF);
                return e;
            }
            default:
                throw new IllegalArgumentException("unknown EVENT tag " + e.tag);
        }
    }

    /** One Minecraft event -> DCF-Game EVENT frames on mc-world, FLAG_RELIABLE. */
    public static List<byte[]> packetize(Event e, int packetId, int tsUs, int src) {
        return Game.packetize(Game.GMSG_EVENT, pack(e), packetId, tsUs, src, CH_WORLD, Game.FLAG_RELIABLE);
    }
}
