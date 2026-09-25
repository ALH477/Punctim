// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.fabric;

import com.demod.dcf.Frame;
import com.demod.dcf.Game;
import com.demod.dcf.McEvent;
import com.demod.dcf.Medium;
import com.demod.dcf.Text;

import net.minecraft.block.BarrelBlock;
import net.minecraft.block.BlockState;
import net.minecraft.block.Blocks;
import net.minecraft.block.RedstoneWireBlock;
import net.minecraft.block.entity.BarrelBlockEntity;
import net.minecraft.block.entity.BlockEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.item.Items;
import net.minecraft.registry.Registries;
import net.minecraft.scoreboard.ScoreHolder;
import net.minecraft.scoreboard.Scoreboard;
import net.minecraft.scoreboard.ScoreboardObjective;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.World;

/** The world side for Fabric: barrels, wires, pulses, scoreboard, commands — main thread only. */
final class FabricWorld {
    private final MinecraftServer server;
    private final DcfConfig cfg;
    private final Game.Reassembler game = new Game.Reassembler();
    private final Text.Reassembler text = new Text.Reassembler(McEvent.CH_CHAT);

    FabricWorld(MinecraftServer server, DcfConfig cfg) {
        this.server = server;
        this.cfg = cfg;
    }

    ServerWorld overworld() {
        return server.getOverworld();
    }

    private static BlockPos pos(int[] p) {
        return new BlockPos(p[0], p[1], p[2]);
    }

    void broadcast(String line) {
        server.getPlayerManager().broadcast(net.minecraft.text.Text.literal(line), false);
    }

    void apply(byte[] frame, DcfMod mod) {
        Frame d;
        try {
            d = Frame.decode(frame);
        } catch (IllegalArgumentException e) {
            return;
        }
        if (d.type == Game.FDATA && d.dst == McEvent.CH_WORLD) {
            for (Game.Packet p : game.push(frame)) {
                if (p.msgTypeId == Game.GMSG_EVENT) {
                    applyEvent(p.payload);
                }
            }
            return;
        }
        if (d.type == Text.FDATA && d.dst == McEvent.CH_CHAT) {
            for (Text.Message m : text.push(frame)) {
                broadcast("[DCF 0x" + Integer.toHexString(m.src) + "] " + m.text());
            }
            return;
        }
        writeRegister(frame);
    }

    void writeRegister(byte[] frame) {
        int[] words = McEvent.frameToWords(frame);
        if (cfg.datapack) {
            for (int k = 0; k < words.length; k++) {
                run("scoreboard players set w" + k + " " + McEvent.OBJ_REG + " " + words[k]);
            }
            run("function " + cfg.namespace + ":rx_commit");
            return;
        }
        ServerWorld w = overworld();
        int[] items = McEvent.frameToItems(frame);
        for (int i = 0; i < McEvent.NIBBLES; i++) {
            BlockPos p = pos(McEvent.lanePos(cfg.origin, i, 0));
            if (!(w.getBlockState(p).getBlock() instanceof BarrelBlock)) {
                w.setBlockState(p, Blocks.BARREL.getDefaultState());
            }
            BlockEntity be = w.getBlockEntity(p);
            if (be instanceof BarrelBlockEntity barrel) {
                barrel.clear();
                int left = items[i];
                for (int slot = 0; left > 0 && slot < McEvent.BARREL_SLOTS; slot++) {
                    int n = Math.min(64, left);
                    barrel.setStack(slot, new ItemStack(Items.STONE, n));
                    left -= n;
                }
                barrel.markDirty();
                w.updateComparators(p, w.getBlockState(p).getBlock());
            }
        }
        pulse(McEvent.validOut(cfg.origin));
        Scoreboard sb = server.getScoreboard();
        ScoreboardObjective reg = sb.getNullableObjective(McEvent.OBJ_REG);
        if (reg != null) {
            for (int k = 0; k < words.length; k++) {
                sb.getOrCreateScore(ScoreHolder.fromName("w" + k), reg).setScore(words[k]);
            }
        }
        broadcast("DCF RX " + DcfMod.join(words));
    }

    private void applyEvent(byte[] body) {
        McEvent.Event e;
        try {
            e = McEvent.unpack(body);
        } catch (IllegalArgumentException ex) {
            return;
        }
        ServerWorld w = worldFor(e.dim);
        switch (e.tag) {
            case McEvent.EVT_BLOCK_SET -> {
                Identifier id = Identifier.tryParse(e.id);
                if (id != null && Registries.BLOCK.containsId(id)) {
                    w.setBlockState(new BlockPos(e.x, e.y, e.z), Registries.BLOCK.get(id).getDefaultState());
                }
            }
            case McEvent.EVT_REDSTONE -> w.setBlockState(new BlockPos(e.x, e.y, e.z),
                    e.newPower > 0 ? Blocks.REDSTONE_BLOCK.getDefaultState() : Blocks.AIR.getDefaultState());
            case McEvent.EVT_SCOREBOARD -> run("scoreboard players set " + e.holder + " " + e.objective + " " + e.value);
            case McEvent.EVT_CMD_TRIGGER -> run("function " + cfg.namespace + ":trigger_" + e.cmdId
                    + (e.arg.isEmpty() ? "" : " " + e.arg));
            default -> { }
        }
    }

    private ServerWorld worldFor(int dim) {
        return switch (dim) {
            case McEvent.DIM_NETHER -> server.getWorld(World.NETHER);
            case McEvent.DIM_END -> server.getWorld(World.END);
            default -> overworld();
        };
    }

    /** Redstone-block pulses: placed now, cleared by {@link #tickPulses} after pulseTicks. */
    private final java.util.Map<BlockPos, Integer> pendingClears = new java.util.HashMap<>();

    void pulse(int[] p) {
        BlockPos bp = pos(p);
        overworld().setBlockState(bp, Blocks.REDSTONE_BLOCK.getDefaultState());
        pendingClears.put(bp, cfg.pulseTicks);
    }

    /** Called once per server tick from DcfMod.onTick (main thread). */
    void tickPulses() {
        if (pendingClears.isEmpty()) {
            return;
        }
        ServerWorld w = overworld();
        java.util.Iterator<java.util.Map.Entry<BlockPos, Integer>> it = pendingClears.entrySet().iterator();
        while (it.hasNext()) {
            java.util.Map.Entry<BlockPos, Integer> e = it.next();
            int left = e.getValue() - 1;
            if (left > 0) {
                e.setValue(left);
                continue;
            }
            if (w.getBlockState(e.getKey()).isOf(Blocks.REDSTONE_BLOCK)) {
                w.setBlockState(e.getKey(), Blocks.AIR.getDefaultState());
            }
            it.remove();
        }
    }

    boolean strobePowered() {
        BlockState s = overworld().getBlockState(pos(McEvent.strobeIn(cfg.origin)));
        return s.isOf(Blocks.REDSTONE_WIRE) && s.get(RedstoneWireBlock.POWER) > 0;
    }

    byte[] readRegister() {
        ServerWorld w = overworld();
        int[] nib = new int[McEvent.NIBBLES];
        for (int i = 0; i < McEvent.NIBBLES; i++) {
            BlockState s = w.getBlockState(pos(McEvent.lanePos(cfg.origin, i, 3)));
            nib[i] = s.isOf(Blocks.REDSTONE_WIRE) ? s.get(RedstoneWireBlock.POWER) : 0;
        }
        return McEvent.nibblesToFrame(nib);
    }

    void pollDatapackTx(DcfMod mod) {
        if (!cfg.datapack) {
            return;
        }
        Scoreboard sb = server.getScoreboard();
        ScoreboardObjective ctl = sb.getNullableObjective(McEvent.OBJ_CTL);
        ScoreboardObjective reg = sb.getNullableObjective(McEvent.OBJ_REG);
        if (ctl == null || reg == null) {
            return;
        }
        if (sb.getOrCreateScore(ScoreHolder.fromName("tx_pending"), ctl).getScore() != 1) {
            return;
        }
        int[] w = new int[McEvent.WORDS];
        for (int k = 0; k < w.length; k++) {
            w[k] = sb.getOrCreateScore(ScoreHolder.fromName("w" + k), reg).getScore();
        }
        byte[] frame = null;
        boolean valid = false;
        try {
            frame = McEvent.wordsToFrame(w);
            valid = Medium.gate(frame);
        } catch (IllegalArgumentException ignored) {
            // NAK below
        }
        sb.getOrCreateScore(ScoreHolder.fromName("ack"), ctl).setScore(valid ? 1 : 2);
        sb.getOrCreateScore(ScoreHolder.fromName("tx_pending"), ctl).setScore(0);
        if (valid) {
            mod.sendRaw(frame);
        }
    }

    private void run(String command) {
        server.getCommandManager().parseAndExecute(server.getCommandSource().withSilent(), command);
    }
}
