// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.paper;

import com.demod.dcf.Frame;
import com.demod.dcf.Game;
import com.demod.dcf.McEvent;
import com.demod.dcf.Text;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.Barrel;
import org.bukkit.block.Block;
import org.bukkit.block.BlockState;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.ItemStack;
import org.bukkit.scoreboard.Objective;
import org.bukkit.scoreboard.Scoreboard;

import java.util.List;

/**
 * Applies inbound frames to the world on the main thread. Two modes (config `mode`):
 * <ul>
 *   <li>{@code native}: the plugin IS the register — raw frames go straight into the 34 barrels
 *       (item counts per {@link McEvent#frameToItems}) with a VALID pulse; DCF-Game EVENTs on
 *       mc-world become setblock / scoreboard changes; DCF-Text on mc-chat is broadcast.</li>
 *   <li>{@code datapack}: the datapack is installed too, so raw frames are handed to it as
 *       scoreboard words + {@code function dcf:rx_commit} (its VALID pulse, its announce).</li>
 * </ul>
 */
public final class WorldWriter {
    private final DcfPlugin plugin;
    private final Game.Reassembler game = new Game.Reassembler();
    private final Text.Reassembler text = new Text.Reassembler(McEvent.CH_CHAT);
    private final String worldName;
    private final int[] origin;
    private final boolean datapack;
    private final String ns;
    private final int pulseTicks;

    WorldWriter(DcfPlugin plugin) {
        this.plugin = plugin;
        this.worldName = plugin.getConfig().getString("world", "world");
        List<Integer> o = plugin.getConfig().getIntegerList("origin");
        this.origin = o.size() == 3 ? new int[] {o.get(0), o.get(1), o.get(2)} : new int[] {0, 64, 0};
        this.datapack = "datapack".equalsIgnoreCase(plugin.getConfig().getString("mode", "native"));
        this.ns = plugin.getConfig().getString("namespace", "dcf");
        this.pulseTicks = plugin.getConfig().getInt("pulse-ticks", 4);
    }

    String originString() {
        return origin[0] + " " + origin[1] + " " + origin[2];
    }

    public int[] origin() {
        return origin;
    }

    public World world() {
        World w = Bukkit.getWorld(worldName);
        return w != null ? w : Bukkit.getWorlds().get(0);
    }

    /** Route one inbound frame (main thread). */
    public void apply(byte[] frame) {
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
                Bukkit.broadcastMessage("[DCF 0x" + Integer.toHexString(m.src) + "] " + m.text());
            }
            return;
        }
        writeRegister(frame);
    }

    /** A raw frame into the OUT lanes. */
    public void writeRegister(byte[] frame) {
        if (datapack) {
            int[] w = McEvent.frameToWords(frame);
            for (int k = 0; k < w.length; k++) {
                Bukkit.dispatchCommand(Bukkit.getConsoleSender(),
                        "scoreboard players set w" + k + " " + McEvent.OBJ_REG + " " + w[k]);
            }
            Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "function " + ns + ":rx_commit");
            return;
        }
        World w = world();
        int[] items = McEvent.frameToItems(frame);
        for (int i = 0; i < McEvent.NIBBLES; i++) {
            int[] p = McEvent.lanePos(origin, i, 0);
            Block b = w.getBlockAt(p[0], p[1], p[2]);
            if (b.getType() != Material.BARREL) {
                b.setType(Material.BARREL);
            }
            BlockState st = b.getState();
            if (st instanceof Barrel) {
                Inventory inv = ((Barrel) st).getInventory();
                inv.clear();
                int left = items[i];
                for (int slot = 0; left > 0 && slot < McEvent.BARREL_SLOTS; slot++) {
                    int n = Math.min(64, left);
                    inv.setItem(slot, new ItemStack(Material.STONE, n));
                    left -= n;
                }
            }
        }
        pulse(McEvent.validOut(origin));
        Scoreboard sb = Bukkit.getScoreboardManager().getMainScoreboard();
        Objective reg = sb.getObjective(McEvent.OBJ_REG);
        if (reg != null) {
            int[] words = McEvent.frameToWords(frame);
            for (int k = 0; k < words.length; k++) {
                reg.getScore("w" + k).setScore(words[k]);
            }
        }
        Bukkit.broadcastMessage("DCF RX " + join(McEvent.frameToWords(frame)));
    }

    private void applyEvent(byte[] body) {
        McEvent.Event e;
        try {
            e = McEvent.unpack(body);
        } catch (IllegalArgumentException ex) {
            plugin.getLogger().warning("bad EVENT body " + Frame.hex(body));
            return;
        }
        World w = worldFor(e.dim);
        switch (e.tag) {
            case McEvent.EVT_BLOCK_SET: {
                Material m = Material.matchMaterial(e.id);
                if (m != null && m.isBlock()) {
                    w.getBlockAt(e.x, e.y, e.z).setType(m);
                }
                break;
            }
            case McEvent.EVT_REDSTONE: {
                Block b = w.getBlockAt(e.x, e.y, e.z);
                b.setType(e.newPower > 0 ? Material.REDSTONE_BLOCK : Material.AIR);
                break;
            }
            case McEvent.EVT_SCOREBOARD: {
                Scoreboard sb = Bukkit.getScoreboardManager().getMainScoreboard();
                Objective o = sb.getObjective(e.objective);
                if (o == null) {
                    o = sb.registerNewObjective(e.objective, "dummy", e.objective);
                }
                o.getScore(e.holder).setScore(e.value);
                break;
            }
            case McEvent.EVT_CMD_TRIGGER: {
                Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "function " + ns + ":trigger_" + e.cmdId
                        + (e.arg.isEmpty() ? "" : " " + e.arg));
                break;
            }
            default:
                break;
        }
    }

    private World worldFor(int dim) {
        for (World w : Bukkit.getWorlds()) {
            switch (w.getEnvironment()) {
                case NORMAL: if (dim == McEvent.DIM_OVERWORLD) return w; break;
                case NETHER: if (dim == McEvent.DIM_NETHER) return w; break;
                case THE_END: if (dim == McEvent.DIM_END) return w; break;
                default: break;
            }
        }
        return world();
    }

    public void pulse(int[] p) {
        World w = world();
        Block b = w.getBlockAt(p[0], p[1], p[2]);
        b.setType(Material.REDSTONE_BLOCK);
        Bukkit.getScheduler().runTaskLater(plugin, () -> {
            if (b.getType() == Material.REDSTONE_BLOCK) {
                b.setType(Material.AIR);
            }
        }, pulseTicks);
    }

    /**
     * In datapack mode the datapack latches frames itself (STROBE_IN, /function dcf:tx, ...)
     * and leaves them in dcf_reg w0..w5 with dcf_ctl tx_pending = 1; poll that every tick (no
     * console hop), send, ACK/NAK by the gate and clear it — what `punctim mc` does over RCON.
     */
    public void pollDatapackTx() {
        if (!datapack) {
            return;
        }
        Scoreboard sb = Bukkit.getScoreboardManager().getMainScoreboard();
        Objective ctl = sb.getObjective(McEvent.OBJ_CTL);
        Objective reg = sb.getObjective(McEvent.OBJ_REG);
        if (ctl == null || reg == null || ctl.getScore("tx_pending").getScore() != 1) {
            return;
        }
        int[] w = new int[McEvent.WORDS];
        for (int k = 0; k < w.length; k++) {
            w[k] = reg.getScore("w" + k).getScore();
        }
        byte[] frame;
        boolean valid;
        try {
            frame = McEvent.wordsToFrame(w);
            valid = com.demod.dcf.Medium.gate(frame);
        } catch (IllegalArgumentException e) {
            frame = null;
            valid = false;
        }
        ctl.getScore("ack").setScore(valid ? 1 : 2);
        ctl.getScore("tx_pending").setScore(0);
        if (valid) {
            plugin.sendRaw(frame);
        }
    }

    /** Current IN-lane reading (redstone-wire power per lane) as a 17-byte frame candidate. */
    public byte[] readRegister() {
        World w = world();
        int[] nib = new int[McEvent.NIBBLES];
        for (int i = 0; i < McEvent.NIBBLES; i++) {
            int[] p = McEvent.lanePos(origin, i, 3);
            Block b = w.getBlockAt(p[0], p[1], p[2]);
            nib[i] = b.getType() == Material.REDSTONE_WIRE ? b.getBlockPower() : 0;
        }
        return McEvent.nibblesToFrame(nib);
    }

    public boolean insideRegister(Location l) {
        if (!l.getWorld().equals(world())) {
            return false;
        }
        int x = l.getBlockX(), y = l.getBlockY(), z = l.getBlockZ();
        return y == origin[1] && x >= origin[0] - 6 && x <= origin[0] + 2 * (McEvent.NIBBLES - 1)
                && z >= origin[2] && z <= origin[2] + 3;
    }

    static String join(int[] w) {
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < w.length; k++) {
            if (k > 0) {
                sb.append(' ');
            }
            sb.append(w[k]);
        }
        return sb.toString();
    }
}
