// SPDX-License-Identifier: LGPL-3.0-only

package game

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"strconv"
	"testing"

	"github.com/ALH477/Punctim/go/dcf"
)

// gameVectors mirrors Documentation/game_vectors.json.
type gameVectors struct {
	Constants struct {
		FragBits      int `json:"frag_bits"`
		MaxFrags      int `json:"max_frags"`
		MaxPayload    int `json:"max_payload"`
		MaxPacketID   int `json:"max_packet_id"`
		FrameTypeData int `json:"frame_type_data"`
	} `json:"constants"`
	Anchors struct {
		ExampleGameMessage framingVec `json:"exampleGameMessage"`
	} `json:"anchors"`
	Framing    []framingVec    `json:"framing"`
	Reassembly []reassemblyVec `json:"reassembly"`
	// Snapshot fields (x/y/z/vx/vy/vz/yaw) are decoded generically in the test via
	// readRawVectorFields, so only the certified bytes are needed here.
	SnapshotRoundtrip []struct {
		Bytes string `json:"bytes"`
	} `json:"snapshot_roundtrip"`
	InputRoundtrip []struct {
		Bytes  string `json:"bytes"`
		Fields struct {
			Tick    uint32 `json:"tick"`
			Buttons uint16 `json:"buttons"`
		} `json:"fields"`
	} `json:"input_roundtrip"`
	JoinRoundtrip []struct {
		Fields struct {
			PlayerID uint16 `json:"player_id"`
			Name     string `json:"name"`
		} `json:"fields"`
		Bytes string `json:"bytes"`
	} `json:"join_roundtrip"`
}

type framingVec struct {
	MsgTypeID uint8    `json:"msg_type_id"`
	Src       uint16   `json:"src"`
	Dst       uint16   `json:"dst"`
	PacketID  uint16   `json:"packet_id"`
	TsUs      uint32   `json:"ts_us"`
	Flags     uint8    `json:"flags"`
	Payload   string   `json:"payload"`
	Frames    []string `json:"frames"`
}

type packetVec struct {
	PacketID  uint16 `json:"packet_id"`
	TsUs      uint32 `json:"ts_us"`
	MsgTypeID uint8  `json:"msg_type_id"`
	Flags     uint8  `json:"flags"`
	Payload   string `json:"payload"`
}

type reassemblyVec struct {
	Name        string      `json:"name"`
	InputFrames []string    `json:"input_frames"`
	Packets     []packetVec `json:"packets"`
	Lost        []int       `json:"lost"`
}

func loadGameVectors(t *testing.T) gameVectors {
	t.Helper()
	paths := []string{
		"../../Documentation/game_vectors.json",
		"../../python/MCP/game_vectors.json",
	}
	var lastErr error
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			lastErr = err
			continue
		}
		var gv gameVectors
		if err := json.Unmarshal(data, &gv); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		return gv
	}
	t.Fatalf("game_vectors.json not found in expected locations: %v", lastErr)
	return gameVectors{}
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

func TestGameConstants(t *testing.T) {
	gv := loadGameVectors(t)
	if gv.Constants.FragBits != FragBits {
		t.Errorf("frag_bits = %d, want %d", gv.Constants.FragBits, FragBits)
	}
	if gv.Constants.MaxFrags != MaxFrags {
		t.Errorf("max_frags = %d, want %d", gv.Constants.MaxFrags, MaxFrags)
	}
	if gv.Constants.MaxPayload != MaxPayload {
		t.Errorf("max_payload = %d, want %d", gv.Constants.MaxPayload, MaxPayload)
	}
	if gv.Constants.MaxPacketID != MaxPacketID {
		t.Errorf("max_packet_id = %d, want %d", gv.Constants.MaxPacketID, MaxPacketID)
	}
	if gv.Constants.FrameTypeData != int(dcf.FData) {
		t.Errorf("frame_type_data = %d, want %d", gv.Constants.FrameTypeData, dcf.FData)
	}
}

// assertFramingVec packetizes one vector and checks the frames hex-equal the certificate.
func assertFramingVec(t *testing.T, label string, v framingVec) {
	t.Helper()
	frames, err := Packetize(v.MsgTypeID, mustHex(t, v.Payload), v.PacketID, v.TsUs, v.Src, v.Dst, v.Flags)
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

func TestGameFramingAnchor(t *testing.T) {
	gv := loadGameVectors(t)
	assertFramingVec(t, "exampleGameMessage", gv.Anchors.ExampleGameMessage)
}

func TestGameFraming(t *testing.T) {
	gv := loadGameVectors(t)
	if len(gv.Framing) == 0 {
		t.Fatal("no framing vectors")
	}
	for i, v := range gv.Framing {
		assertFramingVec(t, "framing["+strconv.Itoa(i)+"]", v)
		// Also push the produced frames through a reassembler and confirm recovery.
		r := NewGameReassembler()
		var got *GamePacket
		for _, f := range v.Frames {
			raw := mustHex(t, f)
			var arr [17]byte
			copy(arr[:], raw)
			if p := r.Push(&arr); p != nil {
				got = p
			}
		}
		if got == nil {
			t.Errorf("framing[%d]: message did not reassemble", i)
			continue
		}
		if hex.EncodeToString(got.Payload) != v.Payload {
			t.Errorf("framing[%d]: recovered payload = %s, want %s", i, hex.EncodeToString(got.Payload), v.Payload)
		}
		if got.PacketID != v.PacketID || got.MsgTypeID != v.MsgTypeID || got.Flags != v.Flags || got.TsUs != v.TsUs {
			t.Errorf("framing[%d]: recovered meta = {pid:%d type:%d flags:%d ts:%d}, want {pid:%d type:%d flags:%d ts:%d}",
				i, got.PacketID, got.MsgTypeID, got.Flags, got.TsUs, v.PacketID, v.MsgTypeID, v.Flags, v.TsUs)
		}
	}
}

func TestGameReassembly(t *testing.T) {
	gv := loadGameVectors(t)
	if len(gv.Reassembly) == 0 {
		t.Fatal("no reassembly vectors")
	}
	for _, rc := range gv.Reassembly {
		r := NewGameReassembler()
		var emitted []*GamePacket
		for _, f := range rc.InputFrames {
			raw := mustHex(t, f)
			var arr [17]byte
			copy(arr[:], raw)
			if p := r.Push(&arr); p != nil {
				emitted = append(emitted, p)
			}
		}
		if len(emitted) != len(rc.Packets) {
			t.Fatalf("reassembly[%s]: emitted %d packets, want %d", rc.Name, len(emitted), len(rc.Packets))
		}
		for i, want := range rc.Packets {
			got := emitted[i]
			if got.PacketID != want.PacketID || got.TsUs != want.TsUs || got.MsgTypeID != want.MsgTypeID || got.Flags != want.Flags {
				t.Errorf("reassembly[%s].packet[%d]: meta = {pid:%d ts:%d type:%d flags:%d}, want {pid:%d ts:%d type:%d flags:%d}",
					rc.Name, i, got.PacketID, got.TsUs, got.MsgTypeID, got.Flags, want.PacketID, want.TsUs, want.MsgTypeID, want.Flags)
			}
			if hex.EncodeToString(got.Payload) != want.Payload {
				t.Errorf("reassembly[%s].packet[%d]: payload = %s, want %s", rc.Name, i, hex.EncodeToString(got.Payload), want.Payload)
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

func TestGameSnapshotRoundtrip(t *testing.T) {
	gv := loadGameVectors(t)
	if len(gv.SnapshotRoundtrip) == 0 {
		t.Fatal("no snapshot_roundtrip vectors")
	}
	// Re-decode the fields generically to avoid struct-tag aliasing for x/y/z/vx/vy/vz.
	raw := readRawVectorFields(t, "snapshot_roundtrip")
	for i := range gv.SnapshotRoundtrip {
		v := gv.SnapshotRoundtrip[i]
		fields := raw[i]["fields"].(map[string]any)
		s := Snapshot{
			X:   float32(fields["x"].(float64)),
			Y:   float32(fields["y"].(float64)),
			Z:   float32(fields["z"].(float64)),
			Vx:  float32(fields["vx"].(float64)),
			Vy:  float32(fields["vy"].(float64)),
			Vz:  float32(fields["vz"].(float64)),
			Yaw: uint16(fields["yaw"].(float64)),
		}
		packed := s.Pack()
		if got := hex.EncodeToString(packed[:]); got != v.Bytes {
			t.Errorf("snapshot[%d]: pack = %s, want %s", i, got, v.Bytes)
		}
		un := SnapshotUnpack(mustHex(t, v.Bytes))
		if un != s {
			t.Errorf("snapshot[%d]: unpack = %+v, want %+v", i, un, s)
		}
	}
}

func TestGameInputRoundtrip(t *testing.T) {
	gv := loadGameVectors(t)
	if len(gv.InputRoundtrip) == 0 {
		t.Fatal("no input_roundtrip vectors")
	}
	for i, v := range gv.InputRoundtrip {
		in := Input{Tick: v.Fields.Tick, Buttons: v.Fields.Buttons}
		packed := in.Pack()
		if got := hex.EncodeToString(packed[:]); got != v.Bytes {
			t.Errorf("input[%d]: pack = %s, want %s", i, got, v.Bytes)
		}
		un := InputUnpack(mustHex(t, v.Bytes))
		if un != in {
			t.Errorf("input[%d]: unpack = %+v, want %+v", i, un, in)
		}
	}
}

func TestGameJoinRoundtrip(t *testing.T) {
	gv := loadGameVectors(t)
	if len(gv.JoinRoundtrip) == 0 {
		t.Fatal("no join_roundtrip vectors")
	}
	for i, v := range gv.JoinRoundtrip {
		packed := JoinPack(v.Fields.PlayerID, v.Fields.Name)
		if got := hex.EncodeToString(packed); got != v.Bytes {
			t.Errorf("join[%d]: pack = %s, want %s", i, got, v.Bytes)
		}
		pid, name, ok := JoinUnpack(mustHex(t, v.Bytes))
		if !ok || pid != v.Fields.PlayerID || name != v.Fields.Name {
			t.Errorf("join[%d]: unpack = (%d,%q,%v), want (%d,%q)", i, pid, name, ok, v.Fields.PlayerID, v.Fields.Name)
		}
	}
}

// readRawVectorFields re-reads game_vectors.json as generic maps for a named list key, so
// fields with map-key collisions (x/y/z) decode unambiguously.
func readRawVectorFields(t *testing.T, key string) []map[string]any {
	t.Helper()
	paths := []string{
		"../../Documentation/game_vectors.json",
		"../../python/MCP/game_vectors.json",
	}
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		var raw map[string]any
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		arr := raw[key].([]any)
		out := make([]map[string]any, len(arr))
		for i, e := range arr {
			out[i] = e.(map[string]any)
		}
		return out
	}
	t.Fatalf("game_vectors.json not found")
	return nil
}
