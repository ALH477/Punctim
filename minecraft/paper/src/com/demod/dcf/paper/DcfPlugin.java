// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.paper;

import com.demod.dcf.Frame;
import com.demod.dcf.Game;
import com.demod.dcf.McEvent;
import com.demod.dcf.Text;
import com.demod.dcf.mc.DcfUdpNode;
import com.demod.dcf.mc.Peer;

import org.bukkit.Bukkit;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Queue;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.logging.Level;

/**
 * DCF-Minecraft for Paper: the world's DeModFrame register (Documentation/DCF_MINECRAFT_SPEC.md)
 * bridged to Punctim UDP peers, no console hop. Frames arrive on the UDP thread, are queued,
 * and applied to the world on the main thread once per tick; frames leave from `/dcf ...`
 * (command blocks included), from BlockRedstoneEvent inside the register, and from the datapack's
 * `DCF TX` announcements when the datapack is installed alongside (mode: datapack).
 */
public final class DcfPlugin extends JavaPlugin {
    private DcfUdpNode node;
    private WorldWriter writer;
    private RedstoneListener listener;
    private final Queue<byte[]> inbound = new ConcurrentLinkedQueue<>();
    private int gameSeq;
    private int textSeq;
    private int rawSeq;
    private int nodeId;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        nodeId = Integer.decode(getConfig().getString("node-id", "0x00B1"));
        List<Peer> peers = new ArrayList<>();
        for (String p : getConfig().getStringList("peers")) {
            peers.add(Peer.parse(p));
        }
        String bind = getConfig().getString("bind", "0.0.0.0:0");
        int colon = bind.lastIndexOf(':');
        try {
            node = new DcfUdpNode(new InetSocketAddress(bind.substring(0, colon), Integer.parseInt(bind.substring(colon + 1))),
                    peers, getConfig().getLong("flush-ms", 20));
        } catch (IOException e) {
            getLogger().log(Level.SEVERE, "cannot open the UDP socket " + bind, e);
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        writer = new WorldWriter(this);
        listener = new RedstoneListener(this);
        getServer().getPluginManager().registerEvents(listener, this);
        getCommand("dcf").setExecutor(new DcfCommand(this));
        node.start(this::onFrame);
        Bukkit.getScheduler().runTaskTimer(this, this::drain, 1L, 1L);
        getLogger().info("DCF node 0x" + Integer.toHexString(nodeId) + " up on UDP " + node.localPort()
                + " -> " + peers + "; register origin " + writer.originString()
                + "; mc-world 0x" + Integer.toHexString(McEvent.CH_WORLD));
    }

    @Override
    public void onDisable() {
        if (node != null) {
            node.close();
        }
    }

    // ── egress ─────────────────────────────────────────────────────────────
    public int nodeId() {
        return nodeId;
    }

    public DcfUdpNode node() {
        return node;
    }

    public WorldWriter writer() {
        return writer;
    }

    /** Send a raw 17-byte frame as-is. */
    public void sendRaw(byte[] frame) {
        node.send(frame);
        getLogger().info("frame " + Frame.hex(frame) + " -> peers");
    }

    /** Send a Minecraft event as DCF-Game EVENT frames on mc-world (reliable). */
    public void sendEvent(McEvent.Event e) {
        int packetId = gameSeq++ & Game.MAX_PACKET_ID;
        for (byte[] f : McEvent.packetize(e, packetId, tsUs(), nodeId)) {
            node.send(f);
        }
    }

    /** Send chat text as DCF-Text on mc-chat. */
    public void sendText(String text, int flags) {
        int packetId = textSeq++ & Text.MAX_PACKET_ID;
        for (byte[] f : Text.packetize(text, packetId, tsUs(), nodeId, McEvent.CH_CHAT, flags)) {
            node.send(f);
        }
    }

    public int nextRawSeq() {
        return rawSeq++ & 0xFFFF;
    }

    private static int tsUs() {
        return (int) ((System.nanoTime() / 1000L) & 0xFFFFFF);
    }

    // ── ingress ────────────────────────────────────────────────────────────
    private void onFrame(byte[] frame, SocketAddress from) {
        inbound.add(frame);           // UDP thread: never touch Bukkit here
    }

    private void drain() {
        byte[] f;
        int n = 0;
        while ((f = inbound.poll()) != null && n++ < 64) {
            getLogger().info("frame " + Frame.hex(f) + " from mesh");
            writer.apply(f);
        }
        writer.pollDatapackTx();
    }
}
