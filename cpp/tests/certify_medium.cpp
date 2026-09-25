// SPDX-License-Identifier: LGPL-3.0-only
//
// Certifies dcf::medium (the Tier-B digital medium codecs: stream, hex, udp_proto,
// udp_bare, l2eth) byte-for-byte against the cross-language golden vectors in
// Documentation/medium_vectors.json. Dependency-free (a ~100-line JSON reader below).
//   g++ -std=c++17 -I cpp/include cpp/tests/certify_medium.cpp -o cert_med && ./cert_med
//   ./cert_med path/to/medium_vectors.json

#include "dcf/medium.hpp"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

// ── a minimal JSON reader (objects, arrays, strings with escapes, ints, bools, null) ──
struct J {
    enum Kind { NUL, BOOL, NUM, STR, ARR, OBJ } kind = NUL;
    bool b = false;
    std::string num;  // raw numeric text (vectors carry only non-negative integers)
    std::string s;
    std::vector<J> arr;
    std::map<std::string, J> obj;

    const J& operator[](const std::string& k) const {
        static const J nul;
        auto it = obj.find(k);
        return it == obj.end() ? nul : it->second;
    }
    const J& operator[](std::size_t i) const { return arr.at(i); }
    std::size_t size() const { return kind == ARR ? arr.size() : obj.size(); }
    std::uint64_t u64() const { return std::strtoull(num.c_str(), nullptr, 10); }
};

struct Parser {
    const std::string& t;
    std::size_t p = 0;
    explicit Parser(const std::string& text) : t(text) {}

    [[noreturn]] void fail(const char* m) const {
        throw std::runtime_error(std::string("json: ") + m + " at " + std::to_string(p));
    }
    void ws() { while (p < t.size() && (t[p] == ' ' || t[p] == '\n' || t[p] == '\r' || t[p] == '\t')) ++p; }
    bool eat(char c) { ws(); if (p < t.size() && t[p] == c) { ++p; return true; } return false; }
    void need(char c) { if (!eat(c)) fail("unexpected token"); }

    static void utf8(std::string& o, unsigned cp) {
        if (cp < 0x80) o += static_cast<char>(cp);
        else if (cp < 0x800) { o += static_cast<char>(0xC0 | (cp >> 6)); o += static_cast<char>(0x80 | (cp & 0x3F)); }
        else if (cp < 0x10000) {
            o += static_cast<char>(0xE0 | (cp >> 12));
            o += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            o += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            o += static_cast<char>(0xF0 | (cp >> 18));
            o += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            o += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            o += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }
    unsigned hex4() {
        if (p + 4 > t.size()) fail("short \\u escape");
        unsigned v = static_cast<unsigned>(std::stoul(t.substr(p, 4), nullptr, 16));
        p += 4;
        return v;
    }
    std::string str() {
        need('"');
        std::string o;
        while (p < t.size() && t[p] != '"') {
            char c = t[p++];
            if (c != '\\') { o += c; continue; }
            if (p >= t.size()) fail("bad escape");
            char e = t[p++];
            switch (e) {
                case '"': o += '"'; break;
                case '\\': o += '\\'; break;
                case '/': o += '/'; break;
                case 'b': o += '\b'; break;
                case 'f': o += '\f'; break;
                case 'n': o += '\n'; break;
                case 'r': o += '\r'; break;
                case 't': o += '\t'; break;
                case 'u': {
                    unsigned cp = hex4();
                    if (cp >= 0xD800 && cp < 0xDC00 && p + 1 < t.size() && t[p] == '\\' && t[p + 1] == 'u') {
                        p += 2;
                        unsigned lo = hex4();
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    }
                    utf8(o, cp);
                    break;
                }
                default: fail("bad escape");
            }
        }
        need('"');
        return o;
    }
    J value() {
        ws();
        if (p >= t.size()) fail("eof");
        J v;
        const char c = t[p];
        if (c == '{') {
            ++p; v.kind = J::OBJ;
            if (eat('}')) return v;
            do { ws(); std::string k = str(); need(':'); v.obj[k] = value(); } while (eat(','));
            need('}');
        } else if (c == '[') {
            ++p; v.kind = J::ARR;
            if (eat(']')) return v;
            do { v.arr.push_back(value()); } while (eat(','));
            need(']');
        } else if (c == '"') {
            v.kind = J::STR; v.s = str();
        } else if (t.compare(p, 4, "true") == 0) {
            v.kind = J::BOOL; v.b = true; p += 4;
        } else if (t.compare(p, 5, "false") == 0) {
            v.kind = J::BOOL; v.b = false; p += 5;
        } else if (t.compare(p, 4, "null") == 0) {
            p += 4;
        } else {
            v.kind = J::NUM;
            const std::size_t s = p;
            while (p < t.size() && (std::isdigit(static_cast<unsigned char>(t[p])) || t[p] == '-' ||
                                    t[p] == '+' || t[p] == '.' || t[p] == 'e' || t[p] == 'E')) ++p;
            if (s == p) fail("bad value");
            v.num = t.substr(s, p - s);
        }
        return v;
    }
};

std::string read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return std::string();
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

std::string load_vectors(int argc, char** argv) {
    if (argc > 1) return read_file(argv[1]);
    const char* candidates[] = {
        "Documentation/medium_vectors.json",
        "../Documentation/medium_vectors.json",
        "../../Documentation/medium_vectors.json",
        "python/MCP/medium_vectors.json",
    };
    for (const char* c : candidates) {
        std::string s = read_file(c);
        if (!s.empty()) return s;
    }
    return std::string();
}

using dcf::Bytes17;
using dcf::medium::Bytes;
using dcf::medium::Frames;

Bytes unhex(const std::string& h) {
    Bytes out;
    for (std::size_t i = 0; i + 1 < h.size(); i += 2)
        out.push_back(static_cast<std::uint8_t>(std::stoul(h.substr(i, 2), nullptr, 16)));
    return out;
}
std::string hex(const std::uint8_t* b, std::size_t n) {
    static const char* d = "0123456789abcdef";
    std::string s;
    for (std::size_t i = 0; i < n; ++i) { s += d[b[i] >> 4]; s += d[b[i] & 0xF]; }
    return s;
}
std::string hex(const Bytes& b) { return hex(b.data(), b.size()); }
std::string hex(const Bytes17& b) { return hex(b.data(), b.size()); }

Frames frames_of(const J& arr) {
    Frames out;
    for (const J& h : arr.arr) {
        const Bytes b = unhex(h.s);
        if (b.size() != dcf::FRAME_SIZE) throw std::runtime_error("vector frame is not 17 bytes");
        Bytes17 f{};
        std::copy(b.begin(), b.end(), f.begin());
        out.push_back(f);
    }
    return out;
}
std::vector<std::string> hexes(const Frames& fs) {
    std::vector<std::string> out;
    for (const auto& f : fs) out.push_back(hex(f));
    return out;
}
std::vector<std::string> strs(const J& arr) {
    std::vector<std::string> out;
    for (const J& h : arr.arr) out.push_back(h.s);
    return out;
}

int fails = 0;
int check(bool cond, const std::string& label) {
    std::printf("  %s  %s\n", cond ? "PASS" : "FAIL", label.c_str());
    if (!cond) ++fails;
    return cond ? 0 : 1;
}
void note(const std::string& what) { std::fprintf(stderr, "    mismatch: %s\n", what.c_str()); }

}  // namespace

int main(int argc, char** argv) {
    const std::string js = load_vectors(argc, argv);
    if (js.empty()) { std::fprintf(stderr, "medium_vectors.json not found\n"); return 2; }
    J v;
    try {
        Parser ps(js);
        v = ps.value();
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        return 2;
    }
    const J& an = v["anchors"];
    const J& fam = v["families"];

    // ── anchors ──────────────────────────────────────────────────────────────────────
    {
        const char* s = "123456789";
        const std::uint8_t z15[15] = {0};
        bool ok = dcf::crc16(reinterpret_cast<const std::uint8_t*>(s), 9) == an["crc_123456789"].u64() &&
                  dcf::crc16(z15, 15) == an["crc_zero15"].u64() &&
                  an["crc_123456789"].u64() == 0x29B1 && an["crc_zero15"].u64() == 0x4EC3 &&
                  an["frame_len"].u64() == dcf::FRAME_SIZE &&
                  an["proto_header_len"].u64() == dcf::medium::PROTO_HEADER_LEN &&
                  an["msg_frame"].u64() == dcf::medium::MSG_FRAME &&
                  an["super_len"].u64() == dcf::SUPER_LEN && an["l2_hdr"].u64() == dcf::medium::L2_HDR &&
                  an["l2_filler"].s == hex(dcf::medium::l2_filler());
        check(ok, "anchors: CRC 0x29B1/0x4EC3, frame 17, proto header 17, MSG_FRAME 12, SuperPack 32, "
                  "l2 hdr 2, l2 filler");
    }

    // ── basis frames ─────────────────────────────────────────────────────────────────
    {
        bool ok = v["basis"].size() > 0;
        for (const J& b : v["basis"].arr) {
            dcf::Frame f;
            f.version = 1;
            f.type = static_cast<std::uint8_t>(b["type"].u64());
            f.seq = static_cast<std::uint16_t>(b["seq"].u64());
            f.src = static_cast<std::uint16_t>(b["src"].u64());
            f.dst = static_cast<std::uint16_t>(b["dst"].u64());
            const Bytes pl = unhex(b["payload"].s);
            std::copy(pl.begin(), pl.end(), f.payload.begin());
            f.ts_us = static_cast<std::uint32_t>(b["ts"].u64());
            const Bytes17 e = f.encode();
            if (hex(e) != b["hex"].s || !dcf::medium::gate(e)) { ok = false; note("basis " + b["hex"].s); }
        }
        check(ok, std::to_string(v["basis"].size()) + " basis frames encode byte-identically + pass the gate");
    }

    // ── stream ───────────────────────────────────────────────────────────────────────
    {
        const J& cases = fam["stream"]["cases"];
        bool dec_ok = cases.size() > 0, chunk_ok = true, rt_ok = true, gate_ok = true;
        for (const J& c : cases.arr) {
            const Bytes in = unhex(c["input"].s);
            const auto want = strs(c["frames"]);
            const auto r = dcf::medium::stream_decode(in);
            if (hexes(r.frames) != want || r.skipped_bytes != c["skipped_bytes"].u64() ||
                r.tail_bytes != c["tail_bytes"].u64()) {
                dec_ok = false; note("stream " + c["name"].s);
            }
            for (const auto& f : r.frames) if (!dcf::medium::gate(f)) gate_ok = false;
            // chunking-invariance law: every chunk size gives the same frames/skipped/tail
            for (std::size_t step = 1; step < 36; ++step) {
                dcf::medium::StreamScanner sc;
                Frames got;
                for (std::size_t i = 0; i < in.size(); i += step) {
                    const std::size_t n = std::min(step, in.size() - i);
                    const Frames part = sc.feed(in.data() + i, n);
                    got.insert(got.end(), part.begin(), part.end());
                }
                if (hexes(got) != want || sc.skipped_bytes() != c["skipped_bytes"].u64() ||
                    sc.frames_out() != want.size() || sc.flush().size() != c["tail_bytes"].u64()) {
                    chunk_ok = false; note("stream chunk " + c["name"].s + " step " + std::to_string(step));
                }
            }
            // lossless: decode(encode(frames)) == frames, nothing skipped, no tail
            const Frames fs = frames_of(c["frames"]);
            const auto rt = dcf::medium::stream_decode(dcf::medium::stream_encode(fs));
            if (hexes(rt.frames) != want || rt.skipped_bytes != 0 || rt.tail_bytes != 0) rt_ok = false;
            if ((c["name"].s == "aligned_1" || c["name"].s == "aligned_3" || c["name"].s == "all_six") &&
                hex(dcf::medium::stream_encode(fs)) != c["input"].s) rt_ok = false;
        }
        const std::string n = std::to_string(cases.size());
        check(dec_ok, n + " stream cases decode byte-identically (frames, skipped_bytes, tail_bytes)");
        check(chunk_ok, "stream scanner chunk-invariant (1..35-byte chunks)");
        check(gate_ok && rt_ok, "stream encode lossless; every decoded frame passes the gate");
    }

    // ── hex ──────────────────────────────────────────────────────────────────────────
    {
        const J& cases = fam["hex"]["cases"];
        bool enc_ok = cases.size() > 0, dec_ok = true, fix_ok = true;
        for (const J& c : cases.arr) {
            const Frames fs = frames_of(c["frames"]);
            if (dcf::medium::hex_encode(fs) != c["text"].s) { enc_ok = false; note("hex encode " + c["name"].s); }
            const auto r = dcf::medium::hex_decode(c["decode_input"].s);
            if (hexes(r.frames) != strs(c["decoded"]) || r.bad_lines != c["bad_lines"].u64()) {
                dec_ok = false; note("hex decode " + c["name"].s);
            }
            const auto canon = dcf::medium::hex_decode(c["text"].s);
            if (hexes(canon.frames) != strs(c["decoded"]) || canon.bad_lines != 0) fix_ok = false;
        }
        const std::string n = std::to_string(cases.size());
        check(enc_ok, n + " hex cases encode byte-identically");
        check(dec_ok, n + " hex cases decode (CRLF/uppercase/comments/blank; bad_lines; ungated)");
        check(fix_ok, "hex canonical text is a decode fixed point");
    }

    // ── udp_proto ────────────────────────────────────────────────────────────────────
    {
        const J& up = fam["udp_proto"];
        const J& ty = up["types"];
        namespace mt = dcf::medium::msg_type;
        bool reg_ok = up["header_len"].u64() == dcf::medium::PROTO_HEADER_LEN &&
                      up["msg_frame"].u64() == dcf::medium::MSG_FRAME && ty.size() == 12 &&
                      ty["POSITION"].u64() == mt::POSITION && ty["AUDIO"].u64() == mt::AUDIO &&
                      ty["GAME_EVENT"].u64() == mt::GAME_EVENT && ty["STATE_SYNC"].u64() == mt::STATE_SYNC &&
                      ty["RELIABLE"].u64() == mt::RELIABLE && ty["ACK"].u64() == mt::ACK &&
                      ty["PING"].u64() == mt::PING && ty["PONG"].u64() == mt::PONG &&
                      ty["GAME_DCF"].u64() == mt::GAME_DCF && ty["TEXT_DCF"].u64() == mt::TEXT_DCF &&
                      ty["MESH"].u64() == mt::MESH && ty["FRAME"].u64() == mt::FRAME;
        check(reg_ok, "udp_proto header 17, msg_type registry 1..12 (FRAME = 12)");

        const J& cases = up["cases"];
        bool enc_ok = cases.size() > 0, dec_ok = true, acc_ok = true;
        std::string golden;
        for (const J& c : cases.arr) {
            const std::uint8_t t = static_cast<std::uint8_t>(c["type"].u64());
            const std::uint32_t seq = static_cast<std::uint32_t>(c["seq"].u64());
            const std::uint64_t ts = std::strtoull(c["ts_hex"].s.c_str(), nullptr, 16);
            if (ts != c["ts"].u64()) { enc_ok = false; note("ts/ts_hex disagree " + c["name"].s); }
            const Bytes pl = unhex(c["payload"].s);
            const Bytes dg = dcf::medium::proto_encode(t, seq, ts, pl);
            if (hex(dg) != c["datagram"].s) { enc_ok = false; note("proto encode " + c["name"].s); }
            const Bytes want_dg = unhex(c["datagram"].s);
            try {
                const auto m = dcf::medium::proto_decode(want_dg);
                if (m.type != t || m.seq != seq || m.ts != ts || m.payload != pl) {
                    dec_ok = false; note("proto decode " + c["name"].s);
                }
            } catch (const std::exception&) { dec_ok = false; note("proto decode threw " + c["name"].s); }
            const auto fr = dcf::medium::proto_frame_decode(want_dg);
            if (fr.has_value() != c["accept_as_frame"].b) { acc_ok = false; note("accept " + c["name"].s); }
            if (fr.has_value()) {
                if (hex(*fr) != c["payload"].s) acc_ok = false;
                if (hex(dcf::medium::proto_frame_encode(*fr, seq, ts)) != c["datagram"].s) acc_ok = false;
            }
            if (c["name"].s == "go_golden") golden = c["datagram"].s;
        }
        const std::string n = std::to_string(cases.size());
        check(enc_ok, n + " udp_proto datagrams encode byte-identically (u64 ts incl. > 2^53)");
        check(dec_ok, n + " udp_proto datagrams decode to (type, seq, ts, payload)");
        check(acc_ok, "accept_as_frame iff type == 12 && len == 17; frame round-trips to 34 B");

        bool guard_ok = !golden.empty();
        const Bytes z16(16, 0);
        Bytes cut = unhex(golden);
        if (!cut.empty()) cut.pop_back();
        for (const Bytes* bad : std::vector<const Bytes*>{&z16, &cut}) {
            bool rejected = false;
            try { dcf::medium::proto_decode(*bad); } catch (const std::invalid_argument&) { rejected = true; }
            if (!rejected || dcf::medium::proto_frame_decode(*bad).has_value()) guard_ok = false;
        }
        check(guard_ok, "udp_proto rejects a short header and a payload_len overrun");
    }

    // ── udp_bare ─────────────────────────────────────────────────────────────────────
    {
        const J& cases = fam["udp_bare"]["cases"];
        bool enc_ok = cases.size() > 0, dec_ok = true, raw_ok = true;
        for (const J& c : cases.arr) {
            const Frames fs = frames_of(c["frames"]);
            const auto dgs = dcf::medium::bare_encode(fs);
            std::vector<std::string> got;
            for (const auto& d : dgs) got.push_back(hex(d));
            if (got != strs(c["datagrams"])) { enc_ok = false; note("bare encode " + c["name"].s); }
            Frames back;
            for (const J& d : c["datagrams"].arr) {
                const Frames part = dcf::medium::bare_decode(unhex(d.s));
                back.insert(back.end(), part.begin(), part.end());
            }
            if (hexes(back) != strs(c["frames"])) { dec_ok = false; note("bare decode " + c["name"].s); }
            const auto raw = dcf::medium::bare_encode(fs, false);
            if (raw.size() != fs.size()) raw_ok = false;
            for (std::size_t i = 0; raw_ok && i < raw.size(); ++i)
                if (hex(raw[i]) != hex(fs[i])) raw_ok = false;
        }
        const std::string n = std::to_string(cases.size());
        check(enc_ok, n + " udp_bare cases encode byte-identically (pairs -> 32-B SuperPack, lone -> 17 B)");
        check(dec_ok && raw_ok, "udp_bare decode lossless; pair=false sends every frame raw");

        bool rej_ok = dcf::medium::bare_decode(Bytes(33, 0)).empty() &&
                      dcf::medium::bare_decode(Bytes(16, 0)).empty();
        for (const J& c : cases.arr) {
            if (c["name"].s != "frames_2") continue;
            Bytes tam = unhex(c["datagrams"][0].s);
            tam[7] ^= 1;
            if (!dcf::medium::bare_decode(tam).empty()) rej_ok = false;
        }
        check(rej_ok, "udp_bare rejects 33-B / 16-B datagrams and a tampered SuperPack");
    }

    // ── l2eth ────────────────────────────────────────────────────────────────────────
    {
        const J& l2 = fam["l2eth"];
        const std::string filler = hex(dcf::medium::l2_filler());
        check(l2["hdr"].u64() == dcf::medium::L2_HDR && l2["filler"].s == filler &&
                  dcf::medium::gate(dcf::medium::l2_filler()),
              "l2eth hdr 2; filler = encode(DATA, 0, 0, 0, 00000000, 0) = " + filler);

        const J& cases = l2["cases"];
        bool enc_ok = cases.size() > 0, dec_ok = true, pad_ok = true, trunc_ok = true, fill_ok = true;
        for (const J& c : cases.arr) {
            const Frames fs = frames_of(c["frames"]);
            const Bytes pl = dcf::medium::l2_batch(fs);
            if (hex(pl) != c["payload"].s) { enc_ok = false; note("l2 batch " + c["name"].s); }
            const Bytes want = unhex(c["payload"].s);
            try {
                if (hexes(dcf::medium::l2_unbatch(want)) != strs(c["frames"])) {
                    dec_ok = false; note("l2 unbatch " + c["name"].s);
                }
                Bytes padded = want;
                padded.insert(padded.end(), 7, 0);
                if (hexes(dcf::medium::l2_unbatch(padded)) != strs(c["frames"])) pad_ok = false;
            } catch (const std::exception&) { dec_ok = false; note("l2 unbatch threw " + c["name"].s); }
            Bytes cut = want;
            cut.pop_back();
            bool rejected = false;
            try { dcf::medium::l2_unbatch(cut); } catch (const std::invalid_argument&) { rejected = true; }
            if (!rejected) trunc_ok = false;
            if (fs.size() % 2) {
                const auto pr = dcf::unpack(want.data() + want.size() - dcf::SUPER_LEN, dcf::SUPER_LEN);
                if (hex(pr.second) != filler) fill_ok = false;
            }
        }
        bool short_ok = false;
        try { dcf::medium::l2_unbatch(Bytes(1, 0)); } catch (const std::invalid_argument&) { short_ok = true; }
        const std::string n = std::to_string(cases.size());
        check(enc_ok, n + " l2eth payloads batch byte-identically ([n u16][SuperPack*ceil(n/2)])");
        check(dec_ok && pad_ok && fill_ok, "l2eth unbatch lossless; filler dropped via n; min-size padding ignored");
        check(trunc_ok && short_ok, "l2eth rejects a truncated / short batch");
        check(dcf::medium::l2_capacity(1500) == 92 && dcf::medium::l2_capacity(9000) == 562 &&
                  dcf::medium::l2_capacity(1) == 0,
              "l2_capacity(1500) = 92, l2_capacity(9000) = 562");
    }

    if (fails) { std::fprintf(stderr, "\n%d CHECK(S) FAILED\n", fails); return 1; }
    std::printf("\nALL MEDIUM VECTORS HOLD — C++ DCF-Medium (stream/hex/udp_proto/udp_bare/l2eth) is cemented.\n");
    return 0;
}
