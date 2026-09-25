// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.mc;

import com.demod.dcf.Frame;
import com.demod.dcf.Medium;
import com.demod.dcf.SuperPack;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.BiConsumer;

/**
 * One UDP socket that speaks BOTH DCF-Medium dialects, per peer. Receives 34-B ProtoMessage
 * MSG_FRAME datagrams, raw 17-B frames and 32-B SuperPacks alike; sends proto frames as
 * {@link Medium#protoFrameEncode} and bare frames paired into SuperPacks (a lone frame is
 * flushed raw after {@code flushMs}). Loop-free: a rolling dedup on (src, dst, seq, crc),
 * since the quantum has no TTL. Everything the codec touches is the certified
 * {@code com.demod.dcf} code; this class only moves datagrams.
 *
 * <p>Thread model: {@link #send} may be called from any thread (the game thread); received
 * frames are handed to the callback on the receive thread — a game must marshal them back to
 * its main thread itself.
 */
public final class DcfUdpNode implements AutoCloseable {
    private final DatagramSocket socket;
    private final List<Peer> peers;
    private final long flushMs;
    private final Object txLock = new Object();
    private byte[] pendingBare;
    private long pendingSince;
    private long protoSeq = 1;
    private volatile boolean running;
    private Thread rx;
    private Thread flusher;
    private final Map<String, Long> seen = new LinkedHashMap<String, Long>(256, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, Long> e) {
            return size() > 4096;
        }
    };
    public volatile long sent;
    public volatile long recv;
    public volatile long invalid;
    public volatile long deduped;

    public DcfUdpNode(InetSocketAddress bind, List<Peer> peers, long flushMs) throws IOException {
        this.socket = bind == null ? new DatagramSocket() : new DatagramSocket(bind);
        this.socket.setSoTimeout(200);
        this.peers = Collections.unmodifiableList(new ArrayList<>(peers));
        this.flushMs = flushMs;
    }

    public int localPort() {
        return socket.getLocalPort();
    }

    public List<Peer> peers() {
        return peers;
    }

    /** Start receiving; {@code onFrame(frame, fromAddress)} runs on the receive thread. */
    public void start(BiConsumer<byte[], SocketAddress> onFrame) {
        running = true;
        rx = new Thread(() -> rxLoop(onFrame), "dcf-udp-rx");
        rx.setDaemon(true);
        rx.start();
        flusher = new Thread(this::flushLoop, "dcf-udp-flush");
        flusher.setDaemon(true);
        flusher.start();
    }

    /** Send one 17-byte frame to every peer, in each peer's dialect. */
    public void send(byte[] frame) {
        if (frame.length != Frame.FRAME_SIZE) {
            throw new IllegalArgumentException("a DeModFrame is 17 bytes");
        }
        synchronized (txLock) {
            boolean anyProto = false;
            boolean anyBare = false;
            for (Peer p : peers) {
                if (p.dialect == Dialect.PROTO) {
                    anyProto = true;
                } else {
                    anyBare = true;
                }
            }
            if (anyProto) {
                byte[] dg = Medium.protoFrameEncode(frame, protoSeq++);
                emit(dg, Dialect.PROTO);
            }
            if (anyBare) {
                if (pendingBare == null) {
                    pendingBare = frame.clone();
                    pendingSince = System.nanoTime();
                } else {
                    byte[] dg = SuperPack.pack(pendingBare, frame);
                    pendingBare = null;
                    emit(dg, Dialect.BARE);
                }
            }
        }
    }

    /** Emit a lone bare frame now instead of waiting for a partner. */
    public void flush() {
        synchronized (txLock) {
            if (pendingBare != null) {
                emit(pendingBare, Dialect.BARE);
                pendingBare = null;
            }
        }
    }

    private void emit(byte[] dg, Dialect d) {
        for (Peer p : peers) {
            if (p.dialect != d) {
                continue;
            }
            try {
                socket.send(new DatagramPacket(dg, dg.length, p.address()));
                sent++;
            } catch (IOException ignored) {
                // a dead peer must not stall the game thread
            }
        }
    }

    private void flushLoop() {
        while (running) {
            try {
                Thread.sleep(Math.max(1, flushMs / 2));
            } catch (InterruptedException e) {
                return;
            }
            synchronized (txLock) {
                if (pendingBare != null && (System.nanoTime() - pendingSince) / 1_000_000L >= flushMs) {
                    emit(pendingBare, Dialect.BARE);
                    pendingBare = null;
                }
            }
        }
    }

    private void rxLoop(BiConsumer<byte[], SocketAddress> onFrame) {
        byte[] buf = new byte[2048];
        while (running) {
            DatagramPacket pkt = new DatagramPacket(buf, buf.length);
            try {
                socket.receive(pkt);
            } catch (SocketTimeoutException e) {
                continue;
            } catch (IOException e) {
                if (running) {
                    continue;
                }
                return;
            }
            byte[] dg = new byte[pkt.getLength()];
            System.arraycopy(buf, 0, dg, 0, dg.length);
            for (byte[] f : decode(dg)) {
                if (isDuplicate(f)) {
                    deduped++;
                    continue;
                }
                recv++;
                onFrame.accept(f, pkt.getSocketAddress());
            }
        }
    }

    /** Frames carried by one datagram of either dialect (empty if it carried none). */
    public List<byte[]> decode(byte[] dg) {
        List<byte[]> out = new ArrayList<>(2);
        if (dg.length == Medium.PROTO_HEADER_LEN + Frame.FRAME_SIZE) {
            byte[] f = Medium.protoFrameDecode(dg);
            if (f != null && Medium.gate(f)) {
                out.add(f);
                return out;
            }
        }
        if (dg.length == Frame.FRAME_SIZE || dg.length == SuperPack.SUPER_LEN) {
            try {
                for (byte[] f : Medium.bareDecode(dg)) {
                    if (Medium.gate(f)) {
                        out.add(f);
                    }
                }
            } catch (IllegalArgumentException ignored) {
                // a bad SuperPack CRC: nothing carried
            }
        }
        if (out.isEmpty()) {
            invalid++;
        }
        return out;
    }

    private boolean isDuplicate(byte[] f) {
        String key = Frame.hex(f);      // src/dst/seq/crc are all inside the 17 bytes
        synchronized (seen) {
            return seen.put(key, System.nanoTime()) != null;
        }
    }

    @Override
    public void close() {
        running = false;
        flush();
        socket.close();
        if (rx != null) {
            try {
                rx.join(1000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
    }
}
