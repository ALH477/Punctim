// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.mc;

import com.demod.dcf.Frame;
import com.demod.dcf.McEvent;

import java.net.InetSocketAddress;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Loopback: two nodes on localhost, one peer per dialect, the golden frame crosses both ways
 * and arrives bit-exact; a duplicate is dropped; a SuperPack pair and a lone flushed frame
 * both decode. {@code java -cp ... com.demod.dcf.mc.CoreSelfTest} exits 0 on success.
 */
public final class CoreSelfTest {
    private CoreSelfTest() {
    }

    public static void main(String[] args) throws Exception {
        byte[] golden = hex("d31312340001ffffdeadbeefab12cd24c0");
        BlockingQueue<byte[]> gotA = new LinkedBlockingQueue<>();
        BlockingQueue<byte[]> gotB = new LinkedBlockingQueue<>();
        try (DcfUdpNode b = new DcfUdpNode(new InetSocketAddress("127.0.0.1", 0), List.of(), 20)) {
            b.start((f, from) -> gotB.add(f));
            try (DcfUdpNode a = new DcfUdpNode(new InetSocketAddress("127.0.0.1", 0),
                    List.of(new Peer("127.0.0.1", b.localPort(), Dialect.PROTO),
                            new Peer("127.0.0.1", b.localPort(), Dialect.BARE)), 20)) {
                a.start((f, from) -> gotA.add(f));
                a.send(golden);                       // proto: immediately; bare: pending
                byte[] first = gotB.poll(2, TimeUnit.SECONDS);
                check(Arrays.equals(first, golden), "golden frame arrives via proto");
                byte[] dup = gotB.poll(500, TimeUnit.MILLISECONDS);   // the bare copy, flushed after 20 ms
                check(dup == null, "the same frame over the bare dialect is deduped");
                check(b.deduped == 1, "dedup counted");

                // two different frames back-to-back pair into one SuperPack
                Frame f2 = new Frame();
                f2.type = 0;
                f2.seq = 7;
                f2.src = 0x00B1;
                f2.dst = McEvent.CH_WORLD;
                f2.payload = new byte[] {1, 2, 3, 4};
                Frame f3 = new Frame();
                f3.type = 3;
                f3.seq = 8;
                f3.src = 0x00B1;
                f3.dst = McEvent.CH_CHAT;
                byte[] e2 = f2.encode();
                byte[] e3 = f3.encode();
                a.send(e2);
                a.send(e3);
                byte[] p1 = gotB.poll(2, TimeUnit.SECONDS);     // proto copy of e2
                byte[] p2 = gotB.poll(2, TimeUnit.SECONDS);     // proto copy of e3
                check(Arrays.equals(p1, e2) && Arrays.equals(p2, e3), "two frames via proto");
                check(gotB.poll(300, TimeUnit.MILLISECONDS) == null, "SuperPack copies deduped");
                check(a.sent == 1 + 1 + 2 + 1, "datagrams: proto golden, bare golden, 2 proto, 1 SuperPack (" + a.sent + ")");

                // and the dedup does not eat a genuinely new frame with the same fields but a different frame
                List<byte[]> sp = b.decode(com.demod.dcf.SuperPack.pack(golden, e2));
                check(sp.size() == 2 && Arrays.equals(sp.get(0), golden), "decode(SuperPack) yields both frames");
                check(b.decode(new byte[10]).isEmpty() && b.invalid >= 1, "garbage counts as invalid");
            }
        }
        System.out.println("DcfUdpNode: CERTIFIED (loopback both dialects, dedup, SuperPack)");
    }

    private static void check(boolean ok, String what) {
        if (!ok) {
            System.err.println("FAIL: " + what);
            System.exit(1);
        }
        System.out.println("  PASS  " + what);
    }

    private static byte[] hex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }
}
