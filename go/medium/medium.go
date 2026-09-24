// SPDX-License-Identifier: LGPL-3.0-only

// Package medium implements the DCF-Medium reference codecs in Go, byte-identical to the
// canonical python/MCP/mediumlab_core.py and certified against
// Documentation/medium_vectors.json (spec: Documentation/DCF_MEDIUM_SPEC.md).
//
// A medium codec is a deterministic pair
//
//	encode : [frame]        -> representation
//	decode : representation -> ([frame], diagnostics)
//
// that carries the 17-byte DeModFrame quantum over one medium WITHOUT parsing it beyond
// the frame gate (sync 0xD3 + version nibble 1 + CRC-16/CCITT-FALSE). Media are transports
// beneath the quantum, so the 246-vector wire certificate is untouched.
//
// Families (all byte-certified; the analog ones to their symbol / bit stream):
//
//	stream         .dcf file / stdio — concatenated frames, byte-wise resync on decode
//	hex            34 lowercase hex chars + "\n" per frame
//	udp_proto      ProtoMessage envelope ([type u8][seq u32][ts u64][len u32]), type FRAME=12
//	udp_bare       bare 17-B frame or a 32-B SuperPack pair per datagram
//	l2eth          [n u16 BE][SuperPack * ceil(n/2)] Ethernet payload (DCF-Snake raw L2)
//	hydra_symbols  HydraModem M-FSK tone-index stream (port of hydramodem/src/hydra_*.c)
//	afsk_bits      python/modem AFSK on-air bit stream (acoustic_frame.encode_bits)
//
// hydra and afsk are two DIFFERENT acoustic media (different tones, sync word and FEC);
// they do not interoperate with each other.
package medium

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"math/bits"
	"strings"

	"github.com/ALH477/Punctim/go/dcf"
)

// FrameLen is the size of one DeModFrame (the wire quantum).
const FrameLen = dcf.FrameSize

// Errors.
var (
	ErrFrameLen       = errors.New("medium: need 17-byte frames")
	ErrShortMessage   = errors.New("medium: message shorter than 17-byte header")
	ErrPayloadOverrun = errors.New("medium: payload length exceeds message size")
	ErrShortBatch     = errors.New("medium: short l2 batch")
	ErrTruncatedBatch = errors.New("medium: truncated l2 batch")
	ErrTooManyFrames  = errors.New("medium: too many frames for one l2 batch")
)

// Gate reports whether frame is a valid DeModFrame: 17 bytes, sync 0xD3, version nibble 1,
// CRC-16/CCITT-FALSE over bytes [0..15) == bytes [15..17) (big-endian). This is the
// existing validity rule; media never parse further.
func Gate(frame []byte) bool {
	return len(frame) == FrameLen && frame[0] == dcf.SyncByte && frame[1]>>4 == dcf.Version &&
		dcf.Crc16CCITT(frame[:15]) == uint16(frame[15])<<8|uint16(frame[16])
}

func clone(b []byte) []byte { return append([]byte(nil), b...) }

// ══ stream (.dcf file, stdio) ═════════════════════════════════════════════════

// StreamEncode concatenates 17-byte frames (the .dcf / stdio representation). It panics
// on a frame that is not exactly 17 bytes (a programming error, like the Python ValueError).
func StreamEncode(frames [][]byte) []byte {
	out := make([]byte, 0, len(frames)*FrameLen)
	for _, f := range frames {
		if len(f) != FrameLen {
			panic(ErrFrameLen)
		}
		out = append(out, f...)
	}
	return out
}

// scan is the byte-wise resync scan of buf from offset i. It appends gated frames and
// returns (i, skipped) with len(buf)-i < 17. At offset i: if the 17-byte window passes the
// gate, emit it and i += 17; else i += 1, skipped += 1.
func scan(buf []byte, i, skipped int, frames [][]byte) (int, int, [][]byte) {
	last := len(buf) - FrameLen // highest offset with a full window
	for i <= last {
		if buf[i] != dcf.SyncByte {
			// fast-forward to the next 0xD3 that still has a full window (a pure speed-up:
			// every skipped offset would fail the gate's sync test anyway)
			j := bytes.IndexByte(buf[i+1:last+1], dcf.SyncByte)
			if j < 0 {
				skipped += last + 1 - i
				i = last + 1
				break
			}
			skipped += j + 1
			i += j + 1
			continue
		}
		w := buf[i : i+FrameLen]
		if Gate(w) {
			frames = append(frames, clone(w))
			i += FrameLen
		} else {
			i++
			skipped++
		}
	}
	return i, skipped, frames
}

// StreamDecode is the byte-wise resync decode of a stream. skipped counts offsets rejected
// by the gate; the trailing < 17 bytes that can never hold a frame are tail (not skipped).
func StreamDecode(buf []byte) (frames [][]byte, skipped, tail int) {
	i, sk, fr := scan(buf, 0, 0, nil)
	return fr, sk, len(buf) - i
}

// StreamScanner is the incremental stream decoder. Feed returns the frames completed so
// far and keeps a carry of at most 16 bytes, so any chunking yields exactly StreamDecode's
// frames and skipped count; Flush returns (and clears) the final tail bytes.
type StreamScanner struct {
	carry     []byte
	skipped   int
	framesOut int
}

// Feed consumes one chunk and returns the frames it completed.
func (s *StreamScanner) Feed(chunk []byte) [][]byte {
	buf := append(s.carry, chunk...)
	i, sk, frames := scan(buf, 0, s.skipped, nil)
	s.skipped = sk
	s.carry = clone(buf[i:])
	s.framesOut += len(frames)
	return frames
}

// Skipped is the running count of byte offsets rejected by the gate.
func (s *StreamScanner) Skipped() int { return s.skipped }

// FramesOut is the running count of frames emitted.
func (s *StreamScanner) FramesOut() int { return s.framesOut }

// Pending is the number of bytes currently carried (a possible frame prefix), <= 16.
func (s *StreamScanner) Pending() int { return len(s.carry) }

// Flush returns and clears the carried tail bytes.
func (s *StreamScanner) Flush() []byte {
	t := s.carry
	s.carry = nil
	return t
}

// ══ hex (text lines) ══════════════════════════════════════════════════════════

// HexEncode renders one frame per line: 34 lowercase hex characters + "\n". It panics on
// a frame that is not exactly 17 bytes.
func HexEncode(frames [][]byte) string {
	var sb strings.Builder
	for _, f := range frames {
		if len(f) != FrameLen {
			panic(ErrFrameLen)
		}
		sb.WriteString(hex.EncodeToString(f))
		sb.WriteByte('\n')
	}
	return sb.String()
}

// hexWS is the ASCII whitespace stripped from each hex line (space, tab, CR, VT, FF).
const hexWS = " \t\r\v\f"

func isHexDigit(c byte) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
}

// HexDecodeLine parses one line (without its "\n"). It returns (frame, false) for a frame
// line, (nil, false) for a skipped blank or "#" comment line, and (nil, true) for a bad
// line. The frame is returned raw (NOT gated — the caller gates).
func HexDecodeLine(line string) (frame []byte, bad bool) {
	s := strings.Trim(line, hexWS)
	if s == "" || s[0] == '#' {
		return nil, false
	}
	if len(s) != 2*FrameLen {
		return nil, true
	}
	for i := 0; i < len(s); i++ {
		if !isHexDigit(s[i]) {
			return nil, true
		}
	}
	b, err := hex.DecodeString(s)
	if err != nil {
		return nil, true
	}
	return b, false
}

// HexDecode parses hex lines -> (frames, badLines). Lines are split on "\n"; leading and
// trailing ASCII whitespace (space, tab, CR, VT, FF) is stripped; blank lines and lines
// starting with "#" are skipped; uppercase is accepted; any other line that is not exactly
// 34 hex digits counts as a bad line. Frames are returned raw (NOT gated).
func HexDecode(text string) (frames [][]byte, badLines int) {
	for _, line := range strings.Split(text, "\n") {
		f, bad := HexDecodeLine(line)
		if bad {
			badLines++
		} else if f != nil {
			frames = append(frames, f)
		}
	}
	return frames, badLines
}

// ══ udp dialect "proto" (ProtoMessage envelope) ══════════════════════════════
// Byte-identical to python/dcf/proto.py, go/node/proto.go, rust/src/lib.rs and
// C_SDK/node/dcf_proto.h:
//
//	[0] msg_type u8 | [1:5] sequence u32 | [5:13] timestamp u64 | [13:17] payload_len u32 | payload

// ProtoHeaderLen is the fixed ProtoMessage header size (type + seq + ts + len).
const ProtoHeaderLen = 1 + 4 + 8 + 4 // 17

// Message types (mirrors go/node/proto.go and the Python MSG_TYPES registry).
const (
	MsgPosition  uint8 = 1
	MsgAudio     uint8 = 2
	MsgGameEvent uint8 = 3
	MsgStateSync uint8 = 4
	MsgReliable  uint8 = 5
	MsgAck       uint8 = 6
	MsgPing      uint8 = 7
	MsgPong      uint8 = 8
	MsgGameDCF   uint8 = 9
	MsgTextDCF   uint8 = 10
	MsgMesh      uint8 = 11
	MsgFrame     uint8 = 12 // one raw 17-byte DeModFrame: the udp:dialect=proto medium
)

// MsgTypes is the name -> id registry (the udp_proto family's "types" table).
var MsgTypes = map[string]uint8{
	"POSITION": MsgPosition, "AUDIO": MsgAudio, "GAME_EVENT": MsgGameEvent,
	"STATE_SYNC": MsgStateSync, "RELIABLE": MsgReliable, "ACK": MsgAck, "PING": MsgPing,
	"PONG": MsgPong, "GAME_DCF": MsgGameDCF, "TEXT_DCF": MsgTextDCF, "MESH": MsgMesh,
	"FRAME": MsgFrame,
}

// ProtoEncode serialises one ProtoMessage (17-byte big-endian header + payload).
func ProtoEncode(msgType uint8, seq uint32, ts uint64, payload []byte) []byte {
	out := make([]byte, ProtoHeaderLen+len(payload))
	out[0] = msgType
	binary.BigEndian.PutUint32(out[1:5], seq)
	binary.BigEndian.PutUint64(out[5:13], ts)
	binary.BigEndian.PutUint32(out[13:17], uint32(len(payload)))
	copy(out[ProtoHeaderLen:], payload)
	return out
}

// ProtoDecode parses one ProtoMessage, with the Go/Rust/C guards: a short header or a
// payload_len that overruns the datagram is an error. Bytes after payload_len are ignored.
func ProtoDecode(d []byte) (msgType uint8, seq uint32, ts uint64, payload []byte, err error) {
	if len(d) < ProtoHeaderLen {
		return 0, 0, 0, nil, ErrShortMessage
	}
	plen := uint64(binary.BigEndian.Uint32(d[13:17]))
	if uint64(len(d)) < ProtoHeaderLen+plen {
		return 0, 0, 0, nil, ErrPayloadOverrun
	}
	return d[0], binary.BigEndian.Uint32(d[1:5]), binary.BigEndian.Uint64(d[5:13]),
		clone(d[ProtoHeaderLen : ProtoHeaderLen+int(plen)]), nil
}

// ProtoFrameEncode wraps one frame as a 34-byte ProtoMessage(MsgFrame, seq, ts, len 17).
// It panics on a frame that is not exactly 17 bytes.
func ProtoFrameEncode(frame []byte, seq uint32, ts uint64) []byte {
	if len(frame) != FrameLen {
		panic(ErrFrameLen)
	}
	return ProtoEncode(MsgFrame, seq, ts, frame)
}

// ProtoFrameDecode returns the carried frame, or (nil, false) unless msg_type == 12 and
// payload_len == 17 (types 1..11 are adapter envelopes, not frames on this medium). The
// frame is NOT gated — the caller gates.
func ProtoFrameDecode(d []byte) ([]byte, bool) {
	t, _, _, payload, err := ProtoDecode(d)
	if err != nil || t != MsgFrame || len(payload) != FrameLen {
		return nil, false
	}
	return payload, true
}

// ══ udp dialect "bare" (17-B frame / 32-B SuperPack per datagram) ═════════════

// BareEncode returns the datagrams for a frame sequence: with pair, consecutive pairs
// become one 32-byte SuperPack and a lone trailing frame goes raw (17 B); without pair,
// every frame goes raw. A pair containing a frame that fails the gate cannot be packed;
// both of its frames then go raw, in order (only reachable with ungated input).
func BareEncode(frames [][]byte, pair bool) [][]byte {
	var out [][]byte
	if !pair {
		for _, f := range frames {
			out = append(out, clone(f))
		}
		return out
	}
	i := 0
	for ; i+1 < len(frames); i += 2 {
		sp, err := dcf.PackSuper(frames[i], frames[i+1])
		if err != nil {
			out = append(out, clone(frames[i]), clone(frames[i+1]))
			continue
		}
		out = append(out, sp[:])
	}
	if i < len(frames) {
		out = append(out, clone(frames[i]))
	}
	return out
}

// BareDecode returns the frames carried by one bare datagram: a valid 32-byte SuperPack ->
// its 2 frames; a 17-byte datagram -> [it] (not gated — the caller gates); anything else ->
// nil.
func BareDecode(d []byte) [][]byte {
	if len(d) == dcf.SuperSize && dcf.IsSuperPack(d) {
		a, b, err := dcf.UnpackSuper(d)
		if err != nil {
			return nil
		}
		return [][]byte{clone(a[:]), clone(b[:])}
	}
	if len(d) == FrameLen {
		return [][]byte{clone(d)}
	}
	return nil
}

// ══ l2eth (raw-L2 Ethernet payload; byte-identical to hydramodem/dcf-tools/snake_l2.h) ═

// L2Hdr is the size of the n_frames u16 header.
const L2Hdr = 2

// L2Filler is the canonical zero filler frame (a valid DATA DeModFrame with all application
// fields 0); it pairs an odd trailing frame and the receiver discards it using n_frames.
var L2Filler = func() []byte {
	f := dcf.Frame{Version: dcf.Version, Type: dcf.FData}.Encode()
	return f[:]
}()

// L2Capacity is the number of DeModFrames that fit one Ethernet payload of the given MTU.
func L2Capacity(mtu int) int {
	if mtu < L2Hdr {
		return 0
	}
	return ((mtu - L2Hdr) / dcf.SuperSize) * 2
}

// L2Batch batches 17-byte DeModFrames into one Ethernet payload:
// [n_frames u16 BE][SuperPack * ceil(n/2)], an odd tail paired with L2Filler.
func L2Batch(frames [][]byte) ([]byte, error) {
	n := len(frames)
	if n > 0xFFFF {
		return nil, ErrTooManyFrames
	}
	out := make([]byte, L2Hdr, L2Hdr+((n+1)/2)*dcf.SuperSize)
	binary.BigEndian.PutUint16(out, uint16(n))
	for i := 0; i < n; i += 2 {
		b := L2Filler
		if i+1 < n {
			b = frames[i+1]
		}
		sp, err := dcf.PackSuper(frames[i], b)
		if err != nil {
			return nil, err
		}
		out = append(out, sp[:]...)
	}
	return out, nil
}

// L2Unbatch splits an Ethernet payload back into 17-byte DeModFrames (bit-exact). It fails
// on a short/truncated batch or a corrupt SuperPack; trailing bytes after the last
// SuperPack (Ethernet minimum-size padding) are ignored.
func L2Unbatch(buf []byte) ([][]byte, error) {
	if len(buf) < L2Hdr {
		return nil, ErrShortBatch
	}
	n := int(binary.BigEndian.Uint16(buf))
	npairs := (n + 1) / 2
	if len(buf) < L2Hdr+npairs*dcf.SuperSize {
		return nil, ErrTruncatedBatch
	}
	frames := make([][]byte, 0, n)
	off := L2Hdr
	for k := 0; k < npairs; k++ {
		a, b, err := dcf.UnpackSuper(buf[off : off+dcf.SuperSize])
		if err != nil {
			return nil, err
		}
		off += dcf.SuperSize
		frames = append(frames, clone(a[:]))
		if len(frames) < n {
			frames = append(frames, clone(b[:]))
		}
	}
	return frames, nil
}

// ══ hydra_symbols (HydraModem M-FSK; port of hydramodem/src/hydra_{profile,frame,conv,
//    fec,interleave,crc}.c — the C is the ground truth) ═══════════════════════════

// HydraModem framing constants.
const (
	HydraDCFBytes  = 17
	HydraCRCBytes  = 2
	HydraSyncBits  = 16
	HydraDataBits  = (HydraDCFBytes + HydraCRCBytes) * 8 // 152
	HydraConvMem   = 6
	hydraConvState = 64
	hydraG0        = 0x79 // 0171 octal; bit6 = newest tap
	hydraG1        = 0x5B // 0133 octal
	HydraSyncWord  = 0x2DD4
)

// HydraModem FEC modes (the C hydra_fec_mode enum order).
const (
	FecNone = 0
	FecRep3 = 1
	FecConv = 2
)

// FecNames maps the URI / vector names to FEC mode ids.
var FecNames = map[string]int{"none": FecNone, "rep3": FecRep3, "conv": FecConv}

// FecName returns the name of a FEC mode id ("none"|"rep3"|"conv").
func FecName(m int) string {
	for k, v := range FecNames {
		if v == m {
			return k
		}
	}
	return fmt.Sprintf("fec%d", m)
}

// HydraProfile mirrors hydra_profile: the user fields plus the fields Init derives exactly
// as hydra_profile_init().
type HydraProfile struct {
	Name         string
	SampleRate   float64
	Baud         float64
	NTones       int
	BaseFreq     float64
	ToneSpacing  float64
	PreambleSyms int
	SyncWord     uint16
	FecMode      int
	Interleave   bool
	TxGain       float64

	// derived by Init
	BitsPerSymbol    int
	SamplesPerSymbol int
	LpCut            float64
	DataBits         int
	CodedBits        int
	InterleaveStride int
	SyncSyms         int
	DataSyms         int
	TotalSyms        int
}

// HydraProfileDefault is hydra_profile_default(): 48 kHz, 1000 baud, 2 tones at
// 2000/3000 Hz, 24-symbol preamble, sync 0x2DD4, conv FEC, interleave on. (Not yet Init'd.)
func HydraProfileDefault() HydraProfile {
	return HydraProfile{Name: "default", SampleRate: 48000, Baud: 1000, NTones: 2,
		BaseFreq: 2000, ToneSpacing: 1000, PreambleSyms: 24, SyncWord: HydraSyncWord,
		FecMode: FecConv, Interleave: true, TxGain: 0.9}
}

// HydraProfileAux is hydra_profile_aux_cable(): 1200 baud, tones 1200/2400 Hz, 16-symbol
// preamble, conv FEC, interleave on. (Not yet Init'd.)
func HydraProfileAux() HydraProfile {
	return HydraProfile{Name: "aux", SampleRate: 48000, Baud: 1200, NTones: 2,
		BaseFreq: 1200, ToneSpacing: 1200, PreambleSyms: 16, SyncWord: HydraSyncWord,
		FecMode: FecConv, Interleave: true, TxGain: 0.9}
}

// HydraProfileByName returns the named profile ("default"|"aux"), not yet Init'd.
func HydraProfileByName(name string) (HydraProfile, error) {
	switch name {
	case "default":
		return HydraProfileDefault(), nil
	case "aux":
		return HydraProfileAux(), nil
	}
	return HydraProfile{}, fmt.Errorf("medium: unknown hydra profile %q (default|aux)", name)
}

// HydraConvCodedLen is the conv-coded length of n message bits (tail-flushed).
func HydraConvCodedLen(nMsg int) int { return 2 * (nMsg + HydraConvMem) }

func gcd(a, b int) int {
	for b != 0 {
		a, b = b, a%b
	}
	return a
}

// HydraInterleaveStride is the deterministic coprime stride ~ sqrt(n) of hydra_interleave.c.
func HydraInterleaveStride(n int) int {
	if n < 3 {
		return 1
	}
	s := 1
	for s*s < n {
		s++
	}
	if s >= n {
		s = n - 1
	}
	for s < n && gcd(s, n) != 1 {
		s++
	}
	if s >= n {
		s = 2
		for s < n && gcd(s, n) != 1 {
			s++
		}
		if s >= n {
			s = 1
		}
	}
	return s
}

// Init validates the user fields and computes every derived field exactly as
// hydra_profile_init(). It returns an error on a config hydra_profile_init rejects.
func (p *HydraProfile) Init() error {
	if p.SampleRate <= 0 || p.Baud <= 0 {
		return errors.New("medium: hydra sample_rate/baud must be > 0")
	}
	if p.ToneSpacing <= 0 || p.BaseFreq <= 0 {
		return errors.New("medium: hydra base_freq/tone_spacing must be > 0")
	}
	if p.PreambleSyms < 0 {
		return errors.New("medium: hydra preamble_syms must be >= 0")
	}
	nt := p.NTones
	if nt < 2 || nt&(nt-1) != 0 {
		return errors.New("medium: hydra n_tones must be a power of two >= 2")
	}
	if p.FecMode != FecNone && p.FecMode != FecRep3 && p.FecMode != FecConv {
		return fmt.Errorf("medium: unknown hydra fec mode %d", p.FecMode)
	}
	bps := bits.Len(uint(nt)) - 1
	p.BitsPerSymbol = bps
	p.SamplesPerSymbol = int(p.SampleRate/p.Baud + 0.5)
	if p.SamplesPerSymbol < 2 {
		return errors.New("medium: hydra samples_per_symbol < 2")
	}
	p.LpCut = p.Baud * 0.5
	p.DataBits = HydraDataBits
	switch p.FecMode {
	case FecNone:
		p.CodedBits = HydraDataBits
	case FecRep3:
		p.CodedBits = 3 * HydraDataBits
	default:
		p.CodedBits = HydraConvCodedLen(HydraDataBits)
	}
	p.InterleaveStride = 1
	if p.Interleave {
		p.InterleaveStride = HydraInterleaveStride(p.CodedBits)
	}
	p.SyncSyms = (HydraSyncBits + bps - 1) / bps
	p.DataSyms = (p.CodedBits + bps - 1) / bps
	p.TotalSyms = p.PreambleSyms + p.SyncSyms + p.DataSyms
	if p.BaseFreq+float64(nt-1)*p.ToneSpacing >= 0.5*p.SampleRate {
		return errors.New("medium: hydra highest tone at/above Nyquist")
	}
	for _, v := range []float64{p.BaseFreq, p.ToneSpacing} {
		c := v / p.Baud
		if d := c - float64(int(c+0.5)); d > 1e-6 || d < -1e-6 {
			return errors.New("medium: hydra base_freq/tone_spacing must be integer multiples of baud")
		}
	}
	return nil
}

// ---- bit / byte / symbol helpers (MSB-first) ----

func hydraBytesToBits(data []byte) []byte {
	out := make([]byte, 0, len(data)*8)
	for _, b := range data {
		for i := 7; i >= 0; i-- {
			out = append(out, (b>>uint(i))&1)
		}
	}
	return out
}

func hydraBitsToBytes(bs []byte) []byte {
	out := make([]byte, 0, len(bs)/8)
	for i := 0; i+8 <= len(bs); i += 8 {
		var v byte
		for j := 0; j < 8; j++ {
			v = v<<1 | (bs[i+j] & 1)
		}
		out = append(out, v)
	}
	return out
}

// hydraBitsToSymbols packs bits into symbols MSB-first; the final symbol is zero-padded.
func hydraBitsToSymbols(bs []byte, bps int) []int {
	n := len(bs)
	nsym := (n + bps - 1) / bps
	out := make([]int, nsym)
	for s := 0; s < nsym; s++ {
		v := 0
		for b := 0; b < bps; b++ {
			idx := s*bps + b
			bit := 0
			if idx < n {
				bit = int(bs[idx] & 1)
			}
			v = v<<1 | bit
		}
		out[s] = v
	}
	return out
}

func hydraSymbolsToBits(syms []int, bps int) []byte {
	out := make([]byte, 0, len(syms)*bps)
	for _, s := range syms {
		for b := 0; b < bps; b++ {
			out = append(out, byte((s>>uint(bps-1-b))&1))
		}
	}
	return out
}

// ---- FEC: K=7 r=1/2 convolutional (G0=0171, G1=0133, 6 zero tail bits) ----

func parity7(v int) byte {
	return byte(bits.OnesCount8(uint8(v&0x7F)) & 1)
}

type trellisEdge struct {
	o0, o1 byte
	next   int
}

// hydraTrellis[state][bit] = (o0, o1, next state).
var hydraTrellis = func() (t [hydraConvState][2]trellisEdge) {
	for s := 0; s < hydraConvState; s++ {
		for b := 0; b < 2; b++ {
			r := (b << 6) | s
			t[s][b] = trellisEdge{parity7(r & hydraG0), parity7(r & hydraG1), r >> 1}
		}
	}
	return t
}()

// HydraConvEncode maps n message bits to 2*(n+6) coded bits (tail-flushed to state 0),
// exactly as hydra_conv.c.
func HydraConvEncode(msg []byte) []byte {
	state := 0
	n := len(msg)
	out := make([]byte, 0, HydraConvCodedLen(n))
	for i := 0; i < n+HydraConvMem; i++ {
		b := 0
		if i < n {
			b = int(msg[i] & 1)
		}
		e := hydraTrellis[state][b]
		out = append(out, e.o0, e.o1)
		state = e.next
	}
	return out
}

// HydraConvDecodeHard is the hard-decision Viterbi decoder: 2*(n+6) coded bits -> n
// message bits. It uses the C soft decoder's correlation metric on +/-1 hard values
// (bit 1 -> +1, bit 0 -> -1), the same state/bit iteration order and strict '>' tie rule,
// start state 0 and traceback from state 0. It returns nil on a bad coded length.
func HydraConvDecodeHard(coded []byte) []byte {
	nc := len(coded)
	if nc%2 != 0 || nc/2 <= HydraConvMem {
		return nil
	}
	L := nc / 2
	nMsg := L - HydraConvMem
	var pm, npm [hydraConvState]int
	var live, nlive [hydraConvState]bool
	live[0] = true
	tbPrev := make([][hydraConvState]uint8, L)
	tbBit := make([][hydraConvState]uint8, L)
	for t := 0; t < L; t++ {
		s0, s1 := -1, -1
		if coded[2*t]&1 != 0 {
			s0 = 1
		}
		if coded[2*t+1]&1 != 0 {
			s1 = 1
		}
		nlive = [hydraConvState]bool{}
		for s := 0; s < hydraConvState; s++ {
			if !live[s] {
				continue
			}
			base := pm[s]
			for b := 0; b < 2; b++ {
				e := hydraTrellis[s][b]
				cand := base
				if e.o0 != 0 {
					cand += s0
				} else {
					cand -= s0
				}
				if e.o1 != 0 {
					cand += s1
				} else {
					cand -= s1
				}
				if !nlive[e.next] || cand > npm[e.next] {
					npm[e.next] = cand
					nlive[e.next] = true
					tbPrev[t][e.next] = uint8(s)
					tbBit[t][e.next] = uint8(b)
				}
			}
		}
		pm, live = npm, nlive
	}
	out := make([]byte, L)
	s := 0
	for t := L - 1; t >= 0; t-- {
		out[t] = tbBit[t][s]
		s = int(tbPrev[t][s])
	}
	return out[:nMsg]
}

// ---- FEC: repetition-3 (hydra_fec.c) ----

// HydraRep3Encode repeats every bit three times.
func HydraRep3Encode(msg []byte) []byte {
	out := make([]byte, 0, 3*len(msg))
	for _, b := range msg {
		b &= 1
		out = append(out, b, b, b)
	}
	return out
}

// HydraRep3Decode is the majority vote over each bit triple.
func HydraRep3Decode(coded []byte) []byte {
	out := make([]byte, len(coded)/3)
	for i := range out {
		if int(coded[3*i]&1)+int(coded[3*i+1]&1)+int(coded[3*i+2]&1) >= 2 {
			out[i] = 1
		}
	}
	return out
}

// ---- coprime-stride block interleaver (hydra_interleave.c) ----

// HydraInterleave is the TX gather: out[i] = in[(i*stride) % n].
func HydraInterleave(in []byte, stride int) []byte {
	n := len(in)
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		out[i] = in[(i*stride)%n]
	}
	return out
}

// HydraDeinterleave is the RX scatter: out[(i*stride) % n] = in[i] — the exact inverse.
func HydraDeinterleave(in []byte, stride int) []byte {
	n := len(in)
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		out[(i*stride)%n] = in[i]
	}
	return out
}

func (p *HydraProfile) syncBits() []byte {
	return hydraBytesToBits([]byte{byte(p.SyncWord >> 8), byte(p.SyncWord)})
}

const symChars = "0123456789abcdef"

// HydraSymbolsEncode renders the full TX symbol stream of hydra_frame_build() as one
// lowercase hex digit per symbol (tone index; n_tones <= 16):
//
//	[preamble: preamble_syms alternating tone 0 / tone n_tones-1, starting with 0]
//	[sync_word bits -> symbols]
//	[interleave?(fec(frame17 || CRC16be(frame17))) -> symbols]
//
// p must have been Init'd.
func HydraSymbolsEncode(p *HydraProfile, frame []byte) (string, error) {
	if len(frame) != HydraDCFBytes {
		return "", ErrFrameLen
	}
	if p.NTones > 16 {
		return "", errors.New("medium: symbol strings need n_tones <= 16")
	}
	if p.BitsPerSymbol == 0 || p.TotalSyms == 0 {
		return "", errors.New("medium: hydra profile not initialised (call Init)")
	}
	crc := dcf.Crc16CCITT(frame)
	field := append(clone(frame), byte(crc>>8), byte(crc))
	data := hydraBytesToBits(field)
	var coded []byte
	switch p.FecMode {
	case FecNone:
		coded = data
	case FecRep3:
		coded = HydraRep3Encode(data)
	default:
		coded = HydraConvEncode(data)
	}
	if len(coded) != p.CodedBits {
		return "", errors.New("medium: hydra coded length mismatch")
	}
	if p.Interleave {
		coded = HydraInterleave(coded, p.InterleaveStride)
	}
	syms := make([]int, 0, p.TotalSyms)
	for k := 0; k < p.PreambleSyms; k++ {
		if k&1 != 0 {
			syms = append(syms, p.NTones-1)
		} else {
			syms = append(syms, 0)
		}
	}
	syms = append(syms, hydraBitsToSymbols(p.syncBits(), p.BitsPerSymbol)...)
	syms = append(syms, hydraBitsToSymbols(coded, p.BitsPerSymbol)...)
	if len(syms) != p.TotalSyms {
		return "", errors.New("medium: hydra symbol count mismatch")
	}
	out := make([]byte, len(syms))
	for i, s := range syms {
		out[i] = symChars[s]
	}
	return string(out), nil
}

// HydraSymbolsDecode inverts HydraSymbolsEncode with hard decisions: strip the preamble by
// count, check the 16 sync bits, deinterleave, FEC-decode (none / rep3 majority / hard
// Viterbi), then the CRC-16 check. It returns (frame, true), or (nil, false) on a wrong
// length, bad symbol, sync mismatch or CRC mismatch. p must have been Init'd.
func HydraSymbolsDecode(p *HydraProfile, symbols string) ([]byte, bool) {
	if p.TotalSyms == 0 || len(symbols) != p.TotalSyms {
		return nil, false
	}
	syms := make([]int, len(symbols))
	for i := 0; i < len(symbols); i++ {
		c := symbols[i]
		var v int
		switch {
		case c >= '0' && c <= '9':
			v = int(c - '0')
		case c >= 'a' && c <= 'f':
			v = int(c-'a') + 10
		case c >= 'A' && c <= 'F':
			v = int(c-'A') + 10
		default:
			return nil, false
		}
		if v >= p.NTones {
			return nil, false
		}
		syms[i] = v
	}
	bps := p.BitsPerSymbol
	off := p.PreambleSyms
	sync := hydraSymbolsToBits(syms[off:off+p.SyncSyms], bps)[:HydraSyncBits]
	if !bytes.Equal(sync, p.syncBits()) {
		return nil, false
	}
	off += p.SyncSyms
	coded := hydraSymbolsToBits(syms[off:off+p.DataSyms], bps)[:p.CodedBits]
	if p.Interleave {
		coded = HydraDeinterleave(coded, p.InterleaveStride)
	}
	var data []byte
	switch p.FecMode {
	case FecNone:
		data = coded[:HydraDataBits]
	case FecRep3:
		data = HydraRep3Decode(coded)
	default:
		data = HydraConvDecodeHard(coded)
	}
	field := hydraBitsToBytes(data)
	if len(field) != HydraDCFBytes+HydraCRCBytes {
		return nil, false
	}
	frame := field[:HydraDCFBytes]
	if dcf.Crc16CCITT(frame) != uint16(field[17])<<8|uint16(field[18]) {
		return nil, false
	}
	return clone(frame), true
}

// ══ afsk_bits (python/modem acoustic_frame: preamble + 0x7E + frame+crc8 | RS + postamble) ═

// AFSK framing constants (python/modem/acoustic_frame.py).
const (
	AfskSyncWord      = 0x7E
	AfskPostambleBits = 16
	AfskNParity       = 16 // RS 2t, the certified DCF-FEC default
)

// AfskProfile is one python/modem acoustic profile (tones + symbol rate + preamble).
type AfskProfile struct {
	Mark         float64
	Space        float64
	Baud         int
	PreambleBits int
}

// AfskProfiles mirrors acoustic_frame.PROFILES.
var AfskProfiles = map[string]AfskProfile{
	"standard":  {Mark: 1200, Space: 2200, Baud: 300, PreambleBits: 80},
	"handheld":  {Mark: 1200, Space: 1800, Baud: 300, PreambleBits: 240},
	"aux-cable": {Mark: 1000, Space: 1500, Baud: 1200, PreambleBits: 16},
}

// AfskCRC8 is the poly-0x31 CRC-8 (MSB-first, init 0x00, non-reflected) of the deployed
// Faust modem (which labels it "MAXIM"; it is not reflected).
func AfskCRC8(data []byte) byte {
	var crc byte
	for _, b := range data {
		crc ^= b
		for i := 0; i < 8; i++ {
			if crc&0x80 != 0 {
				crc = crc<<1 ^ 0x31
			} else {
				crc <<= 1
			}
		}
	}
	return crc
}

func afskAlt(n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = byte(i % 2)
	}
	return out
}

// AfskBitsEncode renders the on-air AFSK bit stream ("0"/"1" string) of
// acoustic_frame.encode_bits with the profile's preamble_bits: preamble (alternating
// 0,1..) + 0x7E + (frame+crc8 | RS16 codeword) + 16 alternating postamble bits.
func AfskBitsEncode(frame []byte, profile string, fec bool) (string, error) {
	pr, ok := AfskProfiles[profile]
	if !ok {
		return "", fmt.Errorf("medium: unknown afsk profile %q", profile)
	}
	var payload []byte
	if fec {
		if len(frame)+AfskNParity > 255 {
			return "", errors.New("medium: RS(255) message+parity must be <= 255 bytes")
		}
		payload = dcf.RSEncode(frame, AfskNParity)
	} else {
		payload = append(clone(frame), AfskCRC8(frame))
	}
	bs := afskAlt(pr.PreambleBits)
	bs = append(bs, hydraBytesToBits([]byte{AfskSyncWord})...)
	bs = append(bs, hydraBytesToBits(payload)...)
	bs = append(bs, afskAlt(AfskPostambleBits)...)
	out := make([]byte, len(bs))
	for i, b := range bs {
		out[i] = '0' + b
	}
	return string(out), nil
}

// afskFindSync is the index of the first bit after the 0x7E sync word, or -1 (bit-level
// search, so the preamble length need not be a whole number of bytes).
func afskFindSync(bs []byte) int {
	pat := hydraBytesToBits([]byte{AfskSyncWord})
	for i := 0; i+len(pat) <= len(bs); i++ {
		if bytes.Equal(bs[i:i+len(pat)], pat) {
			return i + len(pat)
		}
	}
	return -1
}

// AfskBitsDecode inverts AfskBitsEncode (acoustic_frame.decode_bits: bit-level 0x7E
// search, then the crc8 check or the RS decode). It returns (frame, true) or (nil, false).
// profile is validated for symmetry; the bit layer is preamble-length agnostic.
func AfskBitsDecode(bitstr string, profile string, fec bool) ([]byte, bool) {
	if _, ok := AfskProfiles[profile]; !ok {
		return nil, false
	}
	bs := make([]byte, len(bitstr))
	for i := 0; i < len(bitstr); i++ {
		switch bitstr[i] {
		case '0':
		case '1':
			bs[i] = 1
		default:
			return nil, false
		}
	}
	pos := afskFindSync(bs)
	if pos < 0 {
		return nil, false
	}
	data := hydraBitsToBytes(bs[pos:])
	if fec {
		if len(data) < FrameLen+AfskNParity {
			return nil, false
		}
		msg, _, err := dcf.RSDecode(data[:FrameLen+AfskNParity], AfskNParity, FrameLen)
		if err != nil {
			return nil, false
		}
		return clone(msg), true
	}
	if len(data) < FrameLen+1 {
		return nil, false
	}
	frame := data[:FrameLen]
	if AfskCRC8(frame) != data[FrameLen] {
		return nil, false
	}
	return clone(frame), true
}
