// SPDX-License-Identifier: LGPL-3.0-only
#ifndef DCF_MEDIUM_HPP
#define DCF_MEDIUM_HPP

// DCF-Medium — the digital medium codecs (Tier B: stream, hex, udp_proto, udp_bare,
// l2eth). Header-only; byte-identical to the canonical Python reference
// python/MCP/mediumlab_core.py and certified against Documentation/medium_vectors.json
// (spec: Documentation/DCF_MEDIUM_SPEC.md).
//
// A medium codec is a deterministic pair
//     encode : [frame]        -> representation
//     decode : representation -> ([frame], diagnostics)
// that carries the 17-byte DeModFrame quantum over one medium WITHOUT parsing it beyond
// the frame gate (sync 0xD3 + version nibble 1 + CRC-16/CCITT-FALSE). Media are
// transports *beneath* the quantum, so the 246-vector wire certificate is untouched.
//
//   stream     .dcf file / stdio: concatenated frames; decode is a byte-wise resync scan
//   hex        34 lowercase hex chars + "\n" per frame; tolerant line parser
//   udp_proto  ProtoMessage envelope [type u8][seq u32][ts u64][len u32][payload] (BE),
//              one frame = msg_type FRAME (12), len 17 -> 34-byte datagram
//   udp_bare   consecutive pairs -> one 32-byte SuperPack, lone trailing frame raw 17 B
//   l2eth      [n_frames u16 BE][SuperPack * ceil(n/2)], odd tail paired with the filler

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "dcf/frame.hpp"
#include "dcf/superpack.hpp"

namespace dcf {
namespace medium {

using Bytes = std::vector<std::uint8_t>;
using Frames = std::vector<Bytes17>;

// ── the frame gate (the existing validity rule; media never parse further) ─────────
/// True iff w is a valid DeModFrame: 17 bytes, sync 0xD3, version nibble 1, and
/// CRC-16/CCITT-FALSE over bytes [0..14] == bytes [15..16] (big-endian).
inline bool gate(const std::uint8_t* w, std::size_t len) {
    if (len != FRAME_SIZE || w[0] != SYNC || (w[1] >> 4) != VERSION) return false;
    return crc16(w, CRC_COVER) == static_cast<std::uint16_t>((w[15] << 8) | w[16]);
}
inline bool gate(const Bytes17& f) { return gate(f.data(), f.size()); }

namespace detail_med {
inline Bytes17 take17(const std::uint8_t* p) {
    Bytes17 f{};
    std::copy(p, p + FRAME_SIZE, f.begin());
    return f;
}

// Byte-wise resync scan of buf[0..n) from offset i. Appends gated frames to out and
// returns the new offset (n - i < 17 on return). At offset i: if the 17-byte window
// passes the gate, emit it and i += 17; else i += 1, skipped += 1.
inline std::size_t scan(const std::uint8_t* buf, std::size_t n, std::size_t i,
                        std::size_t& skipped, Frames& out) {
    while (n >= FRAME_SIZE && i <= n - FRAME_SIZE) {
        if (buf[i] == SYNC && gate(buf + i, FRAME_SIZE)) {
            out.push_back(take17(buf + i));
            i += FRAME_SIZE;
        } else {
            ++i;
            ++skipped;
        }
    }
    return i;
}

inline int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

inline bool is_ws(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\v' || c == '\f';
}

inline void put_be(Bytes& out, std::uint64_t v, int nbytes) {
    for (int k = nbytes - 1; k >= 0; --k) out.push_back(static_cast<std::uint8_t>(v >> (8 * k)));
}

inline std::uint64_t get_be(const std::uint8_t* p, int nbytes) {
    std::uint64_t v = 0;
    for (int k = 0; k < nbytes; ++k) v = (v << 8) | p[k];
    return v;
}
}  // namespace detail_med

// ══ stream (.dcf file, stdio) ═══════════════════════════════════════════════════════
/// Concatenate 17-byte frames (the .dcf / stdio representation).
inline Bytes stream_encode(const Frames& frames) {
    Bytes out;
    out.reserve(frames.size() * FRAME_SIZE);
    for (const auto& f : frames) out.insert(out.end(), f.begin(), f.end());
    return out;
}

struct StreamDecoded {
    Frames frames;
    std::size_t skipped_bytes = 0;  // offsets rejected by the gate
    std::size_t tail_bytes = 0;     // trailing < 17 bytes that can never hold a frame
};

/// Byte-wise resync decode of a whole stream.
inline StreamDecoded stream_decode(const std::uint8_t* buf, std::size_t n) {
    StreamDecoded r;
    const std::size_t i = detail_med::scan(buf, n, 0, r.skipped_bytes, r.frames);
    r.tail_bytes = n - i;
    return r;
}
inline StreamDecoded stream_decode(const Bytes& buf) { return stream_decode(buf.data(), buf.size()); }

/// Incremental stream decoder. feed() returns the frames completed so far and keeps a
/// carry of at most 16 bytes, so any chunking yields exactly stream_decode()'s frames
/// and skipped_bytes; flush() returns (and clears) the final tail bytes.
class StreamScanner {
public:
    Frames feed(const std::uint8_t* chunk, std::size_t n) {
        carry_.insert(carry_.end(), chunk, chunk + n);
        Frames out;
        const std::size_t i = detail_med::scan(carry_.data(), carry_.size(), 0, skipped_, out);
        carry_.erase(carry_.begin(), carry_.begin() + static_cast<std::ptrdiff_t>(i));
        frames_out_ += out.size();
        return out;
    }
    Frames feed(const Bytes& chunk) { return feed(chunk.data(), chunk.size()); }

    Bytes flush() {
        Bytes tail;
        tail.swap(carry_);
        return tail;
    }

    std::size_t skipped_bytes() const { return skipped_; }
    std::size_t frames_out() const { return frames_out_; }
    /// Bytes currently carried (a possible frame prefix), <= 16.
    std::size_t pending() const { return carry_.size(); }

private:
    Bytes carry_;
    std::size_t skipped_ = 0;
    std::size_t frames_out_ = 0;
};

// ══ hex (text lines) ═════════════════════════════════════════════════════════════════
/// One frame per line: 34 lowercase hex characters + "\n".
inline std::string hex_encode(const Frames& frames) {
    static const char* d = "0123456789abcdef";
    std::string s;
    s.reserve(frames.size() * (2 * FRAME_SIZE + 1));
    for (const auto& f : frames) {
        for (std::uint8_t b : f) {
            s += d[b >> 4];
            s += d[b & 0xF];
        }
        s += '\n';
    }
    return s;
}

struct HexDecoded {
    Frames frames;
    std::size_t bad_lines = 0;
};

/// Parse hex lines. Lines are split on "\n"; leading/trailing space, tab, CR, VT, FF are
/// stripped; blank lines and lines starting with "#" are skipped (not counted); uppercase
/// is accepted; any other line that is not exactly 34 hex digits is a bad line. Frames
/// are returned raw (NOT gated — the caller gates).
inline HexDecoded hex_decode(const std::string& text) {
    HexDecoded r;
    std::size_t pos = 0;
    for (;;) {
        const std::size_t nl = text.find('\n', pos);
        const std::size_t end = (nl == std::string::npos) ? text.size() : nl;
        std::size_t a = pos, b = end;
        while (a < b && detail_med::is_ws(text[a])) ++a;
        while (b > a && detail_med::is_ws(text[b - 1])) --b;
        if (a < b && text[a] != '#') {
            bool good = (b - a) == 2 * FRAME_SIZE;
            Bytes17 f{};
            for (std::size_t k = 0; good && k < FRAME_SIZE; ++k) {
                const int hi = detail_med::hexval(text[a + 2 * k]);
                const int lo = detail_med::hexval(text[a + 2 * k + 1]);
                if (hi < 0 || lo < 0) good = false;
                else f[k] = static_cast<std::uint8_t>((hi << 4) | lo);
            }
            if (good) r.frames.push_back(f);
            else ++r.bad_lines;
        }
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    return r;
}

// ══ udp dialect "proto" (ProtoMessage envelope) ══════════════════════════════════════
// Byte-identical to python/dcf/proto.py, go/node/proto.go, rust/src/lib.rs,
// C_SDK/node/dcf_proto.h:
//   [0] msg_type u8 | [1:5] sequence u32 | [5:13] timestamp u64 | [13:17] payload_len u32
constexpr std::size_t PROTO_HEADER_LEN = 17;
constexpr std::uint8_t MSG_FRAME = 12;

namespace msg_type {
constexpr std::uint8_t POSITION = 1;
constexpr std::uint8_t AUDIO = 2;
constexpr std::uint8_t GAME_EVENT = 3;
constexpr std::uint8_t STATE_SYNC = 4;
constexpr std::uint8_t RELIABLE = 5;
constexpr std::uint8_t ACK = 6;
constexpr std::uint8_t PING = 7;
constexpr std::uint8_t PONG = 8;
constexpr std::uint8_t GAME_DCF = 9;
constexpr std::uint8_t TEXT_DCF = 10;
constexpr std::uint8_t MESH = 11;
constexpr std::uint8_t FRAME = 12;
}  // namespace msg_type

struct ProtoMessage {
    std::uint8_t type = 0;
    std::uint32_t seq = 0;
    std::uint64_t ts = 0;
    Bytes payload;
};

/// Serialize one ProtoMessage (17-byte big-endian header + payload).
inline Bytes proto_encode(std::uint8_t type, std::uint32_t seq, std::uint64_t ts,
                          const std::uint8_t* payload, std::size_t len) {
    if (static_cast<std::uint64_t>(len) > 0xFFFFFFFFull)
        throw std::invalid_argument("medium: payload too long for u32 length");
    Bytes out;
    out.reserve(PROTO_HEADER_LEN + len);
    out.push_back(type);
    detail_med::put_be(out, seq, 4);
    detail_med::put_be(out, ts, 8);
    detail_med::put_be(out, static_cast<std::uint64_t>(len), 4);
    out.insert(out.end(), payload, payload + len);
    return out;
}
inline Bytes proto_encode(std::uint8_t type, std::uint32_t seq, std::uint64_t ts, const Bytes& payload) {
    return proto_encode(type, seq, ts, payload.data(), payload.size());
}

/// Parse one ProtoMessage. Throws std::invalid_argument on a short header or a
/// payload_len that overruns the datagram. Bytes after payload_len are ignored.
inline ProtoMessage proto_decode(const std::uint8_t* dg, std::size_t len) {
    if (len < PROTO_HEADER_LEN) throw std::invalid_argument("medium: message shorter than 17-byte header");
    ProtoMessage m;
    m.type = dg[0];
    m.seq = static_cast<std::uint32_t>(detail_med::get_be(dg + 1, 4));
    m.ts = detail_med::get_be(dg + 5, 8);
    const std::uint64_t plen = detail_med::get_be(dg + 13, 4);
    if (static_cast<std::uint64_t>(len - PROTO_HEADER_LEN) < plen)
        throw std::invalid_argument("medium: payload length exceeds message size");
    m.payload.assign(dg + PROTO_HEADER_LEN, dg + PROTO_HEADER_LEN + static_cast<std::size_t>(plen));
    return m;
}
inline ProtoMessage proto_decode(const Bytes& dg) { return proto_decode(dg.data(), dg.size()); }

/// One frame as a 34-byte ProtoMessage(MSG_FRAME, seq, ts, len 17, frame).
inline Bytes proto_frame_encode(const Bytes17& frame, std::uint32_t seq, std::uint64_t ts = 0) {
    return proto_encode(MSG_FRAME, seq, ts, frame.data(), frame.size());
}

/// The carried frame, or nullopt unless msg_type == 12 and payload_len == 17 (types
/// 1..11 are adapter envelopes, not frames on this medium). Not gated.
inline std::optional<Bytes17> proto_frame_decode(const std::uint8_t* dg, std::size_t len) {
    ProtoMessage m;
    try {
        m = proto_decode(dg, len);
    } catch (const std::invalid_argument&) {
        return std::nullopt;
    }
    if (m.type != MSG_FRAME || m.payload.size() != FRAME_SIZE) return std::nullopt;
    return detail_med::take17(m.payload.data());
}
inline std::optional<Bytes17> proto_frame_decode(const Bytes& dg) {
    return proto_frame_decode(dg.data(), dg.size());
}

// ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ═══════════════════
/// Datagrams for a frame sequence: with pair, consecutive pairs become one 32-byte
/// SuperPack and a lone trailing frame goes raw (17 B); without, every frame goes raw.
/// Throws std::invalid_argument if a frame to be paired fails the SuperPack checks.
inline std::vector<Bytes> bare_encode(const Frames& frames, bool pair = true) {
    std::vector<Bytes> out;
    std::size_t i = 0;
    if (pair) {
        for (; i + 1 < frames.size(); i += 2) {
            const Bytes32 sp = pack(frames[i], frames[i + 1]);
            out.emplace_back(sp.begin(), sp.end());
        }
    }
    for (; i < frames.size(); ++i) out.emplace_back(frames[i].begin(), frames[i].end());
    return out;
}

/// Frames carried by one bare datagram: a valid 32-byte SuperPack -> its 2 frames; a
/// 17-byte datagram -> [it] (not gated); anything else -> [].
inline Frames bare_decode(const std::uint8_t* dg, std::size_t len) {
    Frames out;
    if (len == SUPER_LEN && is_superpack(dg, len)) {
        try {
            const auto pr = unpack(dg, len);
            out.push_back(pr.first);
            out.push_back(pr.second);
        } catch (const std::exception&) {
            out.clear();
        }
        return out;
    }
    if (len == FRAME_SIZE) out.push_back(detail_med::take17(dg));
    return out;
}
inline Frames bare_decode(const Bytes& dg) { return bare_decode(dg.data(), dg.size()); }

// ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ═
constexpr std::size_t L2_HDR = 2;  // the n_frames u16

/// The canonical zero filler frame (a valid DATA DeModFrame, all application fields 0):
/// pairs an odd trailing frame; the receiver discards it using n_frames.
inline Bytes17 l2_filler() {
    Frame z;
    z.version = VERSION;
    z.type = 0;
    return z.encode();
}

/// Number of DeModFrames that fit one Ethernet payload of the given MTU.
inline std::size_t l2_capacity(std::size_t mtu) {
    if (mtu < L2_HDR) return 0;
    return ((mtu - L2_HDR) / SUPER_LEN) * 2;
}

/// Batch frames into one Ethernet payload: [n_frames u16 BE][SuperPack * ceil(n/2)],
/// odd tail paired with l2_filler().
inline Bytes l2_batch(const Frames& frames) {
    const std::size_t n = frames.size();
    if (n > 0xFFFF) throw std::invalid_argument("medium: too many frames for one batch");
    Bytes out;
    out.reserve(L2_HDR + ((n + 1) / 2) * SUPER_LEN);
    detail_med::put_be(out, n, 2);
    const Bytes17 filler = l2_filler();
    for (std::size_t i = 0; i < n; i += 2) {
        const Bytes32 sp = pack(frames[i], (i + 1 < n) ? frames[i + 1] : filler);
        out.insert(out.end(), sp.begin(), sp.end());
    }
    return out;
}

/// Split an Ethernet payload back into its frames (bit-exact). Throws
/// std::invalid_argument on a short/truncated batch or a corrupt SuperPack; trailing
/// bytes after the last SuperPack (Ethernet minimum-size padding) are ignored.
inline Frames l2_unbatch(const std::uint8_t* buf, std::size_t len) {
    if (len < L2_HDR) throw std::invalid_argument("medium: short batch");
    const std::size_t n = static_cast<std::size_t>((buf[0] << 8) | buf[1]);
    const std::size_t npairs = (n + 1) / 2;
    if (len < L2_HDR + npairs * SUPER_LEN) throw std::invalid_argument("medium: truncated batch");
    Frames out;
    out.reserve(n);
    std::size_t off = L2_HDR;
    for (std::size_t p = 0; p < npairs; ++p, off += SUPER_LEN) {
        const auto pr = unpack(buf + off, SUPER_LEN);
        out.push_back(pr.first);
        if (out.size() < n) out.push_back(pr.second);
    }
    return out;
}
inline Frames l2_unbatch(const Bytes& buf) { return l2_unbatch(buf.data(), buf.size()); }

}  // namespace medium
}  // namespace dcf

#endif  // DCF_MEDIUM_HPP
