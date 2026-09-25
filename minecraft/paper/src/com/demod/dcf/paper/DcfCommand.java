// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.paper;

import com.demod.dcf.Frame;
import com.demod.dcf.McEvent;
import com.demod.dcf.Medium;
import com.demod.dcf.Text;

import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;

/**
 * {@code /dcf ...} — usable from command blocks (BlockCommandSender), the console and players:
 * <pre>
 *   /dcf tx HEX34                     send a raw frame as-is
 *   /dcf frame TYPE SEQ DST HEX8      build + send a frame (src = this node)
 *   /dcf event redstone DIM X Y Z OLD NEW | block DIM X Y Z ID | trigger ID [ARG] | score OBJ HOLDER VALUE
 *   /dcf say TEXT...                  DCF-Text on mc-chat
 *   /dcf latch                        read the IN lanes now and send them (what STROBE_IN does)
 *   /dcf status
 * </pre>
 */
public final class DcfCommand implements CommandExecutor {
    private final DcfPlugin plugin;

    DcfCommand(DcfPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender s, Command c, String label, String[] a) {
        if (a.length == 0) {
            return false;
        }
        try {
            switch (a[0].toLowerCase()) {
                case "tx": {
                    byte[] f = unhex(a[1]);
                    if (!Medium.gate(f)) {
                        s.sendMessage("dcf: invalid frame (gate)");
                        return true;
                    }
                    plugin.sendRaw(f);
                    return true;
                }
                case "frame": {
                    Frame f = new Frame();
                    f.type = Integer.decode(a[1]);
                    f.seq = Integer.decode(a[2]);
                    f.src = plugin.nodeId();
                    f.dst = Integer.decode(a[3]);
                    f.payload = unhex(a[4]);
                    f.tsUs = (int) ((System.nanoTime() / 1000L) & 0xFFFFFF);
                    plugin.sendRaw(f.encode());
                    return true;
                }
                case "event": {
                    plugin.sendEvent(event(a));
                    return true;
                }
                case "say": {
                    StringBuilder sb = new StringBuilder();
                    for (int i = 1; i < a.length; i++) {
                        sb.append(i > 1 ? " " : "").append(a[i]);
                    }
                    plugin.sendText(sb.toString(), Text.FLAG_RELIABLE);
                    return true;
                }
                case "latch": {
                    byte[] f = plugin.writer().readRegister();
                    s.sendMessage("DCF TX " + WorldWriter.join(McEvent.frameToWords(f)));
                    if (Medium.gate(f)) {
                        plugin.sendRaw(f);
                    } else {
                        s.sendMessage("dcf: register holds an invalid frame (NAK)");
                    }
                    return true;
                }
                case "status": {
                    s.sendMessage("dcf node 0x" + Integer.toHexString(plugin.nodeId()) + " udp:" + plugin.node().localPort()
                            + " peers=" + plugin.node().peers() + " sent=" + plugin.node().sent + " recv=" + plugin.node().recv
                            + " dedup=" + plugin.node().deduped + " origin=" + plugin.writer().originString());
                    return true;
                }
                default:
                    return false;
            }
        } catch (RuntimeException e) {
            s.sendMessage("dcf: " + e.getMessage());
            return false;
        }
    }

    private static McEvent.Event event(String[] a) {
        switch (a[1].toLowerCase()) {
            case "redstone":
                return McEvent.Event.redstone(Integer.parseInt(a[2]), Integer.parseInt(a[3]), Integer.parseInt(a[4]),
                        Integer.parseInt(a[5]), Integer.parseInt(a[6]), Integer.parseInt(a[7]));
            case "block":
                return McEvent.Event.blockSet(Integer.parseInt(a[2]), Integer.parseInt(a[3]), Integer.parseInt(a[4]),
                        Integer.parseInt(a[5]), a[6]);
            case "trigger":
                return McEvent.Event.cmdTrigger(Integer.parseInt(a[2]), a.length > 3 ? a[3] : "");
            case "score":
                return McEvent.Event.scoreboard(a[2], a[3], Integer.parseInt(a[4]));
            default:
                throw new IllegalArgumentException("event redstone|block|trigger|score");
        }
    }

    static byte[] unhex(String s) {
        if (s.length() % 2 != 0) {
            throw new IllegalArgumentException("odd hex");
        }
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }
}
