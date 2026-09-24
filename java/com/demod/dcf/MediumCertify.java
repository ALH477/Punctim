// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Certifies {@link Medium} (the Tier-B digital medium codecs: stream, hex, udp_proto,
 * udp_bare, l2eth) byte-for-byte against the cross-language golden vectors in
 * Documentation/medium_vectors.json. Dependency-free (a small JSON reader below) so it
 * runs with just a JDK.
 *
 * <p>Usage: {@code java com.demod.dcf.MediumCertify [path/to/medium_vectors.json]}
 */
public final class MediumCertify {

    private static int fails;

    public static void main(String[] args) throws IOException {
        String text = new String(Files.readAllBytes(resolve(args)), StandardCharsets.UTF_8);
        Map<String, Object> v = obj(new Json(text).value());
        Map<String, Object> an = obj(v.get("anchors"));
        Map<String, Object> fam = obj(v.get("families"));

        // ── anchors ──────────────────────────────────────────────────────────────────
        String filler = Frame.hex(Medium.l2Filler());
        check(Frame.crc16("123456789".getBytes(StandardCharsets.US_ASCII)) == num(an.get("crc_123456789"))
                        && Frame.crc16(new byte[15]) == num(an.get("crc_zero15"))
                        && num(an.get("crc_123456789")) == 0x29B1 && num(an.get("crc_zero15")) == 0x4EC3
                        && num(an.get("frame_len")) == Medium.FRAME_LEN
                        && num(an.get("proto_header_len")) == Medium.PROTO_HEADER_LEN
                        && num(an.get("msg_frame")) == Medium.MSG_FRAME
                        && num(an.get("super_len")) == SuperPack.SUPER_LEN
                        && num(an.get("l2_hdr")) == Medium.L2_HDR
                        && filler.equals(an.get("l2_filler")),
                "anchors: CRC 0x29B1/0x4EC3, frame 17, proto header 17, MSG_FRAME 12, SuperPack 32, "
                        + "l2 hdr 2, l2 filler");

        // ── basis frames ─────────────────────────────────────────────────────────────
        {
            List<Object> basis = arr(v.get("basis"));
            boolean ok = !basis.isEmpty();
            for (Object o : basis) {
                Map<String, Object> b = obj(o);
                Frame f = new Frame();
                f.type = (int) num(b.get("type"));
                f.seq = (int) num(b.get("seq"));
                f.src = (int) num(b.get("src"));
                f.dst = (int) num(b.get("dst"));
                f.payload = unhex(str(b.get("payload")));
                f.tsUs = (int) num(b.get("ts"));
                byte[] e = f.encode();
                if (!Frame.hex(e).equals(str(b.get("hex"))) || !Medium.gate(e)) {
                    ok = false;
                    note("basis " + str(b.get("hex")));
                }
            }
            check(ok, basis.size() + " basis frames encode byte-identically + pass the gate");
        }

        // ── stream ───────────────────────────────────────────────────────────────────
        {
            List<Object> cases = arr(obj(fam.get("stream")).get("cases"));
            boolean decOk = !cases.isEmpty();
            boolean chunkOk = true;
            boolean rtOk = true;
            for (Object o : cases) {
                Map<String, Object> c = obj(o);
                String name = str(c.get("name"));
                byte[] in = unhex(str(c.get("input")));
                List<String> want = strs(c.get("frames"));
                long skipped = num(c.get("skipped_bytes"));
                long tail = num(c.get("tail_bytes"));
                Medium.StreamResult r = Medium.streamDecode(in);
                if (!hexes(r.frames).equals(want) || r.skippedBytes != skipped || r.tailBytes != tail) {
                    decOk = false;
                    note("stream " + name);
                }
                for (byte[] f : r.frames) {
                    if (!Medium.gate(f)) {
                        decOk = false;
                    }
                }
                // chunking-invariance law: every chunk size gives the same frames/skipped/tail
                for (int step = 1; step < 36; step++) {
                    Medium.StreamScanner sc = new Medium.StreamScanner();
                    List<byte[]> got = new ArrayList<>();
                    for (int i = 0; i < in.length; i += step) {
                        got.addAll(sc.feed(in, i, Math.min(step, in.length - i)));
                    }
                    if (!hexes(got).equals(want) || sc.skippedBytes() != skipped
                            || sc.framesOut() != want.size() || sc.flush().length != tail) {
                        chunkOk = false;
                        note("stream chunk " + name + " step " + step);
                    }
                }
                // lossless: decode(encode(frames)) == frames, nothing skipped, no tail
                List<byte[]> fs = frames(c.get("frames"));
                byte[] enc = Medium.streamEncode(fs);
                Medium.StreamResult rt = Medium.streamDecode(enc);
                if (!hexes(rt.frames).equals(want) || rt.skippedBytes != 0 || rt.tailBytes != 0) {
                    rtOk = false;
                }
                if ((name.equals("aligned_1") || name.equals("aligned_3") || name.equals("all_six"))
                        && !Frame.hex(enc).equals(str(c.get("input")))) {
                    rtOk = false;
                }
            }
            check(decOk, cases.size() + " stream cases decode byte-identically (frames, skipped_bytes, "
                    + "tail_bytes); every frame passes the gate");
            check(chunkOk, "stream scanner chunk-invariant (1..35-byte chunks)");
            check(rtOk, "stream encode lossless");
        }

        // ── hex ──────────────────────────────────────────────────────────────────────
        {
            List<Object> cases = arr(obj(fam.get("hex")).get("cases"));
            boolean encOk = !cases.isEmpty();
            boolean decOk = true;
            boolean fixOk = true;
            for (Object o : cases) {
                Map<String, Object> c = obj(o);
                String name = str(c.get("name"));
                if (!Medium.hexEncode(frames(c.get("frames"))).equals(str(c.get("text")))) {
                    encOk = false;
                    note("hex encode " + name);
                }
                Medium.HexResult r = Medium.hexDecode(str(c.get("decode_input")));
                if (!hexes(r.frames).equals(strs(c.get("decoded"))) || r.badLines != num(c.get("bad_lines"))) {
                    decOk = false;
                    note("hex decode " + name);
                }
                Medium.HexResult canon = Medium.hexDecode(str(c.get("text")));
                if (!hexes(canon.frames).equals(strs(c.get("decoded"))) || canon.badLines != 0) {
                    fixOk = false;
                }
            }
            check(encOk, cases.size() + " hex cases encode byte-identically");
            check(decOk, cases.size() + " hex cases decode (CRLF/uppercase/comments/blank; bad_lines; ungated)");
            check(fixOk, "hex canonical text is a decode fixed point");
        }

        // ── udp_proto ────────────────────────────────────────────────────────────────
        {
            Map<String, Object> up = obj(fam.get("udp_proto"));
            Map<String, Object> types = obj(up.get("types"));
            boolean regOk = num(up.get("header_len")) == Medium.PROTO_HEADER_LEN
                    && num(up.get("msg_frame")) == Medium.MSG_FRAME
                    && types.size() == Medium.MSG_TYPES.size();
            for (Map.Entry<String, Integer> e : Medium.MSG_TYPES.entrySet()) {
                if (!types.containsKey(e.getKey()) || num(types.get(e.getKey())) != e.getValue()) {
                    regOk = false;
                }
            }
            check(regOk, "udp_proto header 17, msg_type registry 1..12 (FRAME = 12)");

            List<Object> cases = arr(up.get("cases"));
            boolean encOk = !cases.isEmpty();
            boolean decOk = true;
            boolean accOk = true;
            String golden = null;
            for (Object o : cases) {
                Map<String, Object> c = obj(o);
                String name = str(c.get("name"));
                int type = (int) num(c.get("type"));
                long seq = num(c.get("seq"));
                // ts may exceed 2^53 (and in principle 2^63): parse the exact hex form as u64
                long ts = Long.parseUnsignedLong(str(c.get("ts_hex")), 16);
                if (!Long.toUnsignedString(ts).equals(c.get("ts").toString())) {
                    encOk = false;
                    note("ts/ts_hex disagree " + name);
                }
                byte[] pl = unhex(str(c.get("payload")));
                String dgHex = str(c.get("datagram"));
                if (!Frame.hex(Medium.protoEncode(type, seq, ts, pl)).equals(dgHex)) {
                    encOk = false;
                    note("proto encode " + name);
                }
                byte[] dg = unhex(dgHex);
                try {
                    Medium.ProtoMessage m = Medium.protoDecode(dg);
                    if (m.type != type || m.seq != seq || m.ts != ts || !Arrays.equals(m.payload, pl)) {
                        decOk = false;
                        note("proto decode " + name);
                    }
                } catch (IllegalArgumentException e) {
                    decOk = false;
                    note("proto decode threw " + name);
                }
                byte[] fr = Medium.protoFrameDecode(dg);
                boolean accept = (Boolean) c.get("accept_as_frame");
                if ((fr != null) != accept) {
                    accOk = false;
                    note("accept " + name);
                }
                if (fr != null && (!Frame.hex(fr).equals(str(c.get("payload")))
                        || !Frame.hex(Medium.protoFrameEncode(fr, seq, ts)).equals(dgHex))) {
                    accOk = false;
                }
                if (name.equals("go_golden")) {
                    golden = dgHex;
                }
            }
            check(encOk, cases.size() + " udp_proto datagrams encode byte-identically (u64 ts incl. > 2^53)");
            check(decOk, cases.size() + " udp_proto datagrams decode to (type, seq, ts, payload)");
            check(accOk, "accept_as_frame iff type == 12 && len == 17; frame round-trips to 34 B");

            boolean guardOk = golden != null;
            if (golden != null) {
                byte[] g = unhex(golden);
                for (byte[] bad : new byte[][] {new byte[16], Arrays.copyOf(g, g.length - 1)}) {
                    boolean rejected = false;
                    try {
                        Medium.protoDecode(bad);
                    } catch (IllegalArgumentException e) {
                        rejected = true;
                    }
                    if (!rejected || Medium.protoFrameDecode(bad) != null) {
                        guardOk = false;
                    }
                }
            }
            check(guardOk, "udp_proto rejects a short header and a payload_len overrun");
        }

        // ── udp_bare ─────────────────────────────────────────────────────────────────
        {
            List<Object> cases = arr(obj(fam.get("udp_bare")).get("cases"));
            boolean encOk = !cases.isEmpty();
            boolean decOk = true;
            boolean rejOk = Medium.bareDecode(new byte[33]).isEmpty() && Medium.bareDecode(new byte[16]).isEmpty();
            for (Object o : cases) {
                Map<String, Object> c = obj(o);
                String name = str(c.get("name"));
                List<byte[]> fs = frames(c.get("frames"));
                if (!hexes(Medium.bareEncode(fs)).equals(strs(c.get("datagrams")))) {
                    encOk = false;
                    note("bare encode " + name);
                }
                List<byte[]> back = new ArrayList<>();
                for (String d : strs(c.get("datagrams"))) {
                    back.addAll(Medium.bareDecode(unhex(d)));
                }
                if (!hexes(back).equals(strs(c.get("frames")))) {
                    decOk = false;
                    note("bare decode " + name);
                }
                if (!hexes(Medium.bareEncode(fs, false)).equals(strs(c.get("frames")))) {
                    decOk = false;
                }
                if (name.equals("frames_2")) {
                    byte[] tam = unhex(strs(c.get("datagrams")).get(0));
                    tam[7] ^= 1;
                    if (!Medium.bareDecode(tam).isEmpty()) {
                        rejOk = false;
                    }
                }
            }
            check(encOk, cases.size() + " udp_bare cases encode byte-identically (pairs -> 32-B SuperPack, "
                    + "lone -> 17 B)");
            check(decOk, "udp_bare decode lossless; pair=false sends every frame raw");
            check(rejOk, "udp_bare rejects 33-B / 16-B datagrams and a tampered SuperPack");
        }

        // ── l2eth ────────────────────────────────────────────────────────────────────
        {
            Map<String, Object> l2 = obj(fam.get("l2eth"));
            check(num(l2.get("hdr")) == Medium.L2_HDR && filler.equals(l2.get("filler"))
                            && Medium.gate(Medium.l2Filler()),
                    "l2eth hdr 2; filler = encode(DATA, 0, 0, 0, 00000000, 0) = " + filler);
            List<Object> cases = arr(l2.get("cases"));
            boolean encOk = !cases.isEmpty();
            boolean decOk = true;
            boolean truncOk = true;
            for (Object o : cases) {
                Map<String, Object> c = obj(o);
                String name = str(c.get("name"));
                List<byte[]> fs = frames(c.get("frames"));
                List<String> want = strs(c.get("frames"));
                if (!Frame.hex(Medium.l2Batch(fs)).equals(str(c.get("payload")))) {
                    encOk = false;
                    note("l2 batch " + name);
                }
                byte[] pl = unhex(str(c.get("payload")));
                try {
                    if (!hexes(Medium.l2Unbatch(pl)).equals(want)
                            || !hexes(Medium.l2Unbatch(Arrays.copyOf(pl, pl.length + 7))).equals(want)) {
                        decOk = false;
                        note("l2 unbatch " + name);
                    }
                } catch (IllegalArgumentException e) {
                    decOk = false;
                    note("l2 unbatch threw " + name);
                }
                if (fs.size() % 2 == 1) {
                    byte[][] last = SuperPack.unpack(Arrays.copyOfRange(pl, pl.length - 32, pl.length));
                    if (!Frame.hex(last[1]).equals(filler)) {
                        decOk = false;
                    }
                }
                try {
                    Medium.l2Unbatch(Arrays.copyOf(pl, pl.length - 1));
                    truncOk = false;
                } catch (IllegalArgumentException e) {
                    // expected
                }
            }
            try {
                Medium.l2Unbatch(new byte[1]);
                truncOk = false;
            } catch (IllegalArgumentException e) {
                // expected
            }
            check(encOk, cases.size() + " l2eth payloads batch byte-identically ([n u16][SuperPack*ceil(n/2)])");
            check(decOk, "l2eth unbatch lossless; filler dropped via n; min-size padding ignored");
            check(truncOk, "l2eth rejects a truncated / short batch");
            check(Medium.l2Capacity(1500) == 92 && Medium.l2Capacity(9000) == 562 && Medium.l2Capacity(1) == 0,
                    "l2Capacity(1500) = 92, l2Capacity(9000) = 562");
        }

        if (fails > 0) {
            System.err.println("\n" + fails + " CHECK(S) FAILED");
            System.exit(1);
        }
        System.out.println("\nALL MEDIUM VECTORS HOLD — Java DCF-Medium (stream/hex/udp_proto/udp_bare/l2eth) "
                + "is cemented.");
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────
    private static void check(boolean cond, String label) {
        System.out.println((cond ? "  PASS  " : "  FAIL  ") + label);
        if (!cond) {
            fails++;
        }
    }

    private static void note(String what) {
        System.err.println("    mismatch: " + what);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> obj(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    private static List<Object> arr(Object o) {
        return (List<Object>) o;
    }

    private static String str(Object o) {
        return (String) o;
    }

    /** A JSON integer that fits a long (every vector field except a u64 ts does). */
    private static long num(Object o) {
        return Long.parseLong(o.toString());
    }

    private static List<String> strs(Object o) {
        List<String> out = new ArrayList<>();
        for (Object x : arr(o)) {
            out.add(str(x));
        }
        return out;
    }

    private static List<byte[]> frames(Object o) {
        List<byte[]> out = new ArrayList<>();
        for (String h : strs(o)) {
            out.add(unhex(h));
        }
        return out;
    }

    private static List<String> hexes(List<byte[]> bs) {
        List<String> out = new ArrayList<>();
        for (byte[] b : bs) {
            out.add(Frame.hex(b));
        }
        return out;
    }

    private static byte[] unhex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    private static Path resolve(String[] args) {
        if (args.length > 0) {
            return Path.of(args[0]);
        }
        String[] candidates = {
            "Documentation/medium_vectors.json",
            "../Documentation/medium_vectors.json",
            "../../Documentation/medium_vectors.json",
            "python/MCP/medium_vectors.json",
        };
        List<Path> tried = new ArrayList<>();
        for (String c : candidates) {
            Path p = Path.of(c);
            if (Files.exists(p)) {
                return p;
            }
            tried.add(p);
        }
        throw new IllegalStateException("medium_vectors.json not found; tried " + tried);
    }

    /**
     * A minimal JSON reader: objects (LinkedHashMap), arrays (ArrayList), strings (with
     * escapes), numbers (kept as their exact source text, so a u64 never loses precision),
     * true/false (Boolean), null.
     */
    private static final class Json {
        private final String t;
        private int p;

        Json(String text) {
            this.t = text;
        }

        private IllegalStateException fail(String m) {
            return new IllegalStateException("json: " + m + " at " + p);
        }

        private void ws() {
            while (p < t.length() && " \t\r\n".indexOf(t.charAt(p)) >= 0) {
                p++;
            }
        }

        private boolean eat(char c) {
            ws();
            if (p < t.length() && t.charAt(p) == c) {
                p++;
                return true;
            }
            return false;
        }

        private void need(char c) {
            if (!eat(c)) {
                throw fail("expected '" + c + "'");
            }
        }

        Object value() {
            ws();
            if (p >= t.length()) {
                throw fail("eof");
            }
            char c = t.charAt(p);
            if (c == '{') {
                p++;
                Map<String, Object> m = new LinkedHashMap<>();
                if (eat('}')) {
                    return m;
                }
                do {
                    ws();
                    String k = string();
                    need(':');
                    m.put(k, value());
                } while (eat(','));
                need('}');
                return m;
            }
            if (c == '[') {
                p++;
                List<Object> a = new ArrayList<>();
                if (eat(']')) {
                    return a;
                }
                do {
                    a.add(value());
                } while (eat(','));
                need(']');
                return a;
            }
            if (c == '"') {
                return string();
            }
            if (t.startsWith("true", p)) {
                p += 4;
                return Boolean.TRUE;
            }
            if (t.startsWith("false", p)) {
                p += 5;
                return Boolean.FALSE;
            }
            if (t.startsWith("null", p)) {
                p += 4;
                return null;
            }
            int s = p;
            while (p < t.length() && "+-0123456789.eE".indexOf(t.charAt(p)) >= 0) {
                p++;
            }
            if (s == p) {
                throw fail("bad value");
            }
            return new Num(t.substring(s, p));
        }

        private String string() {
            need('"');
            StringBuilder sb = new StringBuilder();
            while (p < t.length() && t.charAt(p) != '"') {
                char c = t.charAt(p++);
                if (c != '\\') {
                    sb.append(c);
                    continue;
                }
                if (p >= t.length()) {
                    throw fail("bad escape");
                }
                char e = t.charAt(p++);
                switch (e) {
                    case '"': sb.append('"'); break;
                    case '\\': sb.append('\\'); break;
                    case '/': sb.append('/'); break;
                    case 'b': sb.append('\b'); break;
                    case 'f': sb.append('\f'); break;
                    case 'n': sb.append('\n'); break;
                    case 'r': sb.append('\r'); break;
                    case 't': sb.append('\t'); break;
                    case 'u':
                        if (p + 4 > t.length()) {
                            throw fail("short \\u escape");
                        }
                        sb.append((char) Integer.parseInt(t.substring(p, p + 4), 16));
                        p += 4;
                        break;
                    default:
                        throw fail("bad escape");
                }
            }
            need('"');
            return sb.toString();
        }
    }

    /** A JSON number kept as its exact source text. */
    private static final class Num {
        private final String text;

        Num(String text) {
            this.text = text;
        }

        @Override
        public String toString() {
            return text;
        }
    }
}
