// SPDX-License-Identifier: LGPL-3.0-only

package audio

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"os"
	"strconv"
	"testing"

	"github.com/ALH477/Punctim/go/dcf"
)

// audioVectors mirrors Documentation/audio_vectors.json.
type audioVectors struct {
	Constants struct {
		FragBits      int `json:"frag_bits"`
		MaxFrags      int `json:"max_frags"`
		MaxPayload    int `json:"max_payload"`
		MaxPacketID   int `json:"max_packet_id"`
		FrameTypeCtrl int `json:"frame_type_ctrl"`
	} `json:"constants"`
	Anchors struct {
		ExampleAudioPacket framingVec `json:"exampleAudioPacket"`
	} `json:"anchors"`
	Framing      []framingVec    `json:"framing"`
	Reassembly   []reassemblyVec `json:"reassembly"`
	PcmRoundtrip []struct {
		Bytes string `json:"bytes"`
	} `json:"pcm_roundtrip"`
}

type framingVec struct {
	CodecID  uint8    `json:"codec_id"`
	Src      uint16   `json:"src"`
	Dst      uint16   `json:"dst"`
	PacketID uint16   `json:"packet_id"`
	TsUs     uint32   `json:"ts_us"`
	Flags    uint8    `json:"flags"`
	Payload  string   `json:"payload"`
	Frames   []string `json:"frames"`
}

type packetVec struct {
	PacketID uint16 `json:"packet_id"`
	TsUs     uint32 `json:"ts_us"`
	CodecID  uint8  `json:"codec_id"`
	Flags    uint8  `json:"flags"`
	Payload  string `json:"payload"`
}

type reassemblyVec struct {
	Name        string      `json:"name"`
	InputFrames []string    `json:"input_frames"`
	Packets     []packetVec `json:"packets"`
	Lost        []int       `json:"lost"`
}

// pmVectors mirrors Documentation/pm_param_vectors.json.
type pmVectors struct {
	Cases []struct {
		Params struct {
			F0       uint16 `json:"f0"`
			Amp      uint8  `json:"amp"`
			ModIndex uint8  `json:"mod_index"`
			ModRatio uint8  `json:"mod_ratio"`
			Bright   uint8  `json:"bright"`
			Env      uint8  `json:"env"`
			Flags    uint8  `json:"flags"`
		} `json:"params"`
		Bytes string `json:"bytes"`
	} `json:"cases"`
}

func loadAudioVectors(t *testing.T) audioVectors {
	t.Helper()
	paths := []string{
		"../../Documentation/audio_vectors.json",
		"../../python/MCP/audio_vectors.json",
	}
	var lastErr error
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			lastErr = err
			continue
		}
		var av audioVectors
		if err := json.Unmarshal(data, &av); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		return av
	}
	t.Fatalf("audio_vectors.json not found in expected locations: %v", lastErr)
	return audioVectors{}
}

func loadPmVectors(t *testing.T) pmVectors {
	t.Helper()
	paths := []string{
		"../../Documentation/pm_param_vectors.json",
		"../../python/MCP/pm_param_vectors.json",
	}
	var lastErr error
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			lastErr = err
			continue
		}
		var pv pmVectors
		if err := json.Unmarshal(data, &pv); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		return pv
	}
	t.Fatalf("pm_param_vectors.json not found in expected locations: %v", lastErr)
	return pmVectors{}
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

func TestAudioConstants(t *testing.T) {
	av := loadAudioVectors(t)
	if av.Constants.FragBits != FragBits {
		t.Errorf("frag_bits = %d, want %d", av.Constants.FragBits, FragBits)
	}
	if av.Constants.MaxFrags != MaxFrags {
		t.Errorf("max_frags = %d, want %d", av.Constants.MaxFrags, MaxFrags)
	}
	if av.Constants.MaxPayload != MaxPayload {
		t.Errorf("max_payload = %d, want %d", av.Constants.MaxPayload, MaxPayload)
	}
	if av.Constants.MaxPacketID != MaxPacketID {
		t.Errorf("max_packet_id = %d, want %d", av.Constants.MaxPacketID, MaxPacketID)
	}
	if av.Constants.FrameTypeCtrl != int(dcf.FCtrl) {
		t.Errorf("frame_type_ctrl = %d, want %d", av.Constants.FrameTypeCtrl, dcf.FCtrl)
	}
}

func assertFramingVec(t *testing.T, label string, v framingVec) {
	t.Helper()
	frames, err := Packetize(v.CodecID, mustHex(t, v.Payload), v.PacketID, v.TsUs, v.Src, v.Dst, v.Flags)
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

func TestAudioFramingAnchor(t *testing.T) {
	av := loadAudioVectors(t)
	assertFramingVec(t, "exampleAudioPacket", av.Anchors.ExampleAudioPacket)
}

func TestAudioFraming(t *testing.T) {
	av := loadAudioVectors(t)
	if len(av.Framing) == 0 {
		t.Fatal("no framing vectors")
	}
	for i, v := range av.Framing {
		assertFramingVec(t, "framing["+strconv.Itoa(i)+"]", v)
		// Round-trip through a reassembler.
		r := NewAudioReassembler()
		var got *AudioPacket
		for _, f := range v.Frames {
			var arr [17]byte
			copy(arr[:], mustHex(t, f))
			if p := r.Push(&arr); p != nil {
				got = p
			}
		}
		if got == nil {
			t.Errorf("framing[%d]: packet did not reassemble", i)
			continue
		}
		if hex.EncodeToString(got.Payload) != v.Payload {
			t.Errorf("framing[%d]: recovered payload = %s, want %s", i, hex.EncodeToString(got.Payload), v.Payload)
		}
		if got.PacketID != v.PacketID || got.CodecID != v.CodecID || got.Flags != v.Flags || got.TsUs != v.TsUs {
			t.Errorf("framing[%d]: recovered meta = {pid:%d codec:%d flags:%d ts:%d}, want {pid:%d codec:%d flags:%d ts:%d}",
				i, got.PacketID, got.CodecID, got.Flags, got.TsUs, v.PacketID, v.CodecID, v.Flags, v.TsUs)
		}
	}
}

func TestAudioReassembly(t *testing.T) {
	av := loadAudioVectors(t)
	if len(av.Reassembly) == 0 {
		t.Fatal("no reassembly vectors")
	}
	for _, rc := range av.Reassembly {
		r := NewAudioReassembler()
		var emitted []*AudioPacket
		for _, f := range rc.InputFrames {
			var arr [17]byte
			copy(arr[:], mustHex(t, f))
			if p := r.Push(&arr); p != nil {
				emitted = append(emitted, p)
			}
		}
		if len(emitted) != len(rc.Packets) {
			t.Fatalf("reassembly[%s]: emitted %d packets, want %d", rc.Name, len(emitted), len(rc.Packets))
		}
		for i, want := range rc.Packets {
			got := emitted[i]
			if got.PacketID != want.PacketID || got.TsUs != want.TsUs || got.CodecID != want.CodecID || got.Flags != want.Flags {
				t.Errorf("reassembly[%s].packet[%d]: meta = {pid:%d ts:%d codec:%d flags:%d}, want {pid:%d ts:%d codec:%d flags:%d}",
					rc.Name, i, got.PacketID, got.TsUs, got.CodecID, got.Flags, want.PacketID, want.TsUs, want.CodecID, want.Flags)
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

// TestAudioPcmRoundtrip asserts the diagnostic codec is byte-lossless: Encode(Decode(b)) == b.
func TestAudioPcmRoundtrip(t *testing.T) {
	av := loadAudioVectors(t)
	if len(av.PcmRoundtrip) == 0 {
		t.Fatal("no pcm_roundtrip vectors")
	}
	for i, v := range av.PcmRoundtrip {
		raw := mustHex(t, v.Bytes)
		got := PcmDiagEncode(PcmDiagDecode(raw))
		if !bytes.Equal(got, raw) {
			t.Errorf("pcm_roundtrip[%d]: Encode(Decode(b)) = %s, want %s", i, hex.EncodeToString(got), v.Bytes)
		}
	}
}

func TestAudioPmParams(t *testing.T) {
	pv := loadPmVectors(t)
	if len(pv.Cases) == 0 {
		t.Fatal("no pm param cases")
	}
	for i, c := range pv.Cases {
		p := PmParams{
			F0:       c.Params.F0,
			Amp:      c.Params.Amp,
			ModIndex: c.Params.ModIndex,
			ModRatio: c.Params.ModRatio,
			Bright:   c.Params.Bright,
			Env:      c.Params.Env,
			Flags:    c.Params.Flags,
		}
		packed := p.Pack()
		if got := hex.EncodeToString(packed[:]); got != c.Bytes {
			t.Errorf("pm[%d]: pack = %s, want %s", i, got, c.Bytes)
		}
		un := PmUnpack(mustHex(t, c.Bytes))
		if un != p {
			t.Errorf("pm[%d]: unpack = %+v, want %+v", i, un, p)
		}
	}
}
