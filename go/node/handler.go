// SPDX-License-Identifier: LGPL-3.0-only

package node

import (
	"encoding/binary"
	"errors"
	"math"
	"net"
)

// Position is a 3-D position update (12 bytes: x, y, z as big-endian float32), mirroring the
// Rust SDK's Position.
type Position struct {
	X, Y, Z float32
}

// Encode serialises the position into 12 big-endian bytes.
func (p Position) Encode() []byte {
	out := make([]byte, 12)
	binary.BigEndian.PutUint32(out[0:4], math.Float32bits(p.X))
	binary.BigEndian.PutUint32(out[4:8], math.Float32bits(p.Y))
	binary.BigEndian.PutUint32(out[8:12], math.Float32bits(p.Z))
	return out
}

// ErrShortPosition is returned when a position payload is shorter than 12 bytes.
var ErrShortPosition = errors.New("node: position payload too short")

// DecodePosition parses 12 big-endian bytes into a Position.
func DecodePosition(data []byte) (Position, error) {
	if len(data) < 12 {
		return Position{}, ErrShortPosition
	}
	return Position{
		X: math.Float32frombits(binary.BigEndian.Uint32(data[0:4])),
		Y: math.Float32frombits(binary.BigEndian.Uint32(data[4:8])),
		Z: math.Float32frombits(binary.BigEndian.Uint32(data[8:12])),
	}, nil
}

// GameEvent is an application game event (a type byte plus a UTF-8 string), mirroring the
// Rust SDK's GameEvent. It rides MsgGameEvent (distinct from the DCF-Game adapter on
// MsgGameDCF).
type GameEvent struct {
	EventType uint8
	Data      string
}

// Encode serialises the event: a 1-byte type followed by the UTF-8 data.
func (e GameEvent) Encode() []byte {
	out := make([]byte, 0, 1+len(e.Data))
	out = append(out, e.EventType)
	out = append(out, e.Data...)
	return out
}

// ErrEmptyEvent is returned when a game-event payload is empty.
var ErrEmptyEvent = errors.New("node: event payload too short")

// DecodeGameEvent parses an event payload (type byte + UTF-8 data).
func DecodeGameEvent(data []byte) (GameEvent, error) {
	if len(data) == 0 {
		return GameEvent{}, ErrEmptyEvent
	}
	return GameEvent{EventType: data[0], Data: string(data[1:])}, nil
}

// MessageHandler receives decoded application messages from the UDP receiver. The DCF-Game
// and DCF-Text adapter callbacks (HandleGame/HandleText) each deliver a single 17-byte
// DeModFrame payload: feed it to a per-source reassembler (ReassembleGamePayload /
// ReassembleTextPayload) to recover whole messages. Mirrors the Rust MessageHandler trait
// (with handle_text added as a Go extension).
type MessageHandler interface {
	HandlePosition(pos Position, from *net.UDPAddr)
	HandleAudio(data []byte, from *net.UDPAddr)
	HandleGameEvent(ev GameEvent, from *net.UDPAddr)
	// HandleGame receives one DCF-Game L2 frame (a single 17-byte DeModFrame DATA frame).
	HandleGame(payload []byte, from *net.UDPAddr)
	// HandleText receives one DCF-Text L2 frame (a single 17-byte DeModFrame DATA frame).
	HandleText(payload []byte, from *net.UDPAddr)
	// HandleFrame receives one raw 17-byte DeModFrame carried as a MsgFrame (12) envelope
	// (the `udp:dialect=proto` medium). It is NOT gated here; callers validate if needed.
	HandleFrame(frame []byte, from *net.UDPAddr)
}

// DefaultMessageHandler is a no-op MessageHandler. Embed it to implement only the callbacks
// you care about (mirrors the Rust DefaultMessageHandler / defaulted trait methods).
type DefaultMessageHandler struct{}

func (DefaultMessageHandler) HandlePosition(Position, *net.UDPAddr)   {}
func (DefaultMessageHandler) HandleAudio([]byte, *net.UDPAddr)        {}
func (DefaultMessageHandler) HandleGameEvent(GameEvent, *net.UDPAddr) {}
func (DefaultMessageHandler) HandleGame([]byte, *net.UDPAddr)         {}
func (DefaultMessageHandler) HandleText([]byte, *net.UDPAddr)         {}
func (DefaultMessageHandler) HandleFrame([]byte, *net.UDPAddr)        {}
