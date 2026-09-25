// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf.mc;

import java.net.InetSocketAddress;

/** A UDP peer and the dialect it speaks, e.g. {@code 127.0.0.1:7801/bare}. */
public final class Peer {
    public final String host;
    public final int port;
    public final Dialect dialect;

    public Peer(String host, int port, Dialect dialect) {
        this.host = host;
        this.port = port;
        this.dialect = dialect;
    }

    /** {@code host:port[/proto|/bare]}, default dialect bare. */
    public static Peer parse(String spec) {
        String hp = spec;
        Dialect d = Dialect.BARE;
        int slash = spec.indexOf('/');
        if (slash >= 0) {
            hp = spec.substring(0, slash);
            d = Dialect.parse(spec.substring(slash + 1));
        }
        int colon = hp.lastIndexOf(':');
        if (colon <= 0) {
            throw new IllegalArgumentException("peer wants host:port[/dialect]: " + spec);
        }
        return new Peer(hp.substring(0, colon), Integer.parseInt(hp.substring(colon + 1)), d);
    }

    public InetSocketAddress address() {
        return new InetSocketAddress(host, port);
    }

    @Override
    public String toString() {
        return host + ":" + port + "/" + dialect.name().toLowerCase();
    }
}
