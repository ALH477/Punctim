// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.io.IOException;
import java.util.List;
import java.util.Map;

import static com.demod.dcf.JsonLite.arr;
import static com.demod.dcf.JsonLite.frames;
import static com.demod.dcf.JsonLite.hexes;
import static com.demod.dcf.JsonLite.num;
import static com.demod.dcf.JsonLite.obj;
import static com.demod.dcf.JsonLite.str;
import static com.demod.dcf.JsonLite.unhex;

/** Certifies {@link Text} against Documentation/text_vectors.json. Dependency-free. */
public final class TextCertify {
    private static int fails;

    private TextCertify() {
    }

    public static void main(String[] args) throws IOException {
        Map<String, Object> v = JsonLite.read(JsonLite.resolve(args, "text_vectors.json"));
        Map<String, Object> c = obj(v.get("constants"));
        check(num(c.get("frag_bits")) == Text.FRAG_BITS && num(c.get("max_frags")) == Text.MAX_FRAGS
                && num(c.get("max_payload")) == Text.MAX_PAYLOAD && num(c.get("max_packet_id")) == Text.MAX_PACKET_ID
                && num(c.get("flag_agent")) == Text.FLAG_AGENT && num(c.get("flag_more")) == Text.FLAG_MORE
                && num(c.get("flag_reliable")) == Text.FLAG_RELIABLE, "constants");

        Map<String, Object> ex = obj(obj(v.get("anchors")).get("exampleTextMessage"));
        check(Frame.hex(Text.packetize(str(ex.get("text")), 0, 0, 0, 0, 0).get(1))
                        .equals(Frame.hex(Text.packetize(unhex(str(ex.get("payload"))), 0, 0, 0, 0, 0).get(1)))
                && Text.channelId("duet") == 0xEED7 && num(ex.get("dst")) == 0xEED7, "anchor: text == payload, duet");

        int n = 0;
        for (Object o : arr(v.get("framing"))) {
            Map<String, Object> k = obj(o);
            List<byte[]> got = Text.packetize(unhex(str(k.get("payload"))), (int) num(k.get("packet_id")),
                    (int) num(k.get("ts_us")), (int) num(k.get("src")), (int) num(k.get("dst")), (int) num(k.get("flags")));
            check(hexes(got).equals(JsonLite.strs(k.get("frames"))), "framing case " + n);
            Text.Reassembler r = new Text.Reassembler();
            Text.Message m = null;
            for (byte[] f : got) {
                for (Text.Message q : r.push(f)) {
                    m = q;
                }
            }
            check(m != null && m.packetId == num(k.get("packet_id")) && m.tsUs == num(k.get("ts_us"))
                    && m.src == num(k.get("src")) && m.dst == num(k.get("dst")) && m.flags == num(k.get("flags"))
                    && Frame.hex(m.payload).equals(str(k.get("payload"))) && r.finalizeLost().isEmpty(),
                    "framing round trip " + n);
            n++;
        }
        System.out.println("  framing: " + n + " cases");

        n = 0;
        for (Object o : arr(v.get("reassembly"))) {
            Map<String, Object> k = obj(o);
            Text.Reassembler r = new Text.Reassembler();
            List<Object> want = arr(k.get("messages"));
            int got = 0;
            boolean ok = true;
            for (byte[] f : frames(k.get("input_frames"))) {
                for (Text.Message m : r.push(f)) {
                    if (got >= want.size()) {
                        ok = false;
                        break;
                    }
                    Map<String, Object> w = obj(want.get(got++));
                    ok &= m.packetId == num(w.get("packet_id")) && m.tsUs == num(w.get("ts_us"))
                            && m.src == num(w.get("src")) && m.dst == num(w.get("dst")) && m.flags == num(w.get("flags"))
                            && Frame.hex(m.payload).equals(str(w.get("payload"))) && m.text().equals(str(w.get("text")));
                }
            }
            List<Integer> lost = r.finalizeLost();
            List<Object> wantLost = arr(k.get("lost"));
            ok &= got == want.size() && lost.size() == wantLost.size();
            for (int i = 0; ok && i < lost.size(); i++) {
                ok &= lost.get(i) == num(wantLost.get(i));
            }
            check(ok, "reassembly " + str(k.get("name")));
            n++;
        }
        System.out.println("  reassembly: " + n + " cases");

        if (fails != 0) {
            System.out.println("Text codec: FAILED (" + fails + " mismatches)");
            System.exit(1);
        }
        System.out.println("Text codec: CERTIFIED against text_vectors.json");
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            System.err.println("    mismatch: " + what);
            fails++;
        }
    }
}
