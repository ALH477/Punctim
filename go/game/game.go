// SPDX-License-Identifier: LGPL-3.0-only

// Package game implements DCF-Game: low-latency multiplayer state/event transport
// over the DeModFrame wire.
//
// Game traffic is an *adapter* over the 17-byte DeModFrame quantum (package dcf), not a
// new wire format — exactly like DCF-Audio. One game message (a state snapshot, an input
// frame, or an opaque event) is serialised into 1 + frag_total ordinary DATA frames.
// This L2 framing is message-type-agnostic and byte-deterministic across C/Rust/Python/Go —
// it is pinned by Documentation/game_vectors.json. See Documentation/DCF_GAME_SPEC.md.
//
// Layout (all frames version=1, type=DATA(0), big-endian):
//
//	seq = packet_id[15:5] (11 bits) | frag_idx[4:0] (5 bits)
//	frag_idx 0  descriptor : payload = [payload_len, frag_total, msg_type_id, flags]
//	frag_idx k  data       : payload = bytes[(k-1)*4 .. +4]  (last frame zero-padded)
//	frag_total = ceil(payload_len/4)  (<= 31  =>  payload_len <= 124 bytes / message)
//
// Audio uses CTRL(3); game uses DATA(0), so the two adapters never collide on the wire.
package game

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
	MaxPayload  = MaxFrags * 4 // 124 bytes / message
	MaxPacketID = (1 << (16 - FragBits)) - 1
)

// Message-type registry ids (profiles in DCF_GAME_SPEC.md). ids 4..=255 reserved.
const (
	MsgSnapshot uint8 = 0 // packed player state (byte-deterministic)
	MsgInput    uint8 = 1 // input bitfield + tick (byte-deterministic)
	MsgEvent    uint8 = 2 // opaque application bytes (not certified)
	MsgJoin     uint8 = 3 // lobby membership (byte-deterministic)
)

// Descriptor flag bits — request transport behaviour; do not change the L2 bytes.
const (
	FlagReliable uint8 = 0x01
	FlagOrdered  uint8 = 0x02
	FlagEndTick  uint8 = 0x04
)

// Packetize errors.
var (
	ErrPayloadTooLarge = errors.New("game: message exceeds the 124-byte cap")
	ErrBadPacketID     = errors.New("game: packet_id exceeds the 11-bit field")
)

// Packetize serialises one game message into DeModFrame DATA frames (descriptor first,
// then data fragments in order). Each frame is a fully valid 17-byte DeModFrame.
func Packetize(msgTypeID uint8, payload []byte, packetID uint16, tsUs uint32, src, dst uint16, flags uint8) ([][17]byte, error) {
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
	desc := [4]byte{byte(len(payload)), fragTotal, msgTypeID, flags}
	frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FData, Seq: descSeq, Src: src, Dst: dst, Payload: desc, TsUs: tsUs}.Encode())

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
		frames = append(frames, dcf.Frame{Version: 1, Type: dcf.FData, Seq: seq, Src: src, Dst: dst, Payload: chunk, TsUs: tsUs}.Encode())
	}
	return frames, nil
}

// GamePacket is a fully reassembled game message.
type GamePacket struct {
	PacketID  uint16
	TsUs      uint32
	MsgTypeID uint8
	Flags     uint8
	Payload   []byte
}

type gameSlot struct {
	hasDesc   bool
	descLen   uint8
	fragTotal uint8
	msgTypeID uint8
	flags     uint8
	tsUs      uint32
	frags     map[uint8][4]byte
}

// GameReassembler is a stateful reassembler. Push emits a completed message as soon as its
// descriptor and every data fragment have arrived; duplicates are ignored; Finalize reports
// any still-incomplete message as lost. Mirrors the C and Rust references.
type GameReassembler struct {
	slots map[uint16]*gameSlot
}

// NewGameReassembler returns an empty reassembler.
func NewGameReassembler() *GameReassembler {
	return &GameReassembler{slots: make(map[uint16]*gameSlot)}
}

// Push feeds one 17-byte frame. It returns a completed message when one becomes whole on
// this push, else nil. Non-DATA frames are ignored.
func (r *GameReassembler) Push(frame *[17]byte) *GamePacket {
	d, err := dcf.Decode(frame[:])
	if err != nil {
		return nil
	}
	if d.Type != dcf.FData {
		return nil
	}
	packetID := d.Seq >> FragBits
	fragIdx := uint8(d.Seq & FragMask)
	entry := r.slots[packetID]
	if entry == nil {
		entry = &gameSlot{frags: make(map[uint8][4]byte)}
		r.slots[packetID] = entry
	}
	entry.tsUs = d.TsUs
	if fragIdx == 0 {
		if !entry.hasDesc {
			entry.hasDesc = true
			entry.descLen = d.Payload[0]
			entry.fragTotal = d.Payload[1]
			entry.msgTypeID = d.Payload[2]
			entry.flags = d.Payload[3]
		}
	} else {
		if _, ok := entry.frags[fragIdx]; !ok {
			entry.frags[fragIdx] = d.Payload
		}
	}
	return r.tryEmit(packetID)
}

func (r *GameReassembler) tryEmit(packetID uint16) *GamePacket {
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
	p := &GamePacket{PacketID: packetID, TsUs: entry.tsUs, MsgTypeID: entry.msgTypeID, Flags: entry.flags, Payload: raw}
	delete(r.slots, packetID)
	return p
}

// Finalize reports every still-incomplete message as lost (ascending packet_id), clearing
// state.
func (r *GameReassembler) Finalize() []uint16 {
	lost := make([]uint16, 0, len(r.slots))
	for id := range r.slots {
		lost = append(lost, id)
	}
	sort.Slice(lost, func(i, j int) bool { return lost[i] < lost[j] })
	r.slots = make(map[uint16]*gameSlot)
	return lost
}

// ── L1: SNAPSHOT body (msg_type 0) — 14-byte player state, Q8.8 fixed-point ──

// SnapshotLen is the fixed byte length of a packed Snapshot.
const SnapshotLen = 14

// Snapshot is a player-state body. Positions/velocities are Q8.8 fixed-point; yaw is a raw
// big-endian u16.
type Snapshot struct {
	X, Y, Z    float32
	Vx, Vy, Vz float32
	Yaw        uint16
}

func q88(v float32) [2]byte {
	q := math.Round(float64(v) * 256.0)
	if q > 32767.0 {
		q = 32767.0
	}
	if q < -32768.0 {
		q = -32768.0
	}
	u := uint16(int16(int32(q)))
	return [2]byte{byte(u >> 8), byte(u)}
}

func unq88(b []byte) float32 {
	q := int16(uint16(b[0])<<8 | uint16(b[1]))
	return float32(q) / 256.0
}

// SnapshotPack packs a snapshot into 14 deterministic bytes. The byte layout is certified.
func (s Snapshot) Pack() [SnapshotLen]byte {
	var out [SnapshotLen]byte
	vals := [6]float32{s.X, s.Y, s.Z, s.Vx, s.Vy, s.Vz}
	for i, v := range vals {
		q := q88(v)
		out[i*2] = q[0]
		out[i*2+1] = q[1]
	}
	out[12] = byte(s.Yaw >> 8)
	out[13] = byte(s.Yaw)
	return out
}

// SnapshotUnpack unpacks 14 bytes into a snapshot. Pack(Unpack(b)) == b for any 14-byte b.
func SnapshotUnpack(b []byte) Snapshot {
	return Snapshot{
		X:   unq88(b[0:2]),
		Y:   unq88(b[2:4]),
		Z:   unq88(b[4:6]),
		Vx:  unq88(b[6:8]),
		Vy:  unq88(b[8:10]),
		Vz:  unq88(b[10:12]),
		Yaw: uint16(b[12])<<8 | uint16(b[13]),
	}
}

// ── L1: INPUT body (msg_type 1) — 6 bytes: tick u32 + buttons u16 bitfield ───

// InputLen is the fixed byte length of a packed Input.
const InputLen = 6

// Input is an input-frame body: a tick counter and a button bitfield.
type Input struct {
	Tick    uint32
	Buttons uint16
}

// Pack packs an input frame into 6 deterministic bytes.
func (p Input) Pack() [InputLen]byte {
	var out [InputLen]byte
	out[0] = byte(p.Tick >> 24)
	out[1] = byte(p.Tick >> 16)
	out[2] = byte(p.Tick >> 8)
	out[3] = byte(p.Tick)
	out[4] = byte(p.Buttons >> 8)
	out[5] = byte(p.Buttons)
	return out
}

// InputUnpack unpacks 6 bytes into an input frame.
func InputUnpack(b []byte) Input {
	return Input{
		Tick:    uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3]),
		Buttons: uint16(b[4])<<8 | uint16(b[5]),
	}
}

// ── L1: JOIN body (msg_type 3) — player_id u16 + len-prefixed UTF-8 name ─────

// JoinPack packs a lobby-join body: player_id (big-endian u16), a 1-byte name length, then
// the UTF-8 name (clamped to fit the 124-byte message cap).
func JoinPack(playerID uint16, name string) []byte {
	nb := []byte(name)
	n := len(nb)
	if n > MaxPayload-3 {
		n = MaxPayload - 3
	}
	out := make([]byte, 0, 3+n)
	out = append(out, byte(playerID>>8), byte(playerID), byte(n))
	out = append(out, nb[:n]...)
	return out
}

// JoinUnpack unpacks a lobby-join body. It returns ok=false if b is shorter than the
// 3-byte header.
func JoinUnpack(b []byte) (playerID uint16, name string, ok bool) {
	if len(b) < 3 {
		return 0, "", false
	}
	playerID = uint16(b[0])<<8 | uint16(b[1])
	n := int(b[2])
	end := 3 + n
	if end > len(b) {
		end = len(b)
	}
	return playerID, string(b[3:end]), true
}

// init self-certifies the L2 framing against the exampleGameMessage anchor on import,
// panicking (refuse to load) if the implementation has diverged from the reference.
func init() {
	// anchors.exampleGameMessage from Documentation/game_vectors.json.
	payload, _ := hex.DecodeString("cafebabe0042")
	want := []string{
		"d31002000001ffff06020201010203df12",
		"d31002010001ffffcafebabe0102036b6a",
		"d31002020001ffff0042000001020320fa",
	}
	frames, err := Packetize(MsgEvent, payload, 16, 66051, 1, 0xFFFF, FlagReliable)
	if err != nil {
		panic(fmt.Sprintf("game: exampleGameMessage anchor packetize failed: %v", err))
	}
	if len(frames) != len(want) {
		panic(fmt.Sprintf("game: exampleGameMessage anchor frame count = %d, want %d", len(frames), len(want)))
	}
	for i, f := range frames {
		if got := hex.EncodeToString(f[:]); got != want[i] {
			panic(fmt.Sprintf("game: exampleGameMessage anchor frame[%d] = %s, want %s", i, got, want[i]))
		}
	}
}
