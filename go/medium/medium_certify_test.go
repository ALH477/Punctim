// SPDX-License-Identifier: LGPL-3.0-only

package medium

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"

	"github.com/ALH477/Punctim/go/dcf"
)

// mediumVectors mirrors Documentation/medium_vectors.json.
type mediumVectors struct {
	Version int `json:"version"`
	Anchors struct {
		Crc123456789     int    `json:"crc_123456789"`
		CrcZero15        int    `json:"crc_zero15"`
		FrameLen         int    `json:"frame_len"`
		ProtoHeaderLen   int    `json:"proto_header_len"`
		MsgFrame         int    `json:"msg_frame"`
		SuperLen         int    `json:"super_len"`
		L2Hdr            int    `json:"l2_hdr"`
		L2Filler         string `json:"l2_filler"`
		HydraSyncWord    int    `json:"hydra_sync_word"`
		AfskSync         int    `json:"afsk_sync"`
		AfskCrc8_1234567 int    `json:"afsk_crc8_123456789"`
	} `json:"anchors"`
	Basis []struct {
		Hex string `json:"hex"`
	} `json:"basis"`
	Families struct {
		Stream struct {
			Cases []struct {
				Name         string   `json:"name"`
				Input        string   `json:"input"`
				Frames       []string `json:"frames"`
				SkippedBytes int      `json:"skipped_bytes"`
				TailBytes    int      `json:"tail_bytes"`
			} `json:"cases"`
		} `json:"stream"`
		Hex struct {
			Cases []struct {
				Name        string   `json:"name"`
				Text        string   `json:"text"`
				Frames      []string `json:"frames"`
				DecodeInput string   `json:"decode_input"`
				Decoded     []string `json:"decoded"`
				BadLines    int      `json:"bad_lines"`
			} `json:"cases"`
		} `json:"hex"`
		UdpProto struct {
			HeaderLen int              `json:"header_len"`
			MsgFrame  int              `json:"msg_frame"`
			Types     map[string]uint8 `json:"types"`
			Cases     []struct {
				Name          string `json:"name"`
				Type          uint8  `json:"type"`
				Seq           uint32 `json:"seq"`
				TsHex         string `json:"ts_hex"`
				Payload       string `json:"payload"`
				Datagram      string `json:"datagram"`
				AcceptAsFrame bool   `json:"accept_as_frame"`
			} `json:"cases"`
		} `json:"udp_proto"`
		UdpBare struct {
			Cases []struct {
				Name      string   `json:"name"`
				Frames    []string `json:"frames"`
				Datagrams []string `json:"datagrams"`
			} `json:"cases"`
		} `json:"udp_bare"`
		L2Eth struct {
			Hdr    int    `json:"hdr"`
			Filler string `json:"filler"`
			Cases  []struct {
				Name    string   `json:"name"`
				Frames  []string `json:"frames"`
				Payload string   `json:"payload"`
			} `json:"cases"`
		} `json:"l2eth"`
		HydraSymbols struct {
			Profiles map[string]struct {
				SampleRate   float64 `json:"sample_rate"`
				Baud         float64 `json:"baud"`
				NTones       int     `json:"n_tones"`
				BaseFreq     float64 `json:"base_freq"`
				ToneSpacing  float64 `json:"tone_spacing"`
				PreambleSyms int     `json:"preamble_syms"`
				SyncWord     int     `json:"sync_word"`
				FecMode      int     `json:"fec_mode"`
				Interleave   int     `json:"interleave"`
			} `json:"profiles"`
			Cases []struct {
				Name             string `json:"name"`
				Profile          string `json:"profile"`
				Fec              string `json:"fec"`
				Interleave       int    `json:"interleave"`
				NTones           int    `json:"n_tones"`
				Frame            string `json:"frame"`
				CodedBits        int    `json:"coded_bits"`
				InterleaveStride int    `json:"interleave_stride"`
				TotalSyms        int    `json:"total_syms"`
				Symbols          string `json:"symbols"`
			} `json:"cases"`
		} `json:"hydra_symbols"`
		AfskBits struct {
			Profiles map[string]struct {
				Mark         float64 `json:"mark"`
				Space        float64 `json:"space"`
				Baud         int     `json:"baud"`
				PreambleBits int     `json:"preamble_bits"`
			} `json:"profiles"`
			Cases []struct {
				Name    string `json:"name"`
				Profile string `json:"profile"`
				Fec     bool   `json:"fec"`
				Frame   string `json:"frame"`
				NBits   int    `json:"n_bits"`
				Bits    string `json:"bits"`
			} `json:"cases"`
		} `json:"afsk_bits"`
	} `json:"families"`
}

func loadMedium(t *testing.T) mediumVectors {
	t.Helper()
	paths := []string{
		"../../Documentation/medium_vectors.json",
		"../../python/MCP/medium_vectors.json",
	}
	var lastErr error
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			lastErr = err
			continue
		}
		var mv mediumVectors
		if err := json.Unmarshal(data, &mv); err != nil {
			t.Fatalf("parse %s: %v", p, err)
		}
		return mv
	}
	t.Fatalf("medium_vectors.json not found (run gen_medium_vectors.py): %v", lastErr)
	return mediumVectors{}
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

func hexList(t *testing.T, ss []string) [][]byte {
	t.Helper()
	out := make([][]byte, len(ss))
	for i, s := range ss {
		out[i] = mustHex(t, s)
	}
	return out
}

func toHexList(bs [][]byte) []string {
	out := make([]string, len(bs))
	for i, b := range bs {
		out[i] = hex.EncodeToString(b)
	}
	return out
}

func eqStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestMediumAnchors(t *testing.T) {
	mv := loadMedium(t)
	a := mv.Anchors
	if int(dcf.Crc16CCITT([]byte("123456789"))) != a.Crc123456789 || a.Crc123456789 != 0x29B1 {
		t.Fatalf("crc anchor drift")
	}
	if int(dcf.Crc16CCITT(make([]byte, 15))) != a.CrcZero15 || a.CrcZero15 != 0x4EC3 {
		t.Fatalf("crc zero anchor drift")
	}
	if a.FrameLen != FrameLen || a.ProtoHeaderLen != ProtoHeaderLen || a.MsgFrame != int(MsgFrame) ||
		a.SuperLen != dcf.SuperSize || a.L2Hdr != L2Hdr || a.HydraSyncWord != HydraSyncWord ||
		a.AfskSync != AfskSyncWord {
		t.Fatalf("constant anchors drift: %+v", a)
	}
	if hex.EncodeToString(L2Filler) != a.L2Filler || !Gate(L2Filler) {
		t.Fatalf("l2 filler drift: %x", L2Filler)
	}
	if int(AfskCRC8([]byte("123456789"))) != a.AfskCrc8_1234567 {
		t.Fatalf("afsk crc8 anchor: 0x%02X != 0x%02X", AfskCRC8([]byte("123456789")), a.AfskCrc8_1234567)
	}
	for i, b := range mv.Basis {
		if !Gate(mustHex(t, b.Hex)) {
			t.Fatalf("basis[%d] fails the gate", i)
		}
	}
}

func TestStreamFamily(t *testing.T) {
	mv := loadMedium(t)
	cases := mv.Families.Stream.Cases
	if len(cases) == 0 {
		t.Fatal("no stream cases")
	}
	for _, c := range cases {
		in := mustHex(t, c.Input)
		frames, skipped, tail := StreamDecode(in)
		if !eqStrings(toHexList(frames), c.Frames) || skipped != c.SkippedBytes || tail != c.TailBytes {
			t.Fatalf("stream %s: got %v skipped=%d tail=%d; want %v skipped=%d tail=%d",
				c.Name, toHexList(frames), skipped, tail, c.Frames, c.SkippedBytes, c.TailBytes)
		}
		// chunk invariance of the incremental scanner
		for step := 1; step <= 40; step++ {
			var sc StreamScanner
			var got [][]byte
			for i := 0; i < len(in); i += step {
				end := i + step
				if end > len(in) {
					end = len(in)
				}
				got = append(got, sc.Feed(in[i:end])...)
			}
			if !eqStrings(toHexList(got), c.Frames) || sc.Skipped() != c.SkippedBytes ||
				len(sc.Flush()) != c.TailBytes {
				t.Fatalf("stream %s: scanner step %d diverges", c.Name, step)
			}
		}
		// encode(frames) re-decodes to the same frames (lossless)
		enc := StreamEncode(hexList(t, c.Frames))
		re, sk, tl := StreamDecode(enc)
		if !eqStrings(toHexList(re), c.Frames) || sk != 0 || tl != 0 {
			t.Fatalf("stream %s: encode/decode not lossless", c.Name)
		}
	}
	t.Logf("%d stream cases decode byte-identically (resync, chunk-invariant)", len(cases))
}

func TestHexFamily(t *testing.T) {
	mv := loadMedium(t)
	cases := mv.Families.Hex.Cases
	if len(cases) == 0 {
		t.Fatal("no hex cases")
	}
	for _, c := range cases {
		if got := HexEncode(hexList(t, c.Frames)); got != c.Text {
			t.Fatalf("hex %s encode: got %q want %q", c.Name, got, c.Text)
		}
		frames, bad := HexDecode(c.DecodeInput)
		if !eqStrings(toHexList(frames), c.Decoded) || bad != c.BadLines {
			t.Fatalf("hex %s decode: got %v bad=%d; want %v bad=%d",
				c.Name, toHexList(frames), bad, c.Decoded, c.BadLines)
		}
		rt, bad := HexDecode(c.Text)
		if !eqStrings(toHexList(rt), c.Frames) || bad != 0 {
			t.Fatalf("hex %s: decode(encode) != frames", c.Name)
		}
	}
	t.Logf("%d hex cases encode/decode byte-identically", len(cases))
}

func TestUdpProtoFamily(t *testing.T) {
	mv := loadMedium(t)
	f := mv.Families.UdpProto
	if f.HeaderLen != ProtoHeaderLen || f.MsgFrame != int(MsgFrame) {
		t.Fatalf("proto constants drift: header %d msg_frame %d", f.HeaderLen, f.MsgFrame)
	}
	for name, id := range f.Types {
		if MsgTypes[name] != id {
			t.Fatalf("type registry drift: %s = %d (vectors) vs %d", name, id, MsgTypes[name])
		}
	}
	if len(f.Types) != len(MsgTypes) {
		t.Fatalf("type registry size drift: %d vs %d", len(f.Types), len(MsgTypes))
	}
	if len(f.Cases) == 0 {
		t.Fatal("no udp_proto cases")
	}
	for _, c := range f.Cases {
		tsb := mustHex(t, c.TsHex)
		if len(tsb) != 8 {
			t.Fatalf("proto %s: ts_hex must be 8 bytes", c.Name)
		}
		var ts uint64
		for _, b := range tsb {
			ts = ts<<8 | uint64(b)
		}
		payload := mustHex(t, c.Payload)
		dg := ProtoEncode(c.Type, c.Seq, ts, payload)
		if got := hex.EncodeToString(dg); got != c.Datagram {
			t.Fatalf("proto %s encode:\n got %s\nwant %s", c.Name, got, c.Datagram)
		}
		mt, seq, ts2, pl, err := ProtoDecode(mustHex(t, c.Datagram))
		if err != nil || mt != c.Type || seq != c.Seq || ts2 != ts || !bytes.Equal(pl, payload) {
			t.Fatalf("proto %s decode mismatch (err=%v)", c.Name, err)
		}
		fr, ok := ProtoFrameDecode(mustHex(t, c.Datagram))
		if ok != c.AcceptAsFrame {
			t.Fatalf("proto %s: accept_as_frame got %v want %v", c.Name, ok, c.AcceptAsFrame)
		}
		if ok {
			if !bytes.Equal(fr, payload) {
				t.Fatalf("proto %s: carried frame mismatch", c.Name)
			}
			if got := hex.EncodeToString(ProtoFrameEncode(payload, c.Seq, ts)); got != c.Datagram {
				t.Fatalf("proto %s: ProtoFrameEncode mismatch", c.Name)
			}
		}
	}
	// the Go/Rust/C header guards
	g := ProtoEncode(1, 42, 0x0102030405060708, []byte{1, 2, 3})
	if _, _, _, _, err := ProtoDecode(g[:16]); err != ErrShortMessage {
		t.Fatalf("short-header guard missed: %v", err)
	}
	if _, _, _, _, err := ProtoDecode(g[:len(g)-1]); err != ErrPayloadOverrun {
		t.Fatalf("overrun guard missed: %v", err)
	}
	t.Logf("%d udp_proto cases encode/decode byte-identically", len(f.Cases))
}

func TestUdpBareFamily(t *testing.T) {
	mv := loadMedium(t)
	cases := mv.Families.UdpBare.Cases
	if len(cases) == 0 {
		t.Fatal("no udp_bare cases")
	}
	for _, c := range cases {
		frames := hexList(t, c.Frames)
		got := toHexList(BareEncode(frames, true))
		if !eqStrings(got, c.Datagrams) {
			t.Fatalf("bare %s encode: got %v want %v", c.Name, got, c.Datagrams)
		}
		var back [][]byte
		for _, d := range c.Datagrams {
			back = append(back, BareDecode(mustHex(t, d))...)
		}
		if !eqStrings(toHexList(back), c.Frames) {
			t.Fatalf("bare %s decode: got %v want %v", c.Name, toHexList(back), c.Frames)
		}
		raw := BareEncode(frames, false)
		if !eqStrings(toHexList(raw), c.Frames) {
			t.Fatalf("bare %s: pair=false must send every frame raw", c.Name)
		}
	}
	if BareDecode(make([]byte, 20)) != nil {
		t.Fatal("bare: a 20-byte datagram must decode to nothing")
	}
	t.Logf("%d udp_bare cases encode/decode byte-identically", len(cases))
}

func TestL2EthFamily(t *testing.T) {
	mv := loadMedium(t)
	f := mv.Families.L2Eth
	if f.Hdr != L2Hdr || f.Filler != hex.EncodeToString(L2Filler) {
		t.Fatalf("l2eth constants drift")
	}
	if len(f.Cases) == 0 {
		t.Fatal("no l2eth cases")
	}
	for _, c := range f.Cases {
		frames := hexList(t, c.Frames)
		pl, err := L2Batch(frames)
		if err != nil {
			t.Fatalf("l2eth %s batch: %v", c.Name, err)
		}
		if got := hex.EncodeToString(pl); got != c.Payload {
			t.Fatalf("l2eth %s batch:\n got %s\nwant %s", c.Name, got, c.Payload)
		}
		back, err := L2Unbatch(mustHex(t, c.Payload))
		if err != nil || !eqStrings(toHexList(back), c.Frames) {
			t.Fatalf("l2eth %s unbatch: %v %v", c.Name, toHexList(back), err)
		}
		// Ethernet padding after the last SuperPack is ignored; truncation is rejected.
		padded := append(mustHex(t, c.Payload), make([]byte, 7)...)
		if back, err := L2Unbatch(padded); err != nil || !eqStrings(toHexList(back), c.Frames) {
			t.Fatalf("l2eth %s: trailing padding not ignored", c.Name)
		}
		if _, err := L2Unbatch(mustHex(t, c.Payload)[:len(c.Payload)/2-1]); err == nil {
			t.Fatalf("l2eth %s: truncated batch accepted", c.Name)
		}
	}
	if L2Capacity(1500) != 92 || L2Capacity(1) != 0 {
		t.Fatalf("l2 capacity drift: %d", L2Capacity(1500))
	}
	t.Logf("%d l2eth cases batch/unbatch byte-identically", len(f.Cases))
}

func TestHydraSymbolsFamily(t *testing.T) {
	mv := loadMedium(t)
	f := mv.Families.HydraSymbols
	for name, vp := range f.Profiles {
		p, err := HydraProfileByName(name)
		if err != nil {
			t.Fatalf("profile %s: %v", name, err)
		}
		if p.SampleRate != vp.SampleRate || p.Baud != vp.Baud || p.NTones != vp.NTones ||
			p.BaseFreq != vp.BaseFreq || p.ToneSpacing != vp.ToneSpacing ||
			p.PreambleSyms != vp.PreambleSyms || int(p.SyncWord) != vp.SyncWord ||
			p.FecMode != vp.FecMode || p.Interleave != (vp.Interleave != 0) {
			t.Fatalf("profile %s drift: %+v vs %+v", name, p, vp)
		}
	}
	if len(f.Cases) == 0 {
		t.Fatal("no hydra_symbols cases")
	}
	for _, c := range f.Cases {
		p, err := HydraProfileByName(c.Profile)
		if err != nil {
			t.Fatalf("hydra %s: %v", c.Name, err)
		}
		fm, ok := FecNames[c.Fec]
		if !ok {
			t.Fatalf("hydra %s: unknown fec %q", c.Name, c.Fec)
		}
		p.FecMode, p.Interleave, p.NTones = fm, c.Interleave != 0, c.NTones
		if err := p.Init(); err != nil {
			t.Fatalf("hydra %s init: %v", c.Name, err)
		}
		if p.CodedBits != c.CodedBits || p.InterleaveStride != c.InterleaveStride ||
			p.TotalSyms != c.TotalSyms {
			t.Fatalf("hydra %s derived: coded %d stride %d total %d; want %d %d %d", c.Name,
				p.CodedBits, p.InterleaveStride, p.TotalSyms, c.CodedBits, c.InterleaveStride,
				c.TotalSyms)
		}
		frame := mustHex(t, c.Frame)
		syms, err := HydraSymbolsEncode(&p, frame)
		if err != nil {
			t.Fatalf("hydra %s encode: %v", c.Name, err)
		}
		if syms != c.Symbols {
			t.Fatalf("hydra %s symbols:\n got %s\nwant %s", c.Name, syms, c.Symbols)
		}
		back, ok := HydraSymbolsDecode(&p, c.Symbols)
		if !ok || !bytes.Equal(back, frame) {
			t.Fatalf("hydra %s: decode(symbols) != frame", c.Name)
		}
	}
	t.Logf("%d hydra_symbols cases encode byte-identically; decode(encode) = id", len(f.Cases))
}

func TestHydraDerivedAndCorrection(t *testing.T) {
	want := map[string][3]int{"default": {192, 496, 356}, "aux": {184, 488, 348}}
	for name, totals := range want {
		for k, fec := range []int{FecNone, FecRep3, FecConv} {
			p, _ := HydraProfileByName(name)
			p.FecMode = fec
			if err := p.Init(); err != nil {
				t.Fatal(err)
			}
			if p.CodedBits != [3]int{152, 456, 316}[k] || p.InterleaveStride != [3]int{13, 23, 19}[k] ||
				p.TotalSyms != totals[k] {
				t.Fatalf("%s/%s derived drift: %+v", name, FecName(fec), p)
			}
		}
	}
	// the hard Viterbi inverts the encoder and corrects isolated coded-bit errors
	msg := hydraBytesToBits(mustHex(t, "d31312340001ffffdeadbeefab12cd24c0"))
	coded := HydraConvEncode(msg)
	if !bytes.Equal(HydraConvDecodeHard(coded), msg) {
		t.Fatal("viterbi(encode) != id")
	}
	bad := append([]byte(nil), coded...)
	for _, i := range []int{3, 90, 200} {
		bad[i] ^= 1
	}
	if !bytes.Equal(HydraConvDecodeHard(bad), msg) {
		t.Fatal("viterbi failed to correct 3 spread coded-bit errors")
	}
	// a corrupted sync or symbol count is rejected
	p := HydraProfileDefault()
	_ = p.Init()
	s, _ := HydraSymbolsEncode(&p, mustHex(t, "d31312340001ffffdeadbeefab12cd24c0"))
	if _, ok := HydraSymbolsDecode(&p, s[:len(s)-1]); ok {
		t.Fatal("short symbol string accepted")
	}
	b := []byte(s)
	b[p.PreambleSyms] ^= 1 // flip the first sync symbol
	if _, ok := HydraSymbolsDecode(&p, string(b)); ok {
		t.Fatal("bad sync accepted")
	}
}

func TestAfskBitsFamily(t *testing.T) {
	mv := loadMedium(t)
	f := mv.Families.AfskBits
	for name, vp := range f.Profiles {
		p, ok := AfskProfiles[name]
		if !ok || p.Mark != vp.Mark || p.Space != vp.Space || p.Baud != vp.Baud ||
			p.PreambleBits != vp.PreambleBits {
			t.Fatalf("afsk profile %s drift: %+v vs %+v", name, p, vp)
		}
	}
	if len(f.Cases) == 0 {
		t.Fatal("no afsk_bits cases")
	}
	for _, c := range f.Cases {
		frame := mustHex(t, c.Frame)
		bits, err := AfskBitsEncode(frame, c.Profile, c.Fec)
		if err != nil {
			t.Fatalf("afsk %s encode: %v", c.Name, err)
		}
		if len(bits) != c.NBits || bits != c.Bits {
			t.Fatalf("afsk %s bits:\n got %s\nwant %s", c.Name, bits, c.Bits)
		}
		back, ok := AfskBitsDecode(c.Bits, c.Profile, c.Fec)
		if !ok || !bytes.Equal(back, frame) {
			t.Fatalf("afsk %s: decode(bits) != frame", c.Name)
		}
	}
	t.Logf("%d afsk_bits cases encode byte-identically; decode(encode) = id", len(f.Cases))
}
