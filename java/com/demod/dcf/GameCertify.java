// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.io.IOException;
import java.util.List;
import java.util.Map;

import static com.demod.dcf.JsonLite.arr;
import static com.demod.dcf.JsonLite.dbl;
import static com.demod.dcf.JsonLite.frames;
import static com.demod.dcf.JsonLite.hexes;
import static com.demod.dcf.JsonLite.num;
import static com.demod.dcf.JsonLite.obj;
import static com.demod.dcf.JsonLite.str;
import static com.demod.dcf.JsonLite.unhex;

/** Certifies {@link Game} against Documentation/game_vectors.json. Dependency-free. */
public final class GameCertify {
    private static int fails;

    private GameCertify() {
    }

    public static void main(String[] args) throws IOException {
        Map<String, Object> v = JsonLite.read(JsonLite.resolve(args, "game_vectors.json"));
        Map<String, Object> c = obj(v.get("constants"));
        check(num(c.get("frag_bits")) == Game.FRAG_BITS && num(c.get("max_frags")) == Game.MAX_FRAGS
                && num(c.get("max_payload")) == Game.MAX_PAYLOAD
                && num(c.get("max_packet_id")) == Game.MAX_PACKET_ID
                && num(c.get("frame_type_data")) == Game.FDATA, "constants");

        int n = 0;
        for (Object o : arr(v.get("framing"))) {
            Map<String, Object> k = obj(o);
            List<byte[]> got = Game.packetize((int) num(k.get("msg_type_id")), unhex(str(k.get("payload"))),
                    (int) num(k.get("packet_id")), (int) num(k.get("ts_us")), (int) num(k.get("src")),
                    (int) num(k.get("dst")), (int) num(k.get("flags")));
            check(hexes(got).equals(JsonLite.strs(k.get("frames"))), "framing case " + n);
            Game.Reassembler r = new Game.Reassembler();
            Game.Packet p = null;
            for (byte[] f : got) {
                for (Game.Packet q : r.push(f)) {
                    p = q;
                }
            }
            check(p != null && p.packetId == num(k.get("packet_id")) && p.tsUs == num(k.get("ts_us"))
                    && p.msgTypeId == num(k.get("msg_type_id")) && p.flags == num(k.get("flags"))
                    && Frame.hex(p.payload).equals(str(k.get("payload"))) && r.finalizeLost().isEmpty(),
                    "framing round trip " + n);
            n++;
        }
        System.out.println("  framing: " + n + " cases");

        n = 0;
        for (Object o : arr(v.get("reassembly"))) {
            Map<String, Object> k = obj(o);
            Game.Reassembler r = new Game.Reassembler();
            List<Object> want = arr(k.get("packets"));
            int got = 0;
            boolean ok = true;
            for (byte[] f : frames(k.get("input_frames"))) {
                for (Game.Packet p : r.push(f)) {
                    if (got >= want.size()) {
                        ok = false;
                        break;
                    }
                    Map<String, Object> w = obj(want.get(got++));
                    ok &= p.packetId == num(w.get("packet_id")) && p.tsUs == num(w.get("ts_us"))
                            && p.msgTypeId == num(w.get("msg_type_id")) && p.flags == num(w.get("flags"))
                            && Frame.hex(p.payload).equals(str(w.get("payload")));
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

        n = 0;
        for (Object o : arr(v.get("snapshot_roundtrip"))) {
            Map<String, Object> k = obj(o);
            byte[] b = unhex(str(k.get("bytes")));
            Map<String, Object> f = obj(k.get("fields"));
            double[] u = Game.snapshotUnpack(b);
            String[] names = {"x", "y", "z", "vx", "vy", "vz"};
            boolean ok = u[6] == num(f.get("yaw"));
            for (int i = 0; i < 6; i++) {
                ok &= u[i] == dbl(f.get(names[i]));
            }
            double[] xyz = {u[0], u[1], u[2], u[3], u[4], u[5]};
            ok &= Frame.hex(Game.snapshotPack(xyz, (int) u[6])).equals(str(k.get("bytes")));
            check(ok, "snapshot " + n++);
        }
        for (Object o : arr(v.get("input_roundtrip"))) {
            Map<String, Object> k = obj(o);
            Map<String, Object> f = obj(k.get("fields"));
            long[] u = Game.inputUnpack(unhex(str(k.get("bytes"))));
            check(u[0] == num(f.get("tick")) && u[1] == num(f.get("buttons"))
                    && Frame.hex(Game.inputPack(u[0], (int) u[1])).equals(str(k.get("bytes"))), "input " + n++);
        }
        for (Object o : arr(v.get("join_roundtrip"))) {
            Map<String, Object> k = obj(o);
            Map<String, Object> f = obj(k.get("fields"));
            byte[] b = unhex(str(k.get("bytes")));
            check(Game.joinPlayerId(b) == num(f.get("player_id")) && Game.joinName(b).equals(str(f.get("name")))
                    && Frame.hex(Game.joinPack((int) num(f.get("player_id")), str(f.get("name")))).equals(str(k.get("bytes"))),
                    "join " + n++);
        }
        System.out.println("  bodies: " + n + " cases");

        if (fails != 0) {
            System.out.println("Game codec: FAILED (" + fails + " mismatches)");
            System.exit(1);
        }
        System.out.println("Game codec: CERTIFIED against game_vectors.json");
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            System.err.println("    mismatch: " + what);
            fails++;
        }
    }
}
