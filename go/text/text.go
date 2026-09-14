// SPDX-License-Identifier: LGPL-3.0-only

// Package text implements DCF-Text: chat / agent-to-agent text transport over the
// DeModFrame wire.
//
// Text traffic is an *adapter* over the 17-byte DeModFrame quantum (package dcf), not a new
// wire format — exactly like DCF-Audio and DCF-Game. One UTF-8 message is fragmented into
// 1 + frag_total ordinary DATA frames whose 4-byte payloads carry the bytes. This L2 framing
// is byte-deterministic across C/Rust/Python/Go — it is pinned by
// Documentation/text_vectors.json. See Documentation/DCF_TEXT_SPEC.md.
//
// Layout (all frames version=1, type=DATA(0), big-endian):
//
//	seq = packet_id[15:10] (6 bits, 0..63) | frag_idx[9:0] (10 bits, 0..1023)
//	frag_idx 0  descriptor : payload = [len_hi, len_lo, flags, 0]
//	frag_idx k  data       : payload = bytes[(k-1)*4 .. +4]  (last frame zero-padded)
//	frag_total = ceil(len/4)  (<= 1023  =>  len <= 4092 bytes / message)
//
// Audio uses CTRL(3); text and game ride DATA(0) but use DIFFERENT seq splits (text's 10-bit
// fragment index vs game's 5-bit) and a 2-byte descriptor length, so do not copy game's
// shape. The descriptor that opens every message tells a receiver which adapter owns the id.
package text

import (
	"encoding/hex"
	"errors"
	"fmt"
	"sort"

	"github.com/ALH477/Punctim/go/dcf"
)

// L2 constants.
const (
	FragBits    = 10
	FragMask    = 0x3FF
	MaxFrags    = 1023
	MaxPayload  = MaxFrags * 4 // 4092 bytes / message
	MaxPacketID = (1 << (16 - FragBits)) - 1
	Broadcast   = 0xFFFF
)

// Descriptor flag bits — opaque to L2 (they do not change the framing bytes).
const (
	FlagAgent    uint8 = 0x01
	FlagMore     uint8 = 0x02
	FlagReliable uint8 = 0x04
)

// Packetize errors.
var (
	ErrPayloadTooLarge = errors.New("text: message exceeds the 4092-byte cap")
	ErrBadPacketID     = errors.New("text: packet_id exceeds the 6-bit field")
)

// ChannelID maps a human channel/passphrase to a 16-bit rendezvous dst (the same
// frequency-channel hash the rest of the repo uses). Empty => broadcast.
func ChannelID(name string) uint16 {
	if name == "" {
		return Broadcast
	}
	return dcf.Crc16CCITT([]byte(name))
}

// Packetize serialises one message (its raw UTF-8 bytes) into DeModFrame DATA frames
// (descriptor first, then data fragments in order). Each frame is a fully valid 17-byte
// DeModFrame.
func Packetize(payload []byte, packetID uint16, tsUs uint32, src, dst uint16, flags uint8) ([][17]byte, error) {
	if len(payload) > MaxPayload {
		return nil, ErrPayloadTooLarge
	}
	if packetID > MaxPacketID {
		return nil, ErrBadPacketID
	}
	fragTotal := uint16((len(payload) + 3) / 4)
	frames := make([][17]byte, 0, 1+int(fragTotal))

	// frag_idx 0 — descriptor (2-byte big-endian length so the last fragment can be unpadded)
	descSeq := packetID << FragBits
	length := uint16(len(payload))
	desc := [4]byte{byte(length >> 8), byte(length & 0xFF), flags, 0}
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
		seq := (packetID << FragBits) | k
		frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FData, Seq: seq, Src: src, Dst: dst, Payload: chunk, TsUs: tsUs}.Encode())
	}
	return frames, nil
}

// TextPacket is a fully reassembled text message.
type TextPacket struct {
	PacketID uint16
	TsUs     uint32
	Src      uint16
	Dst      uint16
	Flags    uint8
	Text     string
}

type textSlot struct {
	hasDesc bool
	length  uint16
	flags   uint8
	tsUs    uint32
	src     uint16
	dst     uint16
	frags   map[uint16][4]byte
}

// TextReassembler is a stateful reassembler. Push emits a completed message as soon as its
// descriptor and every data fragment have arrived; duplicates are ignored; Finalize reports
// any still-incomplete message as lost. Mirrors the C and Rust references. The reassembler is
// channel-agnostic — channel filtering, if any, is the caller's job.
type TextReassembler struct {
	slots map[uint16]*textSlot
}

// NewTextReassembler returns an empty reassembler.
func NewTextReassembler() *TextReassembler {
	return &TextReassembler{slots: make(map[uint16]*textSlot)}
}

// Push feeds one 17-byte frame. It returns a completed message when one becomes whole on this
// push, else nil. Non-DATA frames are ignored.
func (r *TextReassembler) Push(frame *[17]byte) *TextPacket {
	d, err := dcf.Decode(frame[:])
	if err != nil {
		return nil
	}
	if d.Type != dcf.FData {
		return nil
	}
	packetID := d.Seq >> FragBits
	fragIdx := d.Seq & FragMask
	entry := r.slots[packetID]
	if entry == nil {
		entry = &textSlot{frags: make(map[uint16][4]byte)}
		r.slots[packetID] = entry
	}
	entry.tsUs = d.TsUs
	entry.src = d.Src
	entry.dst = d.Dst
	if fragIdx == 0 {
		if !entry.hasDesc {
			entry.hasDesc = true
			entry.length = uint16(d.Payload[0])<<8 | uint16(d.Payload[1])
			entry.flags = d.Payload[2]
		}
	} else {
		if _, ok := entry.frags[fragIdx]; !ok {
			entry.frags[fragIdx] = d.Payload
		}
	}
	return r.tryEmit(packetID)
}

func (r *TextReassembler) tryEmit(packetID uint16) *TextPacket {
	entry := r.slots[packetID]
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
	p := &TextPacket{
		PacketID: packetID,
		TsUs:     entry.tsUs,
		Src:      entry.src,
		Dst:      entry.dst,
		Flags:    entry.flags,
		Text:     string(raw),
	}
	delete(r.slots, packetID)
	return p
}

// Finalize reports every still-incomplete message as lost (ascending packet_id), clearing
// state.
func (r *TextReassembler) Finalize() []uint16 {
	lost := make([]uint16, 0, len(r.slots))
	for id := range r.slots {
		lost = append(lost, id)
	}
	sort.Slice(lost, func(i, j int) bool { return lost[i] < lost[j] })
	r.slots = make(map[uint16]*textSlot)
	return lost
}

// init self-certifies the L2 framing against the exampleTextMessage anchor on import,
// panicking (refuse to load) if the implementation has diverged from the reference.
func init() {
	// anchors.exampleTextMessage from Documentation/text_vectors.json:
	// text "agent ⇄ agent 🚀", dst 61143, flags 5, packet_id 10.
	payload, _ := hex.DecodeString("6167656e7420e28784206167656e7420f09f9a80")
	want := []string{
		"d310280000a1eed7001405000102035490",
		"d310280100a1eed76167656e01020335b6",
		"d310280200a1eed77420e287010203ee3d",
		"d310280300a1eed7842061670102038a38",
		"d310280400a1eed7656e7420010203d0fe",
		"d310280500a1eed7f09f9a80010203fc60",
	}
	frames, err := Packetize(payload, 10, 66051, 161, 61143, 5)
	if err != nil {
		panic(fmt.Sprintf("text: exampleTextMessage anchor packetize failed: %v", err))
	}
	if len(frames) != len(want) {
		panic(fmt.Sprintf("text: exampleTextMessage anchor frame count = %d, want %d", len(frames), len(want)))
	}
	for i, f := range frames {
		if got := hex.EncodeToString(f[:]); got != want[i] {
			panic(fmt.Sprintf("text: exampleTextMessage anchor frame[%d] = %s, want %s", i, got, want[i]))
		}
	}
	// channel_id anchor: crc16("123456789") == 0x29B1, empty => broadcast.
	if c := ChannelID("123456789"); c != 0x29B1 {
		panic(fmt.Sprintf("text: ChannelID anchor diverged: ChannelID(\"123456789\")=0x%04X, want 0x29B1", c))
	}
	if c := ChannelID(""); c != Broadcast {
		panic(fmt.Sprintf("text: ChannelID(\"\")=0x%04X, want 0x%04X", c, Broadcast))
	}
}
