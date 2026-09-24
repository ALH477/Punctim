// SPDX-License-Identifier: LGPL-3.0-only

// Package node provides a stdlib-only UDP DcfNode for Punctim/DCF, mirroring the
// working subset of the Rust reference SDK (rust/src/lib.rs) and wiring in the certified
// DCF-Game, DCF-Audio, and DCF-Text adapters (the go/game, go/audio, go/text packages).
//
// The transport envelope is the binary ProtoMessage (this file); every node datagram is a
// serialised ProtoMessage. The adapters fragment a logical message into 17-byte DeModFrames,
// each of which is shipped as one ProtoMessage payload and reassembled on the far side.
package node

import (
	"encoding/binary"
	"errors"
	"time"
)

// Message-type registry — mirrors rust/src/lib.rs `mod msg_type`.
const (
	MsgPosition  uint8 = 1 // Position update (unreliable, high frequency)
	MsgAudio     uint8 = 2 // Raw audio / DCF-Audio L2 frame payload
	MsgGameEvent uint8 = 3 // GameEvent (reliable, critical)
	MsgStateSync uint8 = 4
	MsgReliable  uint8 = 5
	MsgAck       uint8 = 6
	MsgPing      uint8 = 7
	MsgPong      uint8 = 8
	MsgGameDCF   uint8 = 9 // One DCF-Game L2 frame (a single 17-byte DeModFrame DATA frame)

	// MsgTextDCF carries one DCF-Text L2 frame (a single 17-byte DeModFrame DATA frame).
	// Go extension — not yet in the Rust SDK; back-port for parity.
	MsgTextDCF uint8 = 10

	// MsgMesh carries a DCF-Mesh control message (REPORT/ROLE; see go/mesh) for the
	// self-healing runtime's AUTO/master role assignment.
	MsgMesh uint8 = 11

	// MsgFrame carries exactly one raw 17-byte DeModFrame (payload_len == 17) — the
	// `udp:dialect=proto` medium of Documentation/DCF_MEDIUM_SPEC.md, shared with the
	// Python dcf.transport UDP medium and every `punctim io`.
	MsgFrame uint8 = 12
)

// protoHeaderLen is the fixed ProtoMessage header size:
// type(1) + sequence(4) + timestamp(8) + payload_len(4).
const protoHeaderLen = 1 + 4 + 8 + 4 // 17

// Deserialize errors.
var (
	ErrShortMessage   = errors.New("node: message shorter than 17-byte header")
	ErrPayloadOverrun = errors.New("node: payload length exceeds message size")
)

// ProtoMessage is the binary UDP transport envelope, byte-compatible with the Rust
// reference's ProtoMessage::serialize: a 17-byte big-endian header followed by the payload.
//
// Wire layout (big-endian, matching rust/src/lib.rs):
//
//	[0]      msg_type     u8
//	[1:5]    sequence     u32
//	[5:13]   timestamp    u64  (micros since the Unix epoch)
//	[13:17]  payload_len  u32
//	[17:...] payload      payload_len bytes
type ProtoMessage struct {
	MsgType   uint8
	Sequence  uint32
	Timestamp uint64
	Payload   []byte
}

// NewProtoMessage builds a ProtoMessage stamped with the current time (micros since the
// Unix epoch), mirroring ProtoMessage::new in the Rust SDK.
func NewProtoMessage(msgType uint8, seq uint32, payload []byte) *ProtoMessage {
	return &ProtoMessage{
		MsgType:   msgType,
		Sequence:  seq,
		Timestamp: uint64(time.Now().UnixMicro()),
		Payload:   payload,
	}
}

// Serialize encodes the message into a big-endian byte slice (17-byte header + payload),
// byte-identical to the Rust reference's ProtoMessage::serialize.
func (m *ProtoMessage) Serialize() []byte {
	out := make([]byte, protoHeaderLen+len(m.Payload))
	out[0] = m.MsgType
	binary.BigEndian.PutUint32(out[1:5], m.Sequence)
	binary.BigEndian.PutUint64(out[5:13], m.Timestamp)
	binary.BigEndian.PutUint32(out[13:17], uint32(len(m.Payload)))
	copy(out[17:], m.Payload)
	return out
}

// Deserialize parses a big-endian ProtoMessage, guarding against a short header and a
// payload-length field that overruns the buffer (mirrors the Rust deserialize guards).
func Deserialize(data []byte) (ProtoMessage, error) {
	if len(data) < protoHeaderLen {
		return ProtoMessage{}, ErrShortMessage
	}
	msgType := data[0]
	seq := binary.BigEndian.Uint32(data[1:5])
	ts := binary.BigEndian.Uint64(data[5:13])
	payloadLen := int(binary.BigEndian.Uint32(data[13:17]))
	if len(data) < protoHeaderLen+payloadLen {
		return ProtoMessage{}, ErrPayloadOverrun
	}
	payload := make([]byte, payloadLen)
	copy(payload, data[protoHeaderLen:protoHeaderLen+payloadLen])
	return ProtoMessage{MsgType: msgType, Sequence: seq, Timestamp: ts, Payload: payload}, nil
}
