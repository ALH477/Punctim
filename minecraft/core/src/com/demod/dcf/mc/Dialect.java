// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.mc;

/** The two DCF-Medium UDP dialects (Documentation/DCF_MEDIUM_SPEC.md). */
public enum Dialect {
    /** ProtoMessage envelope, MSG_FRAME=12, 34 B/frame: dcf_node.py, Go/Rust/C nodes, punctim io. */
    PROTO,
    /** Bare 17-B frames, consecutive pairs as one 32-B SuperPack: Hermes mesh_mcp.py, JS, web. */
    BARE;

    public static Dialect parse(String s) {
        switch (s.toLowerCase()) {
            case "proto": return PROTO;
            case "bare": return BARE;
            default: throw new IllegalArgumentException("dialect must be proto or bare: " + s);
        }
    }
}
