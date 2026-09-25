// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.paper;

import com.demod.dcf.McEvent;

import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.block.Block;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockRedstoneEvent;

/**
 * The redstone side of the register. A rising edge on STROBE_IN latches the IN lanes into a
 * frame and sends it (like the datapack's `dcf:latch`, but in-process). Optionally every
 * redstone change inside a configured watch region is reported as a REDSTONE event.
 */
public final class RedstoneListener implements Listener {
    private final DcfPlugin plugin;
    private final boolean watchRegion;
    private final int[] watchMin;
    private final int[] watchMax;
    private long lastStrobeTick = -1;

    RedstoneListener(DcfPlugin plugin) {
        this.plugin = plugin;
        this.watchRegion = plugin.getConfig().getBoolean("watch.enabled", false);
        this.watchMin = ints(plugin.getConfig().getIntegerList("watch.min"), new int[] {0, 0, 0});
        this.watchMax = ints(plugin.getConfig().getIntegerList("watch.max"), new int[] {0, 0, 0});
    }

    private static int[] ints(java.util.List<Integer> l, int[] dflt) {
        return l.size() == 3 ? new int[] {l.get(0), l.get(1), l.get(2)} : dflt;
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onRedstone(BlockRedstoneEvent ev) {
        Block b = ev.getBlock();
        int[] o = plugin.writer().origin();
        int[] strobe = McEvent.strobeIn(o);
        Location l = b.getLocation();
        if (b.getType() == Material.REDSTONE_WIRE && l.getBlockX() == strobe[0] && l.getBlockY() == strobe[1]
                && l.getBlockZ() == strobe[2] && ev.getOldCurrent() == 0 && ev.getNewCurrent() > 0) {
            long tick = b.getWorld().getFullTime();
            if (tick != lastStrobeTick) {            // one latch per tick, however many updates
                lastStrobeTick = tick;
                // the wires settle after this event; sample on the next tick
                plugin.getServer().getScheduler().runTask(plugin, () -> {
                    byte[] frame = plugin.writer().readRegister();
                    plugin.getServer().broadcastMessage("DCF TX " + WorldWriter.join(McEvent.frameToWords(frame)));
                    boolean valid = com.demod.dcf.Medium.gate(frame);
                    plugin.writer().pulse(valid ? McEvent.ackOut(o) : McEvent.nakOut(o));
                    if (valid) {
                        plugin.sendRaw(frame);
                    }
                });
            }
            return;
        }
        if (watchRegion && inside(l) && !plugin.writer().insideRegister(l)) {
            plugin.sendEvent(McEvent.Event.redstone(dim(b), l.getBlockX(), l.getBlockY(), l.getBlockZ(),
                    ev.getOldCurrent(), ev.getNewCurrent()));
        }
    }

    private boolean inside(Location l) {
        return l.getBlockX() >= watchMin[0] && l.getBlockX() <= watchMax[0]
                && l.getBlockY() >= watchMin[1] && l.getBlockY() <= watchMax[1]
                && l.getBlockZ() >= watchMin[2] && l.getBlockZ() <= watchMax[2];
    }

    static int dim(Block b) {
        switch (b.getWorld().getEnvironment()) {
            case NORMAL: return McEvent.DIM_OVERWORLD;
            case NETHER: return McEvent.DIM_NETHER;
            case THE_END: return McEvent.DIM_END;
            default: return McEvent.DIM_OTHER;
        }
    }
}
