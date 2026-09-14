// SPDX-License-Identifier: LGPL-3.0-only

package sstv

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"strconv"
	"testing"

	"github.com/ALH477/Punctim/go/dcf"
)

// sstvVectors mirrors Documentation/sstv_vectors.json.
type sstvVectors struct {
	Constants struct {
		FragBits      int `json:"frag_bits"`
		MaxFrags      int `json:"max_frags"`
		MaxPayload    int `json:"max_payload"`
		MaxImageID    int `json:"max_image_id"`
		FrameTypeData int `json:"frame_type_data"`
		FormatRaw     int `json:"format_raw"`
		FormatJPEG    int `json:"format_jpeg"`
		FlagMore      int `json:"flag_more"`
		FlagKeyframe  int `json:"flag_keyframe"`
		FlagReliable  int `json:"flag_reliable"`
	} `json:"constants"`
	Anchors struct {
		ExampleImage anchorVec `json:"exampleImage"`
	} `json:"anchors"`
	Framing    []framingVec    `json:"framing"`
	Reassembly []reassemblyVec `json:"reassembly"`
}

type framingVec struct {
	Src      uint16   `json:"src"`
	Dst      uint16   `json:"dst"`
	ImageID  uint16   `json:"image_id"`
	TsUs     uint32   `json:"ts_us"`
	FormatID uint8    `json:"format_id"`
	Flags    uint8    `json:"flags"`
	Payload  string   `json:"payload"`
	Frames   []string `json:"frames"`
}

type anchorVec struct {
	ImageID  uint16   `json:"image_id"`
	TsUs     uint32   `json:"ts_us"`
	Src      uint16   `json:"src"`
	Dst      uint16   `json:"dst"`
	FormatID uint8    `json:"format_id"`
	Flags    uint8    `json:"flags"`
	Payload  string   `json:"payload"`
	Frames   []string `json:"frames"`
}

type imageVec struct {
	ImageID  uint16 `json:"image_id"`
	TsUs     uint32 `json:"ts_us"`
	Src      uint16 `json:"src"`
	Dst      uint16 `json:"dst"`
	FormatID uint8  `json:"format_id"`
	Flags    uint8  `json:"flags"`
	Payload  string `json:"payload"`
}

type reassemblyVec struct {
	Name        string     `json:"name"`
	InputFrames []string   `json:"input_frames"`
	Images      []imageVec `json:"images"`
	Lost        []int      `json:"lost"`
}

func loadSstvVectors(t *testing.T) sstvVectors {
	t.Helper()
	paths := []string{
		"../../Documentation/sstv_vectors.json",
		"../../python/MCP/sstv_vectors.json",
	}
	var lastErr error
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			lastErr = err
			continue
		}
		var sv sstvVectors
		if err := json.Unmarshal(data, &sv); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		return sv
	}
	t.Fatalf("sstv_vectors.json not found in expected locations: %v", lastErr)
	return sstvVectors{}
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

func TestSstvConstants(t *testing.T) {
	sv := loadSstvVectors(t)
	if sv.Constants.FragBits != FragBits {
		t.Errorf("frag_bits = %d, want %d", sv.Constants.FragBits, FragBits)
	}
	if sv.Constants.MaxFrags != MaxFrags {
		t.Errorf("max_frags = %d, want %d", sv.Constants.MaxFrags, MaxFrags)
	}
	if sv.Constants.MaxPayload != MaxPayload {
		t.Errorf("max_payload = %d, want %d", sv.Constants.MaxPayload, MaxPayload)
	}
	if sv.Constants.MaxImageID != MaxImageID {
		t.Errorf("max_image_id = %d, want %d", sv.Constants.MaxImageID, MaxImageID)
	}
	if sv.Constants.FrameTypeData != int(dcf.FData) {
		t.Errorf("frame_type_data = %d, want %d", sv.Constants.FrameTypeData, dcf.FData)
	}
	if sv.Constants.FlagMore != int(FlagMore) || sv.Constants.FlagKeyframe != int(FlagKeyframe) || sv.Constants.FlagReliable != int(FlagReliable) {
		t.Errorf("flags = {more:%d keyframe:%d reliable:%d}, want {%d %d %d}",
			sv.Constants.FlagMore, sv.Constants.FlagKeyframe, sv.Constants.FlagReliable, FlagMore, FlagKeyframe, FlagReliable)
	}
	if sv.Constants.FormatRaw != int(FmtRaw) || sv.Constants.FormatJPEG != int(FmtJPEG) {
		t.Errorf("format ids = {raw:%d jpeg:%d}, want {%d %d}", sv.Constants.FormatRaw, sv.Constants.FormatJPEG, FmtRaw, FmtJPEG)
	}
}

func assertFramingVec(t *testing.T, label string, v framingVec) {
	t.Helper()
	frames, err := Packetize(mustHex(t, v.Payload), v.ImageID, v.TsUs, v.Src, v.Dst, v.FormatID, v.Flags)
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

// TestSstvAnchor pins the exampleImage: framing bytes, format_id, and ChannelID.
func TestSstvAnchor(t *testing.T) {
	sv := loadSstvVectors(t)
	a := sv.Anchors.ExampleImage
	frames, err := Packetize(mustHex(t, a.Payload), a.ImageID, a.TsUs, a.Src, a.Dst, a.FormatID, a.Flags)
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
	// Reassemble and confirm the recovered bytes + format equal the anchor.
	r := NewSstvReassembler()
	var got *SstvImage
	for _, f := range a.Frames {
		var arr [17]byte
		copy(arr[:], mustHex(t, f))
		if p := r.Push(&arr); p != nil {
			got = p
		}
	}
	if got == nil {
		t.Fatal("anchor did not reassemble")
	}
	if hex.EncodeToString(got.Data) != a.Payload {
		t.Errorf("anchor reassembled bytes = %s, want %s", hex.EncodeToString(got.Data), a.Payload)
	}
	if got.FormatID != a.FormatID || got.Dst != a.Dst {
		t.Errorf("anchor meta = {fmt:%d dst:%d}, want {fmt:%d dst:%d}", got.FormatID, got.Dst, a.FormatID, a.Dst)
	}
	if c := ChannelID("123456789"); c != 0x29B1 {
		t.Errorf("ChannelID(\"123456789\") = 0x%04X, want 0x29B1", c)
	}
	if c := ChannelID(""); c != Broadcast {
		t.Errorf("ChannelID(\"\") = 0x%04X, want broadcast 0x%04X", c, Broadcast)
	}
}

func TestSstvFraming(t *testing.T) {
	sv := loadSstvVectors(t)
	if len(sv.Framing) == 0 {
		t.Fatal("no framing vectors")
	}
	for i, v := range sv.Framing {
		assertFramingVec(t, "framing["+strconv.Itoa(i)+"]", v)
		// Round-trip through a reassembler.
		r := NewSstvReassembler()
		var got *SstvImage
		for _, f := range v.Frames {
			var arr [17]byte
			copy(arr[:], mustHex(t, f))
			if p := r.Push(&arr); p != nil {
				got = p
			}
		}
		if got == nil {
			t.Errorf("framing[%d]: image did not reassemble", i)
			continue
		}
		if hex.EncodeToString(got.Data) != v.Payload {
			t.Errorf("framing[%d]: recovered bytes = %s, want %s", i, hex.EncodeToString(got.Data), v.Payload)
		}
		if got.ImageID != v.ImageID || got.Src != v.Src || got.Dst != v.Dst || got.FormatID != v.FormatID || got.Flags != v.Flags || got.TsUs != v.TsUs {
			t.Errorf("framing[%d]: recovered meta = {iid:%d src:%d dst:%d fmt:%d flags:%d ts:%d}, want {iid:%d src:%d dst:%d fmt:%d flags:%d ts:%d}",
				i, got.ImageID, got.Src, got.Dst, got.FormatID, got.Flags, got.TsUs, v.ImageID, v.Src, v.Dst, v.FormatID, v.Flags, v.TsUs)
		}
	}
}

func TestSstvReassembly(t *testing.T) {
	sv := loadSstvVectors(t)
	if len(sv.Reassembly) == 0 {
		t.Fatal("no reassembly vectors")
	}
	for _, rc := range sv.Reassembly {
		r := NewSstvReassembler()
		var emitted []*SstvImage
		for _, f := range rc.InputFrames {
			var arr [17]byte
			copy(arr[:], mustHex(t, f))
			if p := r.Push(&arr); p != nil {
				emitted = append(emitted, p)
			}
		}
		if len(emitted) != len(rc.Images) {
			t.Fatalf("reassembly[%s]: emitted %d images, want %d", rc.Name, len(emitted), len(rc.Images))
		}
		for i, want := range rc.Images {
			got := emitted[i]
			if got.ImageID != want.ImageID || got.TsUs != want.TsUs || got.Src != want.Src || got.Dst != want.Dst || got.FormatID != want.FormatID || got.Flags != want.Flags {
				t.Errorf("reassembly[%s].img[%d]: meta = {iid:%d ts:%d src:%d dst:%d fmt:%d flags:%d}, want {iid:%d ts:%d src:%d dst:%d fmt:%d flags:%d}",
					rc.Name, i, got.ImageID, got.TsUs, got.Src, got.Dst, got.FormatID, got.Flags, want.ImageID, want.TsUs, want.Src, want.Dst, want.FormatID, want.Flags)
			}
			if hex.EncodeToString(got.Data) != want.Payload {
				t.Errorf("reassembly[%s].img[%d]: bytes = %s, want %s", rc.Name, i, hex.EncodeToString(got.Data), want.Payload)
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
