// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.fabric;

import com.demod.dcf.mc.Peer;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

/** `config/dcf-minecraft.properties` — the same knobs as the Paper plugin's config.yml. */
public final class DcfConfig {
    public int nodeId = 0x00B1;
    public String bindHost = "0.0.0.0";
    public int bindPort = 0;
    public long flushMs = 20;
    public final List<Peer> peers = new ArrayList<>();
    public int[] origin = {0, 64, 0};
    public boolean datapack = false;
    public String namespace = "dcf";
    public int pulseTicks = 4;

    public static DcfConfig load(Path file) throws IOException {
        DcfConfig c = new DcfConfig();
        if (!Files.exists(file)) {
            Files.createDirectories(file.getParent());
            Files.writeString(file, String.join("\n",
                    "# DCF-Minecraft (Fabric) - Documentation/DCF_MINECRAFT_SPEC.md",
                    "node-id=0x00B1",
                    "bind=0.0.0.0:0",
                    "peers=127.0.0.1:7801/bare",
                    "flush-ms=20",
                    "origin=0 64 0",
                    "mode=native",
                    "namespace=dcf",
                    "pulse-ticks=4", ""), StandardCharsets.UTF_8);
        }
        Properties p = new Properties();
        try (var in = Files.newBufferedReader(file, StandardCharsets.UTF_8)) {
            p.load(in);
        }
        c.nodeId = Integer.decode(p.getProperty("node-id", "0x00B1"));
        String bind = p.getProperty("bind", "0.0.0.0:0");
        int colon = bind.lastIndexOf(':');
        c.bindHost = bind.substring(0, colon);
        c.bindPort = Integer.parseInt(bind.substring(colon + 1));
        c.flushMs = Long.parseLong(p.getProperty("flush-ms", "20"));
        for (String s : p.getProperty("peers", "").split(",")) {
            if (!s.isBlank()) {
                c.peers.add(Peer.parse(s.trim()));
            }
        }
        String[] o = p.getProperty("origin", "0 64 0").trim().split("\\s+");
        c.origin = new int[] {Integer.parseInt(o[0]), Integer.parseInt(o[1]), Integer.parseInt(o[2])};
        c.datapack = "datapack".equalsIgnoreCase(p.getProperty("mode", "native"));
        c.namespace = p.getProperty("namespace", "dcf");
        c.pulseTicks = Integer.parseInt(p.getProperty("pulse-ticks", "4"));
        return c;
    }
}
