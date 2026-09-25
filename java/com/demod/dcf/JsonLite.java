// SPDX-License-Identifier: LGPL-3.0-only
package com.demod.dcf;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A minimal, dependency-free JSON reader shared by the vector certifiers: objects
 * (LinkedHashMap), arrays (ArrayList), strings (with escapes), numbers (kept as their exact
 * source text in {@link Num}, so a u64 never loses precision), true/false, null. Plus the
 * small typed accessors every certifier needs and the repo-relative vector-file resolver.
 */
final class JsonLite {
    private JsonLite() {
    }

    static Object parse(String text) {
        return new Parser(text).value();
    }

    static Map<String, Object> read(Path p) throws IOException {
        return obj(parse(new String(Files.readAllBytes(p), StandardCharsets.UTF_8)));
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> obj(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    static List<Object> arr(Object o) {
        return (List<Object>) o;
    }

    static String str(Object o) {
        return (String) o;
    }

    /** A JSON integer that fits a long. */
    static long num(Object o) {
        return Long.parseLong(o.toString());
    }

    static double dbl(Object o) {
        return Double.parseDouble(o.toString());
    }

    static List<String> strs(Object o) {
        List<String> out = new ArrayList<>();
        for (Object x : arr(o)) {
            out.add(str(x));
        }
        return out;
    }

    static List<byte[]> frames(Object o) {
        List<byte[]> out = new ArrayList<>();
        for (String h : strs(o)) {
            out.add(unhex(h));
        }
        return out;
    }

    static List<String> hexes(List<byte[]> bs) {
        List<String> out = new ArrayList<>();
        for (byte[] b : bs) {
            out.add(Frame.hex(b));
        }
        return out;
    }

    static int[] ints(Object o) {
        List<Object> a = arr(o);
        int[] out = new int[a.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = (int) num(a.get(i));
        }
        return out;
    }

    static byte[] unhex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    /** args[0] if given, else the first of the usual repo-relative locations that exists. */
    static Path resolve(String[] args, String name) {
        if (args.length > 0) {
            return Path.of(args[0]);
        }
        String[] candidates = {
            "Documentation/" + name, "../Documentation/" + name, "../../Documentation/" + name,
            "python/MCP/" + name,
        };
        List<Path> tried = new ArrayList<>();
        for (String c : candidates) {
            Path p = Path.of(c);
            if (Files.exists(p)) {
                return p;
            }
            tried.add(p);
        }
        throw new IllegalStateException(name + " not found; tried " + tried);
    }

    /** A JSON number kept as its exact source text. */
    static final class Num {
        private final String text;

        Num(String text) {
            this.text = text;
        }

        @Override
        public String toString() {
            return text;
        }
    }

    private static final class Parser {
        private final String t;
        private int p;

        Parser(String text) {
            this.t = text;
        }

        private IllegalStateException fail(String m) {
            return new IllegalStateException("json: " + m + " at " + p);
        }

        private void ws() {
            while (p < t.length() && " \t\r\n".indexOf(t.charAt(p)) >= 0) {
                p++;
            }
        }

        private boolean eat(char c) {
            ws();
            if (p < t.length() && t.charAt(p) == c) {
                p++;
                return true;
            }
            return false;
        }

        private void need(char c) {
            if (!eat(c)) {
                throw fail("expected '" + c + "'");
            }
        }

        Object value() {
            ws();
            if (p >= t.length()) {
                throw fail("eof");
            }
            char c = t.charAt(p);
            if (c == '{') {
                p++;
                Map<String, Object> m = new LinkedHashMap<>();
                if (eat('}')) {
                    return m;
                }
                do {
                    ws();
                    String k = string();
                    need(':');
                    m.put(k, value());
                } while (eat(','));
                need('}');
                return m;
            }
            if (c == '[') {
                p++;
                List<Object> a = new ArrayList<>();
                if (eat(']')) {
                    return a;
                }
                do {
                    a.add(value());
                } while (eat(','));
                need(']');
                return a;
            }
            if (c == '"') {
                return string();
            }
            if (t.startsWith("true", p)) {
                p += 4;
                return Boolean.TRUE;
            }
            if (t.startsWith("false", p)) {
                p += 5;
                return Boolean.FALSE;
            }
            if (t.startsWith("null", p)) {
                p += 4;
                return null;
            }
            int s = p;
            while (p < t.length() && "+-0123456789.eE".indexOf(t.charAt(p)) >= 0) {
                p++;
            }
            if (s == p) {
                throw fail("bad value");
            }
            return new Num(t.substring(s, p));
        }

        private String string() {
            need('"');
            StringBuilder sb = new StringBuilder();
            while (p < t.length() && t.charAt(p) != '"') {
                char c = t.charAt(p++);
                if (c != '\\') {
                    sb.append(c);
                    continue;
                }
                if (p >= t.length()) {
                    throw fail("bad escape");
                }
                char e = t.charAt(p++);
                switch (e) {
                    case '"': sb.append('"'); break;
                    case '\\': sb.append('\\'); break;
                    case '/': sb.append('/'); break;
                    case 'b': sb.append('\b'); break;
                    case 'f': sb.append('\f'); break;
                    case 'n': sb.append('\n'); break;
                    case 'r': sb.append('\r'); break;
                    case 't': sb.append('\t'); break;
                    case 'u':
                        if (p + 4 > t.length()) {
                            throw fail("short \\u escape");
                        }
                        sb.append((char) Integer.parseInt(t.substring(p, p + 4), 16));
                        p += 4;
                        break;
                    default:
                        throw fail("bad escape");
                }
            }
            need('"');
            return sb.toString();
        }
    }
}
