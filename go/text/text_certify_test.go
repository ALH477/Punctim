// SPDX-License-Identifier: LGPL-3.0-only

package text

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"strconv"
	"testing"

	"github.com/ALH477/Punctim/go/dcf"
)

// textVectors mirrors Documentation/text_vectors.json.
type textVectors struct {
	Constants struct {
		FragBits      int `json:"frag_bits"`
		MaxFrags      int `json:"max_frags"`
		MaxPayload    int `json:"max_payload"`
		MaxPacketID   int `json:"max_packet_id"`
		FrameTypeData int `json:"frame_type_data"`
		FlagAgent     int `json:"flag_agent"`
		FlagMore      int `json:"flag_more"`
		FlagReliable  int `json:"flag_reliable"`
	} `json:"constants"`
	Anchors struct {
		ExampleTextMessage anchorVec `json:"exampleTextMessage"`
	} `json:"anchors"`
	Framing    []framingVec    `json:"framing"`
	Reassembly []reassemblyVec `json:"reassembly"`
}

type framingVec struct {
	Src      uint16   `json:"src"`
	Dst      uint16   `json:"dst"`
	PacketID uint16   `json:"packet_id"`
	TsUs     uint32   `json:"ts_us"`
	Flags    uint8    `json:"flags"`
	Payload  string   `json:"payload"`
	Frames   []string `json:"frames"`
}

// anchorVec adds the human-readable text/dst the anchor pins (UTF-8 + ChannelID checks).
type anchorVec struct {
	PacketID uint16   `json:"packet_id"`
	TsUs     uint32   `json:"ts_us"`
	Src      uint16   `json:"src"`
	Dst      uint16   `json:"dst"`
	Flags    uint8    `json:"flags"`
	Text     string   `json:"text"`
	Payload  string   `json:"payload"`
	Frames   []string `json:"frames"`
}

type messageVec struct {
	PacketID uint16 `json:"packet_id"`
	TsUs     uint32 `json:"ts_us"`
	Src      uint16 `json:"src"`
	Dst      uint16 `json:"dst"`
	Flags    uint8  `json:"flags"`
	Text     string `json:"text"`
	Payload  string `json:"payload"`
}

type reassemblyVec struct {
	Name        string       `json:"name"`
	InputFrames []string     `json:"input_frames"`
	Messages    []messageVec `json:"messages"`
	Lost        []int        `json:"lost"`
}

func loadTextVectors(t *testing.T) textVectors {
	t.Helper()
	paths := []string{
		"../../Documentation/text_vectors.json",
		"../../python/MCP/text_vectors.json",
	}
	var lastErr error
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			lastErr = err
			continue
		}
		var tv textVectors
		if err := json.Unmarshal(data, &tv); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		return tv
	}
	t.Fatalf("text_vectors.json not found in expected locations: %v", lastErr)
	return textVectors{}
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

func TestTextConstants(t *testing.T) {
	tv := loadTextVectors(t)
	if tv.Constants.FragBits != FragBits {
		t.Errorf("frag_bits = %d, want %d", tv.Constants.FragBits, FragBits)
	}
	if tv.Constants.MaxFrags != MaxFrags {
		t.Errorf("max_frags = %d, want %d", tv.Constants.MaxFrags, MaxFrags)
	}
	if tv.Constants.MaxPayload != MaxPayload {
		t.Errorf("max_payload = %d, want %d", tv.Constants.MaxPayload, MaxPayload)
	}
	if tv.Constants.MaxPacketID != MaxPacketID {
		t.Errorf("max_packet_id = %d, want %d", tv.Constants.MaxPacketID, MaxPacketID)
	}
	if tv.Constants.FrameTypeData != int(dcf.FData) {
		t.Errorf("frame_type_data = %d, want %d", tv.Constants.FrameTypeData, dcf.FData)
	}
	if tv.Constants.FlagAgent != int(FlagAgent) || tv.Constants.FlagMore != int(FlagMore) || tv.Constants.FlagReliable != int(FlagReliable) {
		t.Errorf("flags = {agent:%d more:%d reliable:%d}, want {%d %d %d}",
			tv.Constants.FlagAgent, tv.Constants.FlagMore, tv.Constants.FlagReliable, FlagAgent, FlagMore, FlagReliable)
	}
}

func assertFramingVec(t *testing.T, label string, v framingVec) {
	t.Helper()
	frames, err := Packetize(mustHex(t, v.Payload), v.PacketID, v.TsUs, v.Src, v.Dst, v.Flags)
	if err != nil {
		t.Fatalf("%s: packetize failed: %v", label, err)
	}
	if len(frames) != len(v.Frames) {
		t.Fatalf("%s: frame count = %d, want %d", label, len(frames), len(v.Frames))
	}
	for i, f := range frames {
		if got := hex.EncodeToString(f[:]); got != v.Frames[i] {
			t.Errorf("%s: frame[%d] = %s, want %s", label, i, got, v.Frames[i])
		}
	}
}

// TestTextAnchor pins the exampleTextMessage: framing bytes, the UTF-8 text it carries, and
// ChannelID (dst 61143 is the hash of a channel; broadcast for empty; CRC anchor for digits).
func TestTextAnchor(t *testing.T) {
	tv := loadTextVectors(t)
	a := tv.Anchors.ExampleTextMessage
	frames, err := Packetize(mustHex(t, a.Payload), a.PacketID, a.TsUs, a.Src, a.Dst, a.Flags)
	if err != nil {
		t.Fatalf("anchor packetize: %v", err)
	}
	if len(frames) != len(a.Frames) {
		t.Fatalf("anchor frame count = %d, want %d", len(frames), len(a.Frames))
	}
	for i, f := range frames {
		if got := hex.EncodeToString(f[:]); got != a.Frames[i] {
			t.Errorf("anchor frame[%d] = %s, want %s", i, got, a.Frames[i])
		}
	}
	// The payload is the UTF-8 of the human text; confirm both directions.
	if string(mustHex(t, a.Payload)) != a.Text {
		t.Errorf("anchor payload != UTF-8 text %q", a.Text)
	}
	// Reassemble and confirm the recovered Text equals the anchor text.
	r := NewTextReassembler()
	var got *TextPacket
	for _, f := range a.Frames {
		var arr [17]byte
		copy(arr[:], mustHex(t, f))
		if p := r.Push(&arr); p != nil {
			got = p
		}
	}
	if got == nil || got.Text != a.Text {
		t.Errorf("anchor reassembled text = %q, want %q", textOrNil(got), a.Text)
	}
	if got != nil && got.Dst != a.Dst {
		t.Errorf("anchor reassembled dst = %d, want %d", got.Dst, a.Dst)
	}
	if c := ChannelID("123456789"); c != 0x29B1 {
		t.Errorf("ChannelID(\"123456789\") = 0x%04X, want 0x29B1", c)
	}
	if c := ChannelID(""); c != Broadcast {
		t.Errorf("ChannelID(\"\") = 0x%04X, want broadcast 0x%04X", c, Broadcast)
	}
}

func textOrNil(p *TextPacket) string {
	if p == nil {
		return "<nil>"
	}
	return p.Text
}

func TestTextFraming(t *testing.T) {
	tv := loadTextVectors(t)
	if len(tv.Framing) == 0 {
		t.Fatal("no framing vectors")
	}
	for i, v := range tv.Framing {
		assertFramingVec(t, "framing["+strconv.Itoa(i)+"]", v)
		// Round-trip through a reassembler.
		r := NewTextReassembler()
		var got *TextPacket
		for _, f := range v.Frames {
			var arr [17]byte
			copy(arr[:], mustHex(t, f))
			if p := r.Push(&arr); p != nil {
				got = p
			}
		}
		if got == nil {
			t.Errorf("framing[%d]: message did not reassemble", i)
			continue
		}
		if hex.EncodeToString([]byte(got.Text)) != v.Payload {
			t.Errorf("framing[%d]: recovered bytes = %s, want %s", i, hex.EncodeToString([]byte(got.Text)), v.Payload)
		}
		if got.PacketID != v.PacketID || got.Src != v.Src || got.Dst != v.Dst || got.Flags != v.Flags || got.TsUs != v.TsUs {
			t.Errorf("framing[%d]: recovered meta = {pid:%d src:%d dst:%d flags:%d ts:%d}, want {pid:%d src:%d dst:%d flags:%d ts:%d}",
				i, got.PacketID, got.Src, got.Dst, got.Flags, got.TsUs, v.PacketID, v.Src, v.Dst, v.Flags, v.TsUs)
		}
	}
}

func TestTextReassembly(t *testing.T) {
	tv := loadTextVectors(t)
	if len(tv.Reassembly) == 0 {
		t.Fatal("no reassembly vectors")
	}
	for _, rc := range tv.Reassembly {
		r := NewTextReassembler()
		var emitted []*TextPacket
		for _, f := range rc.InputFrames {
			var arr [17]byte
			copy(arr[:], mustHex(t, f))
			if p := r.Push(&arr); p != nil {
				emitted = append(emitted, p)
			}
		}
		if len(emitted) != len(rc.Messages) {
			t.Fatalf("reassembly[%s]: emitted %d messages, want %d", rc.Name, len(emitted), len(rc.Messages))
		}
		for i, want := range rc.Messages {
			got := emitted[i]
			if got.PacketID != want.PacketID || got.TsUs != want.TsUs || got.Src != want.Src || got.Dst != want.Dst || got.Flags != want.Flags {
				t.Errorf("reassembly[%s].msg[%d]: meta = {pid:%d ts:%d src:%d dst:%d flags:%d}, want {pid:%d ts:%d src:%d dst:%d flags:%d}",
					rc.Name, i, got.PacketID, got.TsUs, got.Src, got.Dst, got.Flags, want.PacketID, want.TsUs, want.Src, want.Dst, want.Flags)
			}
			if got.Text != want.Text {
				t.Errorf("reassembly[%s].msg[%d]: text = %q, want %q", rc.Name, i, got.Text, want.Text)
			}
			if hex.EncodeToString([]byte(got.Text)) != want.Payload {
				t.Errorf("reassembly[%s].msg[%d]: bytes = %s, want %s", rc.Name, i, hex.EncodeToString([]byte(got.Text)), want.Payload)
			}
		}
		lost := r.Finalize()
		if len(lost) != len(rc.Lost) {
			t.Fatalf("reassembly[%s]: lost = %v, want %v", rc.Name, lost, rc.Lost)
		}
		for i, want := range rc.Lost {
			if int(lost[i]) != want {
				t.Errorf("reassembly[%s]: lost[%d] = %d, want %d", rc.Name, i, lost[i], want)
			}
		}
	}
}
