// SPDX-License-Identifier: LGPL-3.0-only

// Package sstv implements DCF-SSTV: slow-scan television (still-image) transport over the
// DeModFrame wire.
//
// Image traffic is an *adapter* over the 17-byte DeModFrame quantum (package dcf), not a new
// wire format — exactly like DCF-Text and DCF-Game. One still image is fragmented into
// 1 + frag_total ordinary DATA frames whose 4-byte payloads carry the opaque image bytes.
// This L2 framing is byte-deterministic across C/Rust/Python/Go/Node — it is pinned by
// Documentation/sstv_vectors.json. See Documentation/DCF_SSTV_SPEC.md.
//
// Layout (all frames version=1, type=DATA(0), big-endian):
//
//	seq = image_id[15:11] (5 bits, 0..31) | frag_idx[10:0] (11 bits, 0..2047)
//	frag_idx 0  descriptor : payload = [len_hi, len_lo, format_id, flags]
//	frag_idx k  data       : payload = bytes[(k-1)*4 .. +4]  (last frame zero-padded)
//	frag_total = ceil(len/4)  (<= 2047  =>  len <= 8188 bytes / image)
//
// Text and game also ride DATA(0) but use different seq splits (text 6:10, game 11:5,
// sstv 5:11 — a wider fragment index for larger images). The descriptor that opens every
// image tells a receiver which adapter owns the id, and a node runs exactly one reassembler
// per dst channel. The image bytes are opaque; format_id is a hint that never changes these
// vectors.
package sstv

import (
	"encoding/hex"
	"errors"
	"fmt"
	"sort"

	"github.com/ALH477/Punctim/go/dcf"
)

// L2 constants.
const (
	FragBits   = 11
	FragMask   = 0x7FF
	MaxFrags   = 2047
	MaxPayload = MaxFrags * 4 // 8188 bytes / image
	MaxImageID = (1 << (16 - FragBits)) - 1
	Broadcast  = 0xFFFF
)

// Image format ids — opaque hints (L2 never parses the bytes).
const (
	FmtRaw    uint8 = 0
	FmtJPEG   uint8 = 1
	FmtPNG    uint8 = 2
	FmtWebP   uint8 = 3
	FmtRGB565 uint8 = 4
)

// Descriptor flag bits — opaque to L2 (they do not change the framing bytes).
const (
	FlagMore     uint8 = 0x01
	FlagKeyframe uint8 = 0x02
	FlagReliable uint8 = 0x04
)

// Packetize errors.
var (
	ErrPayloadTooLarge = errors.New("sstv: image exceeds the 8188-byte cap")
	ErrBadImageID      = errors.New("sstv: image_id exceeds the 5-bit field")
)

// ChannelID maps a human channel/passphrase to a 16-bit rendezvous dst (the same
// frequency-channel hash the rest of the repo uses). Empty => broadcast.
func ChannelID(name string) uint16 {
	if name == "" {
		return Broadcast
	}
	return dcf.Crc16CCITT([]byte(name))
}

// Packetize serialises one image (its raw opaque bytes) into DeModFrame DATA frames
// (descriptor first, then data fragments in order). Each frame is a fully valid 17-byte
// DeModFrame.
func Packetize(payload []byte, imageID uint16, tsUs uint32, src, dst uint16, formatID, flags uint8) ([][17]byte, error) {
	if len(payload) > MaxPayload {
		return nil, ErrPayloadTooLarge
	}
	if imageID > MaxImageID {
		return nil, ErrBadImageID
	}
	fragTotal := uint16((len(payload) + 3) / 4)
	frames := make([][17]byte, 0, 1+int(fragTotal))

	// frag_idx 0 — descriptor (2-byte big-endian length, format_id, flags)
	descSeq := imageID << FragBits
	length := uint16(len(payload))
	desc := [4]byte{byte(length >> 8), byte(length & 0xFF), formatID, flags}
	frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FData, Seq: descSeq, Src: src, Dst: dst, Payload: desc, TsUs: tsUs}.Encode())

	// frag_idx 1..frag_total — data, last chunk zero-padded
	for k := uint16(1); k <= fragTotal; k++ {
		off := (int(k) - 1) * 4
		var chunk [4]byte
		end := off + 4
		if end > len(payload) {
			end = len(payload)
		}
		copy(chunk[:end-off], payload[off:end])
		seq := (imageID << FragBits) | k
		frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FData, Seq: seq, Src: src, Dst: dst, Payload: chunk, TsUs: tsUs}.Encode())
	}
	return frames, nil
}

// SstvImage is a fully reassembled image.
type SstvImage struct {
	ImageID  uint16
	TsUs     uint32
	Src      uint16
	Dst      uint16
	FormatID uint8
	Flags    uint8
	Data     []byte
}

type sstvSlot struct {
	hasDesc  bool
	length   uint16
	formatID uint8
	flags    uint8
	tsUs     uint32
	src      uint16
	dst      uint16
	frags    map[uint16][4]byte
}

// SstvReassembler is a stateful reassembler. Push emits a completed image as soon as its
// descriptor and every data fragment have arrived; duplicates are ignored; Finalize reports
// any still-incomplete image as lost. Mirrors the C, Rust and Python references. The
// reassembler is channel-agnostic — channel filtering, if any, is the caller's job.
type SstvReassembler struct {
	slots map[uint16]*sstvSlot
}

// NewSstvReassembler returns an empty reassembler.
func NewSstvReassembler() *SstvReassembler {
	return &SstvReassembler{slots: make(map[uint16]*sstvSlot)}
}

// Push feeds one 17-byte frame. It returns a completed image when one becomes whole on this
// push, else nil. Non-DATA frames are ignored.
func (r *SstvReassembler) Push(frame *[17]byte) *SstvImage {
	d, err := dcf.Decode(frame[:])
	if err != nil {
		return nil
	}
	if d.Type != dcf.FData {
		return nil
	}
	imageID := d.Seq >> FragBits
	fragIdx := d.Seq & FragMask
	entry := r.slots[imageID]
	if entry == nil {
		entry = &sstvSlot{frags: make(map[uint16][4]byte)}
		r.slots[imageID] = entry
	}
	entry.tsUs = d.TsUs
	entry.src = d.Src
	entry.dst = d.Dst
	if fragIdx == 0 {
		if !entry.hasDesc {
			entry.hasDesc = true
			entry.length = uint16(d.Payload[0])<<8 | uint16(d.Payload[1])
			entry.formatID = d.Payload[2]
			entry.flags = d.Payload[3]
		}
	} else {
		if _, ok := entry.frags[fragIdx]; !ok {
			entry.frags[fragIdx] = d.Payload
		}
	}
	return r.tryEmit(imageID)
}

func (r *SstvReassembler) tryEmit(imageID uint16) *SstvImage {
	entry := r.slots[imageID]
	if entry == nil || !entry.hasDesc {
		return nil
	}
	fragTotal := uint16((int(entry.length) + 3) / 4)
	for k := uint16(1); k <= fragTotal; k++ {
		if _, ok := entry.frags[k]; !ok {
			return nil
		}
	}
	raw := make([]byte, 0, int(fragTotal)*4)
	for k := uint16(1); k <= fragTotal; k++ {
		chunk := entry.frags[k]
		raw = append(raw, chunk[:]...)
	}
	if int(entry.length) < len(raw) {
		raw = raw[:entry.length]
	}
	p := &SstvImage{
		ImageID:  imageID,
		TsUs:     entry.tsUs,
		Src:      entry.src,
		Dst:      entry.dst,
		FormatID: entry.formatID,
		Flags:    entry.flags,
		Data:     raw,
	}
	delete(r.slots, imageID)
	return p
}

// Finalize reports every still-incomplete image as lost (ascending image_id), clearing state.
func (r *SstvReassembler) Finalize() []uint16 {
	lost := make([]uint16, 0, len(r.slots))
	for id := range r.slots {
		lost = append(lost, id)
	}
	sort.Slice(lost, func(i, j int) bool { return lost[i] < lost[j] })
	r.slots = make(map[uint16]*sstvSlot)
	return lost
}

// init self-certifies the L2 framing against the exampleImage anchor on import, panicking
// (refuse to load) if the implementation has diverged from the reference.
func init() {
	// anchors.exampleImage from Documentation/sstv_vectors.json:
	// image_id 10, ts_us 66051, src 161, dst 1789, format_id 1 (JPEG), flags 6.
	payload, _ := hex.DecodeString("ffd8ffe000104a46494600010100")
	want := []string{
		"d310500000a106fd000e010601020388a3",
		"d310500100a106fdffd8ffe0010203547f",
		"d310500200a106fd00104a460102038765",
		"d310500300a106fd49460001010203e732",
		"d310500400a106fd01000000010203307d",
	}
	frames, err := Packetize(payload, 10, 66051, 161, 1789, 1, 6)
	if err != nil {
		panic(fmt.Sprintf("sstv: exampleImage anchor packetize failed: %v", err))
	}
	if len(frames) != len(want) {
		panic(fmt.Sprintf("sstv: exampleImage anchor frame count = %d, want %d", len(frames), len(want)))
	}
	for i, f := range frames {
		if got := hex.EncodeToString(f[:]); got != want[i] {
			panic(fmt.Sprintf("sstv: exampleImage anchor frame[%d] = %s, want %s", i, got, want[i]))
		}
	}
	// channel_id anchor: crc16("123456789") == 0x29B1, empty => broadcast.
	if c := ChannelID("123456789"); c != 0x29B1 {
		panic(fmt.Sprintf("sstv: ChannelID anchor diverged: ChannelID(\"123456789\")=0x%04X, want 0x29B1", c))
	}
	if c := ChannelID(""); c != Broadcast {
		panic(fmt.Sprintf("sstv: ChannelID(\"\")=0x%04X, want 0x%04X", c, Broadcast))
	}
}
