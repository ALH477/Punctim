// SPDX-License-Identifier: LGPL-3.0-only

// Package audio implements DCF-Audio: collaborative audio over the DeModFrame wire.
//
// Audio is an *adapter* over the 17-byte DeModFrame quantum (package dcf), not a new wire
// format. A 20 ms codec block is serialised into 1 + frag_total ordinary CTRL frames. This
// L2 framing is codec-agnostic and byte-deterministic across C/Rust/Python/Go — it is
// pinned by Documentation/audio_vectors.json. See Documentation/DCF_AUDIO_SPEC.md.
//
// Layout (all frames version=1, type=CTRL(3), big-endian):
//
//	seq = packet_id[15:5] (11 bits) | frag_idx[4:0] (5 bits)
//	frag_idx 0  descriptor : payload = [payload_len, frag_total, codec_id, flags]
//	frag_idx k  data       : payload = bytes[(k-1)*4 .. +4]  (last frame zero-padded)
//	frag_total = ceil(payload_len/4)  (<= 31  =>  payload_len <= 124 bytes / 20 ms)
//
// Only the L2 framing, the PCM-diag codec bytes, and the PM param layout are byte-certified;
// Opus output and PM synthesis audio are NOT byte-certified (and not implemented here).
package audio

import (
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"sort"

	"github.com/ALH477/Punctim/go/dcf"
)

// L2 constants.
const (
	FragBits    = 5
	FragMask    = 0x1F
	MaxFrags    = 31
	MaxPayload  = MaxFrags * 4 // 124 bytes / 20 ms block
	MaxPacketID = (1 << (16 - FragBits)) - 1
)

// Codec ids (profiles in DCF_AUDIO_SPEC.md). id 3 reserved.
const (
	CodecOpus    uint8 = 0 // opaque to L2 (output not byte-certified; no libopus here)
	CodecPcmDiag uint8 = 1 // 6 kHz 8-bit mono diagnostic (byte-certified)
	CodecFaustPM uint8 = 2 // Faust phase-mod, 8-byte param block (param layout certified)
)

// Descriptor flag bits.
const (
	FlagEndTalkspurt uint8 = 0x01
	FlagPmVoice      uint8 = 0x02
)

// Packetize errors.
var (
	ErrPayloadTooLarge = errors.New("audio: codec block exceeds the 124-byte cap")
	ErrBadPacketID     = errors.New("audio: packet_id exceeds the 11-bit field")
)

// Packetize serialises one codec block into DeModFrame CTRL frames (descriptor first, then
// data fragments in order). Each frame is a fully valid 17-byte DeModFrame.
func Packetize(codecID uint8, payload []byte, packetID uint16, tsUs uint32, src, dst uint16, flags uint8) ([][17]byte, error) {
	if len(payload) > MaxPayload {
		return nil, ErrPayloadTooLarge
	}
	if packetID > MaxPacketID {
		return nil, ErrBadPacketID
	}
	fragTotal := uint8((len(payload) + 3) / 4)
	frames := make([][17]byte, 0, 1+int(fragTotal))

	// frag_idx 0 — descriptor
	descSeq := packetID << FragBits
	desc := [4]byte{byte(len(payload)), fragTotal, codecID, flags}
	frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FCtrl, Seq: descSeq, Src: src, Dst: dst, Payload: desc, TsUs: tsUs}.Encode())

	// frag_idx 1..frag_total — data, last chunk zero-padded
	for k := uint8(1); k <= fragTotal; k++ {
		off := (int(k) - 1) * 4
		var chunk [4]byte
		end := off + 4
		if end > len(payload) {
			end = len(payload)
		}
		copy(chunk[:end-off], payload[off:end])
		seq := (packetID << FragBits) | uint16(k)
		frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FCtrl, Seq: seq, Src: src, Dst: dst, Payload: chunk, TsUs: tsUs}.Encode())
	}
	return frames, nil
}

// AudioPacket is a fully reassembled codec block.
type AudioPacket struct {
	PacketID uint16
	TsUs     uint32
	CodecID  uint8
	Flags    uint8
	Payload  []byte
}

type audioSlot struct {
	hasDesc   bool
	descLen   uint8
	fragTotal uint8
	codecID   uint8
	flags     uint8
	tsUs      uint32
	frags     map[uint8][4]byte
}

// AudioReassembler is a stateful reassembler. Push emits a completed packet as soon as its
// descriptor and every data fragment have arrived; duplicates are ignored; Finalize reports
// any still-incomplete packet as lost. Mirrors the C and Rust references.
type AudioReassembler struct {
	slots map[uint16]*audioSlot
}

// NewAudioReassembler returns an empty reassembler.
func NewAudioReassembler() *AudioReassembler {
	return &AudioReassembler{slots: make(map[uint16]*audioSlot)}
}

// Push feeds one 17-byte frame. It returns a completed packet when one becomes whole on this
// push, else nil. Non-CTRL frames are ignored.
func (r *AudioReassembler) Push(frame *[17]byte) *AudioPacket {
	d, err := dcf.Decode(frame[:])
	if err != nil {
		return nil
	}
	if d.Type != dcf.FCtrl {
		return nil
	}
	packetID := d.Seq >> FragBits
	fragIdx := uint8(d.Seq & FragMask)
	entry := r.slots[packetID]
	if entry == nil {
		entry = &audioSlot{frags: make(map[uint8][4]byte)}
		r.slots[packetID] = entry
	}
	entry.tsUs = d.TsUs
	if fragIdx == 0 {
		if !entry.hasDesc {
			entry.hasDesc = true
			entry.descLen = d.Payload[0]
			entry.fragTotal = d.Payload[1]
			entry.codecID = d.Payload[2]
			entry.flags = d.Payload[3]
		}
	} else {
		if _, ok := entry.frags[fragIdx]; !ok {
			entry.frags[fragIdx] = d.Payload
		}
	}
	return r.tryEmit(packetID)
}

func (r *AudioReassembler) tryEmit(packetID uint16) *AudioPacket {
	entry := r.slots[packetID]
	if entry == nil || !entry.hasDesc {
		return nil
	}
	for k := uint8(1); k <= entry.fragTotal; k++ {
		if _, ok := entry.frags[k]; !ok {
			return nil
		}
	}
	raw := make([]byte, 0, int(entry.fragTotal)*4)
	for k := uint8(1); k <= entry.fragTotal; k++ {
		chunk := entry.frags[k]
		raw = append(raw, chunk[:]...)
	}
	if int(entry.descLen) < len(raw) {
		raw = raw[:entry.descLen]
	}
	p := &AudioPacket{PacketID: packetID, TsUs: entry.tsUs, CodecID: entry.codecID, Flags: entry.flags, Payload: raw}
	delete(r.slots, packetID)
	return p
}

// Finalize reports every still-incomplete packet as lost (ascending packet_id), clearing
// state.
func (r *AudioReassembler) Finalize() []uint16 {
	lost := make([]uint16, 0, len(r.slots))
	for id := range r.slots {
		lost = append(lost, id)
	}
	sort.Slice(lost, func(i, j int) bool { return lost[i] < lost[j] })
	r.slots = make(map[uint16]*audioSlot)
	return lost
}

// ── L1: PCM-diagnostic codec (id 1) — 6 kHz 8-bit mono, 120 B/block ─────────

// PcmDiagRate and PcmDiagBlock describe the diagnostic codec profile.
const (
	PcmDiagRate  = 6000
	PcmDiagBlock = 120
)

// PcmDiagEncode maps float [-1,1] -> unsigned 8-bit (mid 128). PcmDiagEncode(PcmDiagDecode(b))
// is byte-lossless, which the certificate asserts.
func PcmDiagEncode(samples []float32) []byte {
	out := make([]byte, len(samples))
	for i, s := range samples {
		v := int32(math.Round(float64(s)*128.0)) + 128
		if v < 0 {
			v = 0
		}
		if v > 255 {
			v = 255
		}
		out[i] = byte(v)
	}
	return out
}

// PcmDiagDecode maps unsigned 8-bit -> float [-1,1].
func PcmDiagDecode(data []byte) []float32 {
	out := make([]float32, len(data))
	for i, b := range data {
		out[i] = (float32(b) - 128.0) / 128.0
	}
	return out
}

// ── L1: PM (Faust phase-mod, id 2) — 8-byte parameter layout (certified) ────

// PmParams is the certified 8-byte phase-mod parameter block. Synthesis audio is NOT
// byte-certified and is not implemented here; only this parameter layout is.
type PmParams struct {
	F0       uint16
	Amp      uint8
	ModIndex uint8
	ModRatio uint8
	Bright   uint8
	Env      uint8
	Flags    uint8
}

// Pack serialises the parameter block into 8 deterministic bytes (layout f0_hi, f0_lo, amp,
// mod_index, mod_ratio, bright, env, flags).
func (p PmParams) Pack() [8]byte {
	return [8]byte{
		byte(p.F0 >> 8), byte(p.F0),
		p.Amp, p.ModIndex, p.ModRatio, p.Bright, p.Env, p.Flags,
	}
}

// PmUnpack deserialises 8 bytes into a parameter block.
func PmUnpack(b []byte) PmParams {
	return PmParams{
		F0:       uint16(b[0])<<8 | uint16(b[1]),
		Amp:      b[2],
		ModIndex: b[3],
		ModRatio: b[4],
		Bright:   b[5],
		Env:      b[6],
		Flags:    b[7],
	}
}

// init self-certifies the L2 framing against the exampleAudioPacket anchor on import,
// panicking (refuse to load) if the implementation has diverged from the reference.
func init() {
	// anchors.exampleAudioPacket from Documentation/audio_vectors.json.
	payload, _ := hex.DecodeString("deadbeef1234")
	want := []string{
		"d31302000001ffff06020100010203cad7",
		"d31302010001ffffdeadbeef0102032489",
		"d31302020001ffff1234000001020332bc",
	}
	frames, err := Packetize(CodecPcmDiag, payload, 16, 66051, 1, 0xFFFF, 0)
	if err != nil {
		panic(fmt.Sprintf("audio: exampleAudioPacket anchor packetize failed: %v", err))
	}
	if len(frames) != len(want) {
		panic(fmt.Sprintf("audio: exampleAudioPacket anchor frame count = %d, want %d", len(frames), len(want)))
	}
	for i, f := range frames {
		if got := hex.EncodeToString(f[:]); got != want[i] {
			panic(fmt.Sprintf("audio: exampleAudioPacket anchor frame[%d] = %s, want %s", i, got, want[i]))
		}
	}
}
