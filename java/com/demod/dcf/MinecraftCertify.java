// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.io.IOException;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static com.demod.dcf.JsonLite.arr;
import static com.demod.dcf.JsonLite.hexes;
import static com.demod.dcf.JsonLite.ints;
import static com.demod.dcf.JsonLite.num;
import static com.demod.dcf.JsonLite.obj;
import static com.demod.dcf.JsonLite.str;
import static com.demod.dcf.JsonLite.unhex;

/** Certifies {@link McEvent} against Documentation/minecraft_vectors.json. Dependency-free. */
public final class MinecraftCertify {
    private static int fails;

    private MinecraftCertify() {
    }

    public static void main(String[] args) throws IOException {
        Map<String, Object> v = JsonLite.read(JsonLite.resolve(args, "minecraft_vectors.json"));
        Map<String, Object> c = obj(v.get("constants"));
        check(num(c.get("nibbles")) == McEvent.NIBBLES && num(c.get("words")) == McEvent.WORDS
                && num(c.get("barrel_capacity")) == McEvent.BARREL_CAPACITY
                && num(c.get("barrel_slots")) == McEvent.BARREL_SLOTS && num(c.get("lane_pitch")) == McEvent.LANE_PITCH
                && num(c.get("max_event")) == McEvent.MAX_EVENT && num(c.get("gmsg_event")) == Game.GMSG_EVENT
                && num(c.get("flag_reliable")) == Game.FLAG_RELIABLE
                && Arrays.equals(ints(c.get("word_bytes")), McEvent.WORD_BYTES)
                && str(c.get("obj_reg")).equals(McEvent.OBJ_REG) && str(c.get("obj_ctl")).equals(McEvent.OBJ_CTL),
                "constants");

        Map<String, Object> ch = obj(v.get("channels"));
        check(num(ch.get("mc-world")) == McEvent.CH_WORLD && num(ch.get("mc-chat")) == McEvent.CH_CHAT
                && num(ch.get("duet")) == Text.channelId("duet") && McEvent.CH_WORLD == 0xD952
                && McEvent.CH_CHAT == 0xE624, "channels: mc-world 0xD952, mc-chat 0xE624, duet");

        int[] table = ints(v.get("signal_table"));
        boolean ok = table.length == 16;
        for (int s = 0; ok && s < 16; s++) {
            ok &= McEvent.signalToItems(s) == table[s] && McEvent.itemsToSignal(table[s]) == s;
        }
        check(ok, "signal table");

        Map<String, Object> an = obj(v.get("anchors"));
        byte[] g = unhex(str(an.get("golden_frame")));
        check(Arrays.equals(McEvent.frameToNibbles(g), ints(an.get("golden_nibbles")))
                && Arrays.equals(McEvent.frameToWords(g), ints(an.get("golden_words")))
                && Arrays.equals(McEvent.frameToItems(g), ints(an.get("golden_items")))
                && Frame.crc16("123456789".getBytes()) == num(an.get("crc_123456789")), "anchors: golden frame");

        int n = 0;
        for (Object o : arr(v.get("register"))) {
            Map<String, Object> k = obj(o);
            byte[] f = unhex(str(k.get("frame")));
            int[] nib = ints(k.get("nibbles"));
            int[] w = ints(k.get("words"));
            int[] items = ints(k.get("items"));
            boolean r = Arrays.equals(McEvent.frameToNibbles(f), nib) && Arrays.equals(McEvent.frameToWords(f), w)
                    && Arrays.equals(McEvent.frameToItems(f), items) && Arrays.equals(McEvent.nibblesToFrame(nib), f)
                    && Arrays.equals(McEvent.wordsToFrame(w), f);
            StringBuilder line = new StringBuilder("[CHAT] DCF TX");
            for (int x : w) {
                line.append(' ').append(x);
            }
            r &= Arrays.equals(McEvent.parseChatTx(line.toString()), f);
            check(r, "register case " + n++);
        }
        System.out.println("  register: " + n + " cases");

        n = 0;
        for (Object o : arr(v.get("events"))) {
            Map<String, Object> k = obj(o);
            Map<String, Object> e = obj(k.get("event"));
            McEvent.Event ev = fromJson(e);
            byte[] body = unhex(str(k.get("body")));
            boolean r = Frame.hex(McEvent.pack(ev)).equals(str(k.get("body")));
            McEvent.Event back = McEvent.unpack(body);
            r &= Frame.hex(McEvent.pack(back)).equals(str(k.get("body"))) && same(back, ev);
            List<byte[]> fr = McEvent.packetize(ev, (int) num(k.get("packet_id")), (int) num(k.get("ts_us")),
                    (int) num(k.get("src")));
            r &= hexes(fr).equals(JsonLite.strs(k.get("frames")));
            Game.Reassembler ra = new Game.Reassembler();
            Game.Packet p = null;
            for (byte[] f : fr) {
                for (Game.Packet q : ra.push(f)) {
                    p = q;
                }
            }
            r &= p != null && p.msgTypeId == Game.GMSG_EVENT && p.flags == Game.FLAG_RELIABLE
                    && Arrays.equals(p.payload, body) && Frame.decode(fr.get(0)).dst == McEvent.CH_WORLD;
            check(r, "event " + str(k.get("name")));
            n++;
        }
        System.out.println("  events: " + n + " cases");

        Map<String, Object> geo = obj(v.get("geometry"));
        int[] origin = ints(geo.get("origin"));
        ok = true;
        for (Object o : arr(geo.get("lanes"))) {
            Map<String, Object> l = obj(o);
            int i = (int) num(l.get("i"));
            String[] kinds = {"out", "cmp", "mid", "in"};
            for (int kind = 0; kind < 4; kind++) {
                ok &= Arrays.equals(McEvent.lanePos(origin, i, kind), ints(l.get(kinds[kind])));
            }
        }
        Map<String, Object> ctrl = obj(geo.get("ctrl"));
        ok &= Arrays.equals(McEvent.strobeIn(origin), add(origin, ints(ctrl.get("strobe_in"))))
                && Arrays.equals(McEvent.validOut(origin), add(origin, ints(ctrl.get("valid_out"))))
                && Arrays.equals(McEvent.ackOut(origin), add(origin, ints(ctrl.get("ack_out"))))
                && Arrays.equals(McEvent.nakOut(origin), add(origin, ints(ctrl.get("nak_out"))));
        check(ok, "geometry");

        if (fails != 0) {
            System.out.println("Minecraft register/events: FAILED (" + fails + " mismatches)");
            System.exit(1);
        }
        System.out.println("Minecraft register/events: CERTIFIED against minecraft_vectors.json");
    }

    private static int[] add(int[] o, int[] d) {
        return new int[] {o[0] + d[0], o[1] + d[1], o[2] + d[2]};
    }

    private static McEvent.Event fromJson(Map<String, Object> e) {
        int tag = (int) num(e.get("tag"));
        switch (tag) {
            case McEvent.EVT_REDSTONE:
                return McEvent.Event.redstone((int) num(e.get("dim")), (int) num(e.get("x")), (int) num(e.get("y")),
                        (int) num(e.get("z")), (int) num(e.get("old")), (int) num(e.get("new")));
            case McEvent.EVT_BLOCK_SET:
                return McEvent.Event.blockSet((int) num(e.get("dim")), (int) num(e.get("x")), (int) num(e.get("y")),
                        (int) num(e.get("z")), str(e.get("id")));
            case McEvent.EVT_CMD_TRIGGER:
                return McEvent.Event.cmdTrigger((int) num(e.get("id")), str(e.get("arg")));
            case McEvent.EVT_SCOREBOARD:
                return McEvent.Event.scoreboard(str(e.get("objective")), str(e.get("holder")), (int) num(e.get("value")));
            default:
                throw new IllegalStateException("tag " + tag);
        }
    }

    private static boolean same(McEvent.Event a, McEvent.Event b) {
        return a.tag == b.tag && a.dim == b.dim && a.x == b.x && a.y == b.y && a.z == b.z
                && a.oldPower == b.oldPower && a.newPower == b.newPower && a.id.equals(b.id)
                && a.cmdId == b.cmdId && a.arg.equals(b.arg) && a.objective.equals(b.objective)
                && a.holder.equals(b.holder) && a.value == b.value;
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            System.err.println("    mismatch: " + what);
            fails++;
        }
    }
}
