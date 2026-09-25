// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.fabric;

import com.demod.dcf.Frame;
import com.demod.dcf.Game;
import com.demod.dcf.McEvent;
import com.demod.dcf.Medium;
import com.demod.dcf.mc.DcfUdpNode;

import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.text.Text;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.Queue;
import java.util.concurrent.ConcurrentLinkedQueue;

import static net.minecraft.server.command.CommandManager.argument;
import static net.minecraft.server.command.CommandManager.literal;

/**
 * DCF-Minecraft for Fabric (server side, so it also runs inside a single-player world's
 * integrated server). Same behaviour as the Paper plugin: UDP both dialects; inbound frames
 * queued from the UDP thread and applied at END_SERVER_TICK; STROBE_IN sampled every tick
 * (rising edge latches the IN lanes); `/dcf ...` for command blocks.
 */
public final class DcfMod implements ModInitializer {
    public static final String MOD_ID = "dcf_minecraft";
    private DcfConfig cfg;
    private DcfUdpNode node;
    private FabricWorld world;
    private final Queue<byte[]> inbound = new ConcurrentLinkedQueue<>();
    private boolean strobePrev;
    private int gameSeq;
    private int textSeq;

    @Override
    public void onInitialize() {
        try {
            cfg = DcfConfig.load(FabricLoader.getInstance().getConfigDir().resolve("dcf-minecraft.properties"));
        } catch (IOException e) {
            throw new IllegalStateException("dcf-minecraft.properties", e);
        }
        ServerLifecycleEvents.SERVER_STARTED.register(this::onStarted);
        ServerLifecycleEvents.SERVER_STOPPING.register(s -> {
            if (node != null) {
                node.close();
                node = null;
            }
        });
        ServerTickEvents.END_SERVER_TICK.register(this::onTick);
        CommandRegistrationCallback.EVENT.register((d, reg, env) -> d.register(
                literal("dcf").requires(net.minecraft.server.command.CommandManager.requirePermissionLevel(
                                net.minecraft.server.command.CommandManager.GAMEMASTERS_CHECK))
                        .then(literal("tx").then(argument("hex", StringArgumentType.word()).executes(c -> {
                            byte[] f = unhex(StringArgumentType.getString(c, "hex"));
                            if (!Medium.gate(f)) {
                                c.getSource().sendError(Text.literal("dcf: invalid frame (gate)"));
                                return 0;
                            }
                            sendRaw(f);
                            return 1;
                        })))
                        .then(literal("say").then(argument("text", StringArgumentType.greedyString()).executes(c -> {
                            sendText(StringArgumentType.getString(c, "text"), com.demod.dcf.Text.FLAG_RELIABLE);
                            return 1;
                        })))
                        .then(literal("latch").executes(c -> {
                            latch(c.getSource());
                            return 1;
                        }))
                        .then(literal("event").then(literal("trigger")
                                .then(argument("id", IntegerArgumentType.integer(0, 65535)).executes(c -> {
                                    sendEvent(McEvent.Event.cmdTrigger(IntegerArgumentType.getInteger(c, "id"), ""));
                                    return 1;
                                }).then(argument("arg", StringArgumentType.greedyString()).executes(c -> {
                                    sendEvent(McEvent.Event.cmdTrigger(IntegerArgumentType.getInteger(c, "id"),
                                            StringArgumentType.getString(c, "arg")));
                                    return 1;
                                })))))
                        .then(literal("status").executes(c -> {
                            c.getSource().sendFeedback(() -> Text.literal("dcf node 0x" + Integer.toHexString(cfg.nodeId)
                                    + " udp:" + (node == null ? "-" : node.localPort()) + " peers=" + cfg.peers
                                    + (node == null ? "" : " sent=" + node.sent + " recv=" + node.recv)), false);
                            return 1;
                        }))));
    }

    private void onStarted(MinecraftServer server) {
        world = new FabricWorld(server, cfg);
        try {
            node = new DcfUdpNode(new InetSocketAddress(cfg.bindHost, cfg.bindPort), cfg.peers, cfg.flushMs);
        } catch (IOException e) {
            throw new IllegalStateException("cannot open the DCF UDP socket", e);
        }
        node.start((f, from) -> inbound.add(f));
        server.sendMessage(Text.literal("[DCF] node 0x" + Integer.toHexString(cfg.nodeId) + " up on UDP "
                + node.localPort() + " -> " + cfg.peers + "; origin " + cfg.origin[0] + " " + cfg.origin[1] + " " + cfg.origin[2]));
    }

    private void onTick(MinecraftServer server) {
        if (world == null) {
            return;
        }
        byte[] f;
        int n = 0;
        while ((f = inbound.poll()) != null && n++ < 64) {
            world.apply(f, this);
        }
        boolean strobe = world.strobePowered();
        if (strobe && !strobePrev) {
            latch(server.getCommandSource());
        }
        strobePrev = strobe;
        world.pollDatapackTx(this);
        world.tickPulses();
    }

    void latch(ServerCommandSource src) {
        byte[] frame = world.readRegister();
        String line = "DCF TX " + join(McEvent.frameToWords(frame));
        world.broadcast(line);
        boolean valid = Medium.gate(frame);
        world.pulse(valid ? McEvent.ackOut(cfg.origin) : McEvent.nakOut(cfg.origin));
        if (valid) {
            sendRaw(frame);
        } else {
            src.sendError(Text.literal("dcf: register holds an invalid frame (NAK)"));
        }
    }

    public void sendRaw(byte[] frame) {
        if (node != null) {
            node.send(frame);
        }
    }

    public void sendEvent(McEvent.Event e) {
        int packetId = gameSeq++ & Game.MAX_PACKET_ID;
        for (byte[] f : McEvent.packetize(e, packetId, tsUs(), cfg.nodeId)) {
            sendRaw(f);
        }
    }

    public void sendText(String text, int flags) {
        int packetId = textSeq++ & com.demod.dcf.Text.MAX_PACKET_ID;
        for (byte[] f : com.demod.dcf.Text.packetize(text, packetId, tsUs(), cfg.nodeId, McEvent.CH_CHAT, flags)) {
            sendRaw(f);
        }
    }

    private static int tsUs() {
        return (int) ((System.nanoTime() / 1000L) & 0xFFFFFF);
    }

    static String join(int[] w) {
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < w.length; k++) {
            sb.append(k > 0 ? " " : "").append(w[k]);
        }
        return sb.toString();
    }

    static byte[] unhex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }
}
