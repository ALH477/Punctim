// SPDX-License-Identifier: LGPL-3.0-only

// Command punctim is the DCF medium tool (Go implementation; stdlib only). It reads
// DeModFrames from any medium and emits them on any other, byte-deterministically;
// encodes/decodes single frames; and certifies the medium codecs (go/medium) against
// Documentation/medium_vectors.json. The same CLI exists in Python (the reference,
// python/punctim.py), C, Rust and Node — Documentation/DCF_MEDIUM_SPEC.md.
//
//	punctim version [--json]
//	punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
//	                [--no-validate] [--stats] [--queue N]
//	punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
//	punctim decode  (HEX | --stdin) [--json]
//	punctim certify [--vectors DIR] [--family NAME ...]
//
// Exit codes: 0 ok · 1 I/O error · 2 usage · 3 medium unsupported in this build ·
// 4 certification failed · 5 invalid frame · 6 --expect not met.
//
// Media in this build: file: stdio: hex: udp: (dialect=proto|bare) hydra: (via the
// frame_tx/frame_rx tools). l2eth: loop: afsk:/audio: sdr: janus: are recognised (URI
// keys validated) but exit 3 (medium unsupported) — they live in the Python build.
//
// Determinism rule (normative): for finite inputs (file/hex without follow, stdio),
// `punctim io` produces byte-identical output to every other language's punctim for
// identical input and URI; udp:dialect=proto needs ts=0 (the default).
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/ALH477/Punctim/go/dcf"
	"github.com/ALH477/Punctim/go/medium"
)

const (
	version = "0.3.0"
	impl    = "go"
)

// Exit codes (Documentation/DCF_MEDIUM_SPEC.md).
const (
	exitOK          = 0
	exitIO          = 1
	exitUsage       = 2
	exitUnsupported = 3
	exitCert        = 4
	exitInvalid     = 5
	exitExpect      = 6
)

// The media this build can open (version --json).
var supportedMedia = []string{"file", "stdio", "hex", "udp", "hydra"}

// The certified medium families, in certification order.
var families = []string{"stream", "hex", "udp_proto", "udp_bare", "l2eth", "hydra_symbols", "afsk_bits"}

// cliError carries an exit code (usage / unsupported); any other error is an I/O error.
type cliError struct {
	code int
	msg  string
}

func (e *cliError) Error() string { return e.msg }

func usagef(format string, a ...any) error {
	return &cliError{exitUsage, fmt.Sprintf(format, a...)}
}

func unsupportedf(format string, a ...any) error {
	return &cliError{exitUnsupported, fmt.Sprintf(format, a...)}
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	// A broken stdout pipe returns EPIPE from write (exit 1) instead of killing the process.
	signal.Notify(make(chan os.Signal, 1), syscall.SIGPIPE)
	if len(args) == 0 {
		usage(os.Stderr)
		fmt.Fprintln(os.Stderr, "punctim: error: the following arguments are required: command")
		return exitUsage
	}
	cmd, rest := args[0], args[1:]
	var err error
	code := exitOK
	switch cmd {
	case "version":
		code, err = cmdVersion(rest)
	case "io":
		code, err = cmdIO(rest)
	case "encode":
		code, err = cmdEncode(rest)
	case "decode":
		code, err = cmdDecode(rest)
	case "certify":
		code, err = cmdCertify(rest)
	case "sim":
		fmt.Fprintln(os.Stderr, "punctim sim: medium unsupported: the simulator ships in the Python build "+
			"(python3 python/punctim.py sim)")
		return exitUnsupported
	case "-h", "--help", "help":
		usage(os.Stdout)
		return exitOK
	default:
		usage(os.Stderr)
		fmt.Fprintf(os.Stderr, "punctim: error: invalid command %q\n", cmd)
		return exitUsage
	}
	if err != nil {
		var ce *cliError
		switch {
		case errors.As(err, &ce) && ce.code == exitUnsupported:
			fmt.Fprintf(os.Stderr, "punctim %s: medium unsupported: %s\n", cmd, ce.msg)
			return exitUnsupported
		case errors.As(err, &ce):
			fmt.Fprintf(os.Stderr, "punctim %s: %s\n", cmd, ce.msg)
			return ce.code
		default:
			fmt.Fprintf(os.Stderr, "punctim %s: I/O error: %v\n", cmd, err)
			return exitIO
		}
	}
	return code
}

func usage(w io.Writer) {
	fmt.Fprint(w, `usage: punctim {version,io,encode,decode,certify,sim} ...

DCF medium tool (Go): move DeModFrames between any two media, deterministically
(Documentation/DCF_MEDIUM_SPEC.md).

  punctim version [--json]
  punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
                  [--no-validate] [--stats] [--queue N]
  punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
  punctim decode  (HEX | --stdin) [--json]
  punctim certify [--vectors DIR] [--family NAME ...]

media: file:path=,append=,follow=,mode=  stdio:  hex:[path=]
       udp:dialect=proto|bare,bind=,peer=a|b,pair=1,flush_ms=20,ts=0|now,seq_start=1
       hydra:in=DIR,out=DIR,profile=default|aux,fec=none|rep3|conv,interleave=0|1,
             base_freq=,tone_spacing=,baud=,n_tones=,tx=,rx=
       (l2eth: loop: afsk: audio: sdr: janus: -> exit 3 in this build)
exit: 0 ok, 1 I/O, 2 usage, 3 medium unsupported, 4 cert failed, 5 invalid frame,
      6 --expect not met
`)
}

// ── argv ─────────────────────────────────────────────────────────────────────

type parsedArgs struct {
	vals  map[string]string
	multi map[string][]string
	flags map[string]bool
	pos   []string
}

// parseArgs is a small argparse-alike: --name VALUE / --name=VALUE for valued flags,
// --name for boolean flags, --name V1 V2 ... for multi-value flags; everything else is
// positional. A later duplicate wins. Unknown --flags are a usage error.
func parseArgs(args []string, valued, boolean, multi []string) (*parsedArgs, error) {
	in := func(set []string, s string) bool {
		for _, x := range set {
			if x == s {
				return true
			}
		}
		return false
	}
	p := &parsedArgs{vals: map[string]string{}, multi: map[string][]string{}, flags: map[string]bool{}}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			p.pos = append(p.pos, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(a, "--") || a == "-" {
			if strings.HasPrefix(a, "-") && len(a) > 1 && !isNumberLike(a) {
				return nil, usagef("unrecognized arguments: %s", a)
			}
			p.pos = append(p.pos, a)
			continue
		}
		name, val, hasVal := strings.Cut(a[2:], "=")
		switch {
		case in(valued, name):
			if !hasVal {
				if i+1 >= len(args) {
					return nil, usagef("argument --%s: expected one argument", name)
				}
				i++
				val = args[i]
			}
			p.vals[name] = val
		case in(boolean, name):
			if hasVal {
				return nil, usagef("argument --%s: ignored explicit argument %q", name, val)
			}
			p.flags[name] = true
		case in(multi, name):
			var vs []string
			if hasVal {
				vs = append(vs, val)
			}
			for i+1 < len(args) && !strings.HasPrefix(args[i+1], "--") {
				i++
				vs = append(vs, args[i])
			}
			if len(vs) == 0 {
				return nil, usagef("argument --%s: expected at least one argument", name)
			}
			p.multi[name] = append(p.multi[name], vs...)
		default:
			return nil, usagef("unrecognized arguments: %s", a)
		}
	}
	return p, nil
}

func isNumberLike(s string) bool {
	_, err := strconv.ParseFloat(s, 64)
	return err == nil
}

// pyInt0 mirrors Python int(s, 0): decimal (no leading zeros), 0x/0o/0b prefixes.
func pyInt0(s string) (int64, error) {
	t := strings.TrimSpace(s)
	u := strings.TrimLeft(t, "+-")
	if len(u) > 1 && u[0] == '0' && u[1] >= '0' && u[1] <= '9' {
		return 0, fmt.Errorf("invalid literal %q", s)
	}
	return strconv.ParseInt(t, 0, 64)
}

// ── version ──────────────────────────────────────────────────────────────────

func cmdVersion(args []string) (int, error) {
	a, err := parseArgs(args, nil, []string{"json"}, nil)
	if err != nil {
		return 0, err
	}
	if len(a.pos) > 0 {
		return 0, usagef("unrecognized arguments: %s", strings.Join(a.pos, " "))
	}
	if a.flags["json"] {
		fmt.Println(pyJSON([]kv{{"name", "punctim"}, {"version", version}, {"impl", impl},
			{"media", supportedMedia}, {"families", families}}))
	} else {
		fmt.Printf("punctim %s (%s)\n", version, impl)
	}
	return exitOK, nil
}

// ── encode ───────────────────────────────────────────────────────────────────

// numArg mirrors the Python CLI's _num: decimal or 0x hex, range-checked.
func numArg(s, what string, hi *big.Int) (*big.Int, error) {
	t := strings.TrimSpace(s)
	v := new(big.Int)
	ok := false
	if strings.HasPrefix(strings.ToLower(t), "0x") {
		_, ok = v.SetString(t[2:], 16)
	} else {
		_, ok = v.SetString(t, 10)
	}
	if !ok {
		return nil, usagef("%s: '%s' is not an integer (decimal or 0x hex)", what, s)
	}
	if v.Sign() < 0 || v.Cmp(hi) > 0 {
		return nil, usagef("%s: %s out of range 0..%s", what, v.String(), hi.String())
	}
	return v, nil
}

func cmdEncode(args []string) (int, error) {
	a, err := parseArgs(args, []string{"type", "seq", "src", "dst", "payload", "text", "ts"}, nil, nil)
	if err != nil {
		return 0, err
	}
	if len(a.pos) > 0 {
		return 0, usagef("unrecognized arguments: %s", strings.Join(a.pos, " "))
	}
	var missing []string
	for _, k := range []string{"type", "seq", "src", "dst"} {
		if _, ok := a.vals[k]; !ok {
			missing = append(missing, "--"+k)
		}
	}
	_, hasP := a.vals["payload"]
	_, hasT := a.vals["text"]
	if hasP && hasT {
		return 0, usagef("argument --text: not allowed with argument --payload")
	}
	if !hasP && !hasT {
		missing = append(missing, "one of the arguments --payload --text")
	}
	if len(missing) > 0 {
		return 0, usagef("the following arguments are required: %s", strings.Join(missing, ", "))
	}
	u16 := big.NewInt(0xFFFF)
	t, err := numArg(a.vals["type"], "--type", big.NewInt(15))
	if err != nil {
		return 0, err
	}
	seq, err := numArg(a.vals["seq"], "--seq", u16)
	if err != nil {
		return 0, err
	}
	src, err := numArg(a.vals["src"], "--src", u16)
	if err != nil {
		return 0, err
	}
	dst, err := numArg(a.vals["dst"], "--dst", u16)
	if err != nil {
		return 0, err
	}
	tsStr, ok := a.vals["ts"]
	if !ok {
		tsStr = "0"
	}
	ts, err := numArg(tsStr, "--ts", new(big.Int).SetUint64(math.MaxUint64))
	if err != nil {
		return 0, err
	}
	var payload [4]byte
	if hasP {
		p := strings.TrimSpace(a.vals["payload"])
		b, herr := hex.DecodeString(p)
		if len(p) != 8 || herr != nil {
			return 0, usagef("--payload wants exactly 8 hex digits (4 bytes)")
		}
		copy(payload[:], b)
	} else {
		b := []byte(a.vals["text"])
		if len(b) > 4 {
			return 0, usagef("--text is at most 4 UTF-8 bytes (zero-padded)")
		}
		copy(payload[:], b)
	}
	f := dcf.Frame{Version: dcf.Version, Type: dcf.FrameType(t.Uint64()), Seq: uint16(seq.Uint64()),
		Src: uint16(src.Uint64()), Dst: uint16(dst.Uint64()), Payload: payload,
		TsUs: uint32(ts.Uint64() & 0xFFFFFF)} // ts_us is 24-bit on the wire
	w := f.Encode()
	if _, err := os.Stdout.WriteString(hex.EncodeToString(w[:]) + "\n"); err != nil {
		return 0, err
	}
	return exitOK, nil
}

// ── decode ───────────────────────────────────────────────────────────────────

var frameTypeNames = map[byte]string{0: "FData", 1: "FAck", 2: "FBeacon", 3: "FCtrl"}

// decodeRecord is the decode record for one hex string: the wirelab_core.decode() fields
// plus hex/valid/syndrome (+ error when invalid), in the Python CLI's key order. Fields
// are read raw from a 17-byte word even when the gate fails.
func decodeRecord(text string) ([]kv, bool) {
	s := strings.Trim(text, " \t\r\v\f")
	if len(s)%2 != 0 || !isHexString(s) {
		return []kv{{"hex", s}, {"valid", false}, {"error", "not hex"}}, false
	}
	b, _ := hex.DecodeString(s)
	if len(b) != medium.FrameLen {
		return []kv{{"hex", hex.EncodeToString(b)}, {"valid", false},
			{"error", fmt.Sprintf("length %d != 17", len(b))}}, false
	}
	syn, _ := dcf.Syndrome(b)
	t := b[1] & 0x0F
	name, ok := frameTypeNames[t]
	if !ok {
		name = fmt.Sprintf("0x%X", t)
	}
	errMsg := ""
	switch {
	case b[0] != dcf.SyncByte:
		errMsg = "bad sync byte"
	case b[1]>>4 != dcf.Version:
		errMsg = "bad version nibble"
	case syn != 0:
		errMsg = "CRC mismatch"
	}
	rec := []kv{{"hex", hex.EncodeToString(b)}, {"valid", errMsg == ""}, {"syndrome", int(syn)},
		{"frame_type", int(t)}, {"frame_type_name", name},
		{"seq", int(b[2])<<8 | int(b[3])}, {"src", int(b[4])<<8 | int(b[5])},
		{"dst", int(b[6])<<8 | int(b[7])}, {"payload", hex.EncodeToString(b[8:12])},
		{"ts_us", int(b[12])<<16 | int(b[13])<<8 | int(b[14])}, {"crc", int(b[15])<<8 | int(b[16])}}
	if errMsg != "" {
		rec = append(rec, kv{"error", errMsg})
	}
	return rec, errMsg == ""
}

func isHexString(s string) bool {
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return true
}

func recGet(rec []kv, k string) any {
	for _, e := range rec {
		if e.k == k {
			return e.v
		}
	}
	return nil
}

func humanRecord(rec []kv) string {
	if recGet(rec, "syndrome") == nil {
		return fmt.Sprintf("invalid (%s) %s", recGet(rec, "error"), recGet(rec, "hex"))
	}
	head := "valid"
	if v, _ := recGet(rec, "valid").(bool); !v {
		head = fmt.Sprintf("invalid (%s)", recGet(rec, "error"))
	}
	return fmt.Sprintf("%s type=%d (%s) seq=%d src=%d dst=%d payload=%s ts_us=%d crc=0x%04x syndrome=0x%04x",
		head, recGet(rec, "frame_type"), recGet(rec, "frame_type_name"), recGet(rec, "seq"),
		recGet(rec, "src"), recGet(rec, "dst"), recGet(rec, "payload"), recGet(rec, "ts_us"),
		recGet(rec, "crc"), recGet(rec, "syndrome"))
}

func cmdDecode(args []string) (int, error) {
	a, err := parseArgs(args, nil, []string{"stdin", "json"}, nil)
	if err != nil {
		return 0, err
	}
	if len(a.pos) > 1 {
		return 0, usagef("unrecognized arguments: %s", strings.Join(a.pos[1:], " "))
	}
	if a.flags["stdin"] == (len(a.pos) == 1) {
		return 0, usagef("decode wants exactly one of HEX or --stdin")
	}
	var items []string
	if a.flags["stdin"] {
		data, rerr := io.ReadAll(os.Stdin)
		if rerr != nil {
			return 0, rerr
		}
		for _, line := range strings.Split(string(data), "\n") {
			s := strings.Trim(line, " \t\r\v\f\n")
			if s != "" && s[0] != '#' {
				items = append(items, s)
			}
		}
	} else {
		items = []string{a.pos[0]}
	}
	out := bufio.NewWriter(os.Stdout)
	code := exitOK
	for _, it := range items {
		rec, valid := decodeRecord(it)
		if !valid {
			code = exitInvalid
		}
		if a.flags["json"] {
			fmt.Fprintln(out, pyJSON(rec))
		} else {
			fmt.Fprintln(out, humanRecord(rec))
		}
	}
	if err := out.Flush(); err != nil {
		return 0, err
	}
	return code, nil
}

// ── JSON (byte-identical to Python json.dumps(..., separators=(",", ":"))) ───────

type kv struct {
	k string
	v any
}

// pyStr renders s like Python's json.dumps with ensure_ascii=True: backslash, quote and
// \b \f \n \r \t escaped short; every other byte outside 0x20..0x7E as a lowercase \uXXXX
// (surrogate pairs above the BMP; invalid UTF-8 bytes as surrogateescape \udcXX).
func pyStr(s string) string {
	var sb strings.Builder
	sb.WriteByte('"')
	for i := 0; i < len(s); {
		c := s[i]
		if c < utf8.RuneSelf {
			switch c {
			case '\\':
				sb.WriteString(`\\`)
			case '"':
				sb.WriteString(`\"`)
			case '\b':
				sb.WriteString(`\b`)
			case '\f':
				sb.WriteString(`\f`)
			case '\n':
				sb.WriteString(`\n`)
			case '\r':
				sb.WriteString(`\r`)
			case '\t':
				sb.WriteString(`\t`)
			default:
				if c < 0x20 || c == 0x7F {
					fmt.Fprintf(&sb, `\u%04x`, c)
				} else {
					sb.WriteByte(c)
				}
			}
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			fmt.Fprintf(&sb, `\u%04x`, 0xDC00+int(c))
		} else if r > 0xFFFF {
			r -= 0x10000
			fmt.Fprintf(&sb, `\u%04x\u%04x`, 0xD800+(r>>10), 0xDC00+(r&0x3FF))
		} else {
			fmt.Fprintf(&sb, `\u%04x`, r)
		}
		i += size
	}
	sb.WriteByte('"')
	return sb.String()
}

// pyFloat renders a float like Python's repr of a small non-negative value (always with a
// decimal point: 0.0, 1.5, 4.004).
func pyFloat(f float64) string {
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.ContainsAny(s, ".eEnN") {
		s += ".0"
	}
	return s
}

func pyJSON(items []kv) string {
	var sb strings.Builder
	sb.WriteByte('{')
	for i, e := range items {
		if i > 0 {
			sb.WriteByte(',')
		}
		sb.WriteString(pyStr(e.k))
		sb.WriteByte(':')
		switch v := e.v.(type) {
		case string:
			sb.WriteString(pyStr(v))
		case bool:
			if v {
				sb.WriteString("true")
			} else {
				sb.WriteString("false")
			}
		case int:
			sb.WriteString(strconv.Itoa(v))
		case int64:
			sb.WriteString(strconv.FormatInt(v, 10))
		case float64:
			sb.WriteString(pyFloat(v))
		case []string:
			sb.WriteByte('[')
			for j, s := range v {
				if j > 0 {
					sb.WriteByte(',')
				}
				sb.WriteString(pyStr(s))
			}
			sb.WriteByte(']')
		case nil:
			sb.WriteString("null")
		default:
			sb.WriteString(pyStr(fmt.Sprint(v)))
		}
	}
	sb.WriteByte('}')
	return sb.String()
}

// ── the medium URI grammar (mirrors python/dcf/medium.py:parse_uri) ─────────────

var schemeOrder = []string{"file", "stdio", "hex", "udp", "l2eth", "loop", "hydra", "afsk",
	"audio", "sdr", "janus", "mc"}

var schemeKeys = map[string][]string{
	"file":  {"path", "mode", "append", "follow", "in", "out"},
	"stdio": {},
	"hex":   {"path", "mode", "append", "follow"},
	"udp":   {"dialect", "bind", "peer", "pair", "flush_ms", "ts", "seq_start"},
	"l2eth": {"if", "ethertype", "dst", "mtu", "impl", "id", "flush_ms"},
	"loop":  {"id"},
	"hydra": {"in", "out", "profile", "fec", "interleave", "base_freq", "tone_spacing", "baud",
		"n_tones", "impl", "tx", "rx"},
	"afsk":  {"in", "out", "profile", "fec"},
	"audio": {"in", "out", "profile", "fec"},
	"sdr":   {"in", "out", "mod"},
	"janus": {"in", "out", "pset", "fs", "pset_file", "tx", "rx"},
	"mc":    {"rcon", "pass_file", "pass_env", "fifo", "log", "bot", "egress", "ns", "poll_hz"},
}

type uri struct {
	scheme string
	kw     map[string]string
}

func (u uri) get(k, def string) string {
	if v, ok := u.kw[k]; ok {
		return v
	}
	return def
}

func (u uri) has(k string) bool { _, ok := u.kw[k]; return ok }

// parseURI: SCHEME[:k=v,...] -> (scheme, {k: v}). The scheme is case-insensitive; keys are
// validated against the scheme (plus name); values are kept verbatim (a later duplicate
// wins; "|" inside a value separates multiple values).
func parseURI(spec string) (uri, error) {
	if strings.TrimSpace(spec) == "" {
		return uri{}, usagef("empty medium URI")
	}
	scheme, rest, _ := strings.Cut(spec, ":")
	scheme = strings.ToLower(strings.TrimSpace(scheme))
	keys, ok := schemeKeys[scheme]
	if !ok {
		return uri{}, usagef("unknown medium '%s' (one of: %s)", scheme, strings.Join(schemeOrder, ", "))
	}
	allowed := map[string]bool{"name": true}
	for _, k := range keys {
		allowed[k] = true
	}
	u := uri{scheme: scheme, kw: map[string]string{}}
	for _, item := range strings.Split(rest, ",") {
		if item == "" {
			continue
		}
		k, v, ok := strings.Cut(item, "=")
		if !ok {
			return uri{}, usagef("%s: bad item '%s' (want key=value)", scheme, item)
		}
		k = strings.TrimSpace(k)
		if !allowed[k] {
			return uri{}, usagef("%s: unknown key '%s'", scheme, k)
		}
		u.kw[k] = v
	}
	return u, nil
}

func multiValues(v string) []string {
	var out []string
	for _, p := range strings.Split(v, "|") {
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func uriBool(v, key string) (bool, error) {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off", "":
		return false, nil
	}
	return false, usagef("%s='%s': want 0|1", key, v)
}

func uriInt(v, key string) (int64, error) {
	s := strings.TrimSpace(v)
	var n int64
	var err error
	if strings.HasPrefix(strings.ToLower(s), "0x") {
		n, err = strconv.ParseInt(s[2:], 16, 64)
	} else {
		n, err = strconv.ParseInt(s, 10, 64)
	}
	if err != nil {
		return 0, usagef("%s='%s': want an integer (decimal or 0x hex)", key, v)
	}
	return n, nil
}

func uriNum(v, key string) (float64, error) {
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil {
		return 0, usagef("%s='%s': want a number", key, v)
	}
	return f, nil
}

func uriChoice(v, key string, choices ...string) (string, error) {
	for _, c := range choices {
		if v == c {
			return v, nil
		}
	}
	return "", usagef("%s='%s': want %s", key, v, strings.Join(choices, "|"))
}

func uriHostPort(s, key string) (string, int, error) {
	i := strings.LastIndexByte(s, ':')
	if i <= 0 {
		return "", 0, usagef("%s='%s': want host:port", key, s)
	}
	port, err := uriInt(s[i+1:], key)
	if err != nil {
		return "", 0, err
	}
	if port < 0 || port > 65535 {
		return "", 0, usagef("%s='%s': port out of range", key, s)
	}
	return s[:i], int(port), nil
}

func resolveUDP4(host string, port int) (*net.UDPAddr, error) {
	return net.ResolveUDPAddr("udp4", net.JoinHostPort(host, strconv.Itoa(port)))
}

// finiteInput reports whether an --in URI is finite (read to EOF, never drops): stdio, and
// hex/file without follow. Everything else runs until --count / --seconds / SIGINT.
func finiteInput(u uri) (bool, error) {
	switch u.scheme {
	case "stdio":
		return true, nil
	case "hex", "file":
		f, err := uriBool(u.get("follow", "0"), "follow")
		return !f, err
	}
	return false, nil
}

// unsupportedMedium validates what Python validates for a medium this build cannot open,
// then reports it as unsupported (exit 3).
func unsupportedMedium(u uri, dir string) error {
	switch u.scheme {
	case "afsk", "audio":
		if _, err := uriChoice(u.get("profile", "handheld"), "profile", "standard", "handheld", "aux-cable"); err != nil {
			return err
		}
		if _, err := uriBool(u.get("fec", "0"), "fec"); err != nil {
			return err
		}
		return unsupportedf("%s: the AFSK acoustic modem needs the Python build (numpy)", u.scheme)
	case "l2eth":
		def := "loop"
		if u.get("if", "") != "" {
			def = "raw"
		}
		im, err := uriChoice(u.get("impl", def), "impl", "raw", "loop")
		if err != nil {
			return err
		}
		if im == "raw" && u.get("if", "") == "" {
			return usagef("l2eth:impl=raw needs if=<interface>")
		}
		return unsupportedf("l2eth: raw-L2 Ethernet is not in the Go build (use the Python or C tools)")
	case "loop":
		return unsupportedf("loop: an in-process medium has no peer inside a single punctim io")
	case "sdr", "janus":
		return unsupportedf("%s: needs the Python build", u.scheme)
	case "mc":
		return unsupportedf("mc: a Minecraft world's register is Python-only (punctim mc)")
	}
	return unsupportedf("%s: not supported as %s in this build", u.scheme, dir)
}

// ── HydraModem tool plumbing (mirrors python/dcf/transport.py HydraTransport) ─────

var hydraAuxFlags = []string{"--base-freq", "1200", "--tone-spacing", "1200", "--baud", "1200"}

var (
	hydraCapsMu    sync.Mutex
	hydraCapsCache = map[string]map[string]bool{}
)

// hydraToolCaps: the optional flags a frame_tx/frame_rx build understands (from its usage
// text) — a subset of {--profile, --interleave, --preamble}; cached per binary path.
func hydraToolCaps(tool string) map[string]bool {
	hydraCapsMu.Lock()
	defer hydraCapsMu.Unlock()
	if c, ok := hydraCapsCache[tool]; ok {
		return c
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var buf bytes.Buffer
	cmd := exec.CommandContext(ctx, tool)
	cmd.Stdout, cmd.Stderr = &buf, &buf
	_ = cmd.Run()
	caps := map[string]bool{}
	for _, f := range []string{"--profile", "--interleave", "--preamble"} {
		if strings.Contains(buf.String(), f) {
			caps[f] = true
		}
	}
	hydraCapsCache[tool] = caps
	return caps
}

type hydraConf struct {
	name, in, out, tx, rx string
	fecFlag               string
	prof                  []string
}

func fmtNum(v float64) string {
	if v == math.Trunc(v) && math.Abs(v) < 1e18 {
		return strconv.FormatInt(int64(v), 10)
	}
	return strconv.FormatFloat(v, 'f', -1, 64)
}

func hydraConfig(u uri) (*hydraConf, error) {
	c := &hydraConf{name: u.get("name", u.scheme), in: u.get("in", ""), out: u.get("out", "")}
	fec, err := uriChoice(u.get("fec", "conv"), "fec", "none", "rep3", "conv")
	if err != nil {
		return nil, err
	}
	profile, err := uriChoice(u.get("profile", "default"), "profile", "default", "aux",
		"melody", "chime", "nocturne", "bass", "duet")
	if err != nil {
		return nil, err
	}
	// The musical tone-table profiles (hydramodem/docs/MUSIC.md) pass to the tools as
	// --profile NAME; their pitches come from the tone table, so the linear tone-plan
	// overrides are usage errors. The duet (two frames per WAV) is Python-only.
	music := profile == "melody" || profile == "chime" || profile == "nocturne" || profile == "bass"
	if music && (u.has("base_freq") || u.has("tone_spacing") || u.has("n_tones")) {
		return nil, usagef("hydra: base_freq/tone_spacing/n_tones do not apply to a musical " +
			"profile (its pitches come from the tone table)")
	}
	interleaveOff := false
	if u.has("interleave") {
		on, err := uriBool(u.get("interleave", ""), "interleave")
		if err != nil {
			return nil, err
		}
		interleaveOff = !on
	}
	var fdma []string
	for _, k := range []struct{ key, flag string }{{"base_freq", "--base-freq"},
		{"tone_spacing", "--tone-spacing"}, {"baud", "--baud"}} {
		if u.has(k.key) {
			v, err := uriNum(u.get(k.key, ""), k.key)
			if err != nil {
				return nil, err
			}
			fdma = append(fdma, k.flag, fmtNum(v))
		}
	}
	if u.has("n_tones") {
		v, err := uriInt(u.get("n_tones", ""), "n_tones")
		if err != nil {
			return nil, err
		}
		fdma = append(fdma, "--n-tones", strconv.FormatInt(v, 10))
	}
	if im, err := uriChoice(u.get("impl", "tool"), "impl", "tool", "cffi"); err != nil {
		return nil, err
	} else if im == "cffi" {
		return nil, unsupportedf("hydra:impl=cffi (in-process libhydramodem) is Python-only; use impl=tool")
	}
	if profile == "duet" {
		return nil, unsupportedf("hydra:profile=duet (two frames per WAV) is Python-only")
	}
	c.tx = u.get("tx", "")
	if c.tx == "" {
		c.tx = os.Getenv("HYDRA_TX")
	}
	if c.tx == "" {
		c.tx, _ = exec.LookPath("frame_tx")
	}
	c.rx = u.get("rx", "")
	if c.rx == "" {
		c.rx = os.Getenv("HYDRA_RX")
	}
	if c.rx == "" {
		c.rx, _ = exec.LookPath("frame_rx")
	}
	if c.tx == "" || c.rx == "" {
		return nil, unsupportedf("HydraModem tools not found: build hydramodem/dcf-tools (build.sh) and put " +
			"frame_tx/frame_rx on PATH or in $HYDRA_TX/$HYDRA_RX")
	}
	txc, rxc := hydraToolCaps(c.tx), hydraToolCaps(c.rx)
	both := func(f string) bool { return txc[f] && rxc[f] }
	if music {
		if !both("--profile") {
			return nil, unsupportedf("hydra: profile=%s needs frame_tx/frame_rx with --profile "+
				"(rebuild hydramodem/dcf-tools)", profile)
		}
		c.prof = append(c.prof, "--profile", profile)
	}
	if profile == "aux" {
		if both("--profile") {
			c.prof = append(c.prof, "--profile", "aux")
		} else {
			c.prof = append(c.prof, hydraAuxFlags...)
			if both("--preamble") {
				c.prof = append(c.prof, "--preamble", "16")
			}
		}
	}
	if interleaveOff {
		if !both("--interleave") {
			return nil, unsupportedf("hydra: interleave=0 needs frame_tx/frame_rx with --interleave " +
				"(rebuild hydramodem/dcf-tools)")
		}
		c.prof = append(c.prof, "--interleave", "0")
	}
	c.prof = append(c.prof, fdma...)
	c.fecFlag = "--" + fec
	for _, d := range []string{c.out, c.in} {
		if d != "" {
			if err := os.MkdirAll(d, 0o755); err != nil {
				return nil, err
			}
		}
	}
	return c, nil
}

// ── readers ──────────────────────────────────────────────────────────────────

// finiteReader decodes a finite stream (.dcf bytes or hex lines) chunk by chunk.
type finiteReader struct {
	r        io.Reader
	closer   io.Closer
	isHex    bool
	sc       medium.StreamScanner
	badLines int
	carry    []byte
}

func (f *finiteReader) decode(chunk []byte) [][]byte {
	if !f.isHex {
		return f.sc.Feed(chunk)
	}
	buf := append(f.carry, chunk...)
	cut := bytes.LastIndexByte(buf, '\n') + 1
	var frames [][]byte
	if cut > 0 {
		frames = f.lines(buf[:cut])
	}
	f.carry = append([]byte(nil), buf[cut:]...)
	return frames
}

func (f *finiteReader) finish() [][]byte {
	if !f.isHex || len(f.carry) == 0 {
		return nil
	}
	fr := f.lines(f.carry)
	f.carry = nil
	return fr
}

func (f *finiteReader) lines(b []byte) [][]byte {
	frames, bad := medium.HexDecode(string(b))
	f.badLines += bad
	return frames
}

func (f *finiteReader) close() {
	if f.closer != nil {
		_ = f.closer.Close()
	}
}

// liveReader is an infinite medium: it delivers frames from its own goroutine.
type liveReader interface {
	start(push func([]byte)) error
	stop()
	counters() (skipped, bad, invalid int)
}

// udpReader: the udp medium's receive side (dialect proto: type-12 len-17 payloads only;
// bare: 32-B SuperPack -> 2 frames, 17-B -> 1).
type udpReader struct {
	conn    *net.UDPConn
	proto   bool
	stopped atomic.Bool
	done    chan struct{}
	invalid atomic.Int64
}

func (r *udpReader) start(push func([]byte)) error {
	r.done = make(chan struct{})
	go func() {
		defer close(r.done)
		buf := make([]byte, 65536)
		for !r.stopped.Load() {
			_ = r.conn.SetReadDeadline(time.Now().Add(50 * time.Millisecond))
			n, _, err := r.conn.ReadFromUDP(buf)
			if err != nil {
				var ne net.Error
				if errors.As(err, &ne) && ne.Timeout() {
					continue
				}
				return
			}
			d := buf[:n]
			if r.proto {
				t, _, _, payload, perr := medium.ProtoDecode(d)
				switch {
				case perr != nil:
					r.invalid.Add(1)
				case t != medium.MsgFrame:
					// an adapter envelope (types 1..11), not a frame on this medium
				case len(payload) != medium.FrameLen:
					r.invalid.Add(1)
				default:
					push(payload)
				}
				continue
			}
			frames := medium.BareDecode(d)
			if len(frames) == 0 {
				r.invalid.Add(1)
				continue
			}
			for _, f := range frames {
				push(f)
			}
		}
	}()
	return nil
}

func (r *udpReader) stop() {
	r.stopped.Store(true)
	if r.done != nil {
		<-r.done
	}
	_ = r.conn.Close()
}

func (r *udpReader) counters() (int, int, int) { return 0, 0, int(r.invalid.Load()) }

// tailReader: file/hex with follow=1 — tails a file for growth (poll 200 ms), or (hex
// without path) reads stdin lines.
type tailReader struct {
	path    string
	isHex   bool
	stopped atomic.Bool
	mu      sync.Mutex
	sc      medium.StreamScanner
	bad     int
}

func (r *tailReader) start(push func([]byte)) error {
	go func() {
		if r.isHex && r.path == "" {
			br := bufio.NewReader(os.Stdin)
			for !r.stopped.Load() {
				line, err := br.ReadString('\n')
				if line != "" {
					fr, bad := medium.HexDecode(line)
					r.mu.Lock()
					r.bad += bad
					r.mu.Unlock()
					for _, f := range fr {
						push(f)
					}
				}
				if err != nil {
					return
				}
			}
			return
		}
		var pos int64
		var carry []byte
		for !r.stopped.Load() {
			var data []byte
			if fh, err := os.Open(r.path); err == nil {
				if _, err := fh.Seek(pos, io.SeekStart); err == nil {
					data, _ = io.ReadAll(fh)
					pos += int64(len(data))
				}
				fh.Close()
			}
			var frames [][]byte
			r.mu.Lock()
			if r.isHex {
				buf := append(carry, data...)
				cut := bytes.LastIndexByte(buf, '\n') + 1
				if cut > 0 {
					fr, bad := medium.HexDecode(string(buf[:cut]))
					frames = fr
					r.bad += bad
				}
				carry = append([]byte(nil), buf[cut:]...)
			} else {
				frames = r.sc.Feed(data)
			}
			r.mu.Unlock()
			for _, f := range frames {
				push(f)
			}
			for i := 0; i < 4 && !r.stopped.Load(); i++ {
				time.Sleep(50 * time.Millisecond)
			}
		}
	}()
	return nil
}

func (r *tailReader) stop() { r.stopped.Store(true) }

func (r *tailReader) counters() (int, int, int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sc.Skipped(), r.bad, 0
}

// hydraReader: the _DirMedium spool — polls in= every 100 ms for unseen *.wav files not
// starting with '.', in sorted order, and decodes each with frame_rx.
type hydraReader struct {
	c       *hydraConf
	stopped atomic.Bool
	done    chan struct{}
}

func (r *hydraReader) decodeFile(path string) []byte {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	args := append([]string{path, r.c.fecFlag}, r.c.prof...)
	var out bytes.Buffer
	cmd := exec.CommandContext(ctx, r.c.rx, args...)
	cmd.Stdout = &out
	if err := cmd.Run(); err != nil {
		return nil
	}
	b, err := hex.DecodeString(strings.TrimSpace(out.String()))
	if err != nil || len(b) != medium.FrameLen {
		return nil
	}
	return b
}

func (r *hydraReader) start(push func([]byte)) error {
	r.done = make(chan struct{})
	go func() {
		defer close(r.done)
		seen := map[string]bool{}
		for !r.stopped.Load() {
			entries, _ := os.ReadDir(r.c.in) // sorted by file name
			for _, e := range entries {
				n := e.Name()
				if !strings.HasSuffix(n, ".wav") || strings.HasPrefix(n, ".") || seen[n] {
					continue
				}
				seen[n] = true
				if f := r.decodeFile(filepath.Join(r.c.in, n)); f != nil {
					push(f)
				}
			}
			for i := 0; i < 2 && !r.stopped.Load(); i++ {
				time.Sleep(50 * time.Millisecond)
			}
		}
	}()
	return nil
}

func (r *hydraReader) stop() {
	r.stopped.Store(true)
	if r.done != nil {
		select {
		case <-r.done:
		case <-time.After(2 * time.Second):
		}
	}
}

func (r *hydraReader) counters() (int, int, int) { return 0, 0, 0 }

// openReader opens the input side: exactly one of the two results is non-nil.
func openReader(spec string) (*finiteReader, liveReader, error) {
	u, err := parseURI(spec)
	if err != nil {
		return nil, nil, err
	}
	fin, err := finiteInput(u)
	if err != nil {
		return nil, nil, err
	}
	switch {
	case u.scheme == "stdio":
		return &finiteReader{r: os.Stdin}, nil, nil
	case (u.scheme == "file" || u.scheme == "hex") && fin:
		path := u.get("path", "")
		if path == "" && u.scheme == "file" {
			path = u.get("in", "")
		}
		if u.scheme == "file" && path == "" {
			return nil, nil, usagef("file: needs path=")
		}
		fr := &finiteReader{r: os.Stdin, isHex: u.scheme == "hex"}
		if path != "" {
			fh, err := os.Open(path)
			if err != nil {
				return nil, nil, err
			}
			fr.r, fr.closer = fh, fh
		}
		return fr, nil, nil
	case u.scheme == "file" || u.scheme == "hex": // follow=1
		mode, err := uriChoice(u.get("mode", "r"), "mode", "r", "w", "rw")
		if err != nil {
			return nil, nil, err
		}
		path := ""
		if u.scheme == "file" {
			path = u.get("in", "")
			if path == "" && strings.Contains(mode, "r") {
				path = u.get("path", "")
			}
			if path == "" {
				return nil, nil, usagef("file: needs path= (or in=/out=)")
			}
		} else {
			path = u.get("path", "")
		}
		return nil, &tailReader{path: path, isHex: u.scheme == "hex"}, nil
	case u.scheme == "udp":
		if !u.has("bind") {
			return nil, nil, usagef("udp as input needs bind=host:port")
		}
		o, err := udpOptions(u)
		if err != nil {
			return nil, nil, err
		}
		conn, err := net.ListenUDP("udp4", o.bind)
		if err != nil {
			return nil, nil, err
		}
		return nil, &udpReader{conn: conn, proto: o.dialect == "proto"}, nil
	case u.scheme == "hydra":
		if u.get("in", "") == "" {
			return nil, nil, usagef("hydra as input needs in=<dir>")
		}
		c, err := hydraConfig(u)
		if err != nil {
			return nil, nil, err
		}
		return nil, &hydraReader{c: c}, nil
	}
	for _, s := range []string{"afsk", "audio", "sdr", "janus"} {
		if u.scheme == s && u.get("in", "") == "" {
			return nil, nil, usagef("%s as input needs in=<dir>", s)
		}
	}
	return nil, nil, unsupportedMedium(u, "input")
}

// ── writers ──────────────────────────────────────────────────────────────────

type frameWriter interface {
	Write(frame []byte) error
	Poll() error // time-based flushes (bare-UDP lone frame); idle hook of infinite inputs
	Close() error
}

// streamWriter: file / stdio (raw 17-B frames) or hex (34 lowercase hex + "\n").
type streamWriter struct {
	w     *bufio.Writer
	f     *os.File // nil for stdout
	isHex bool
}

func (s *streamWriter) Write(frame []byte) error {
	if s.isHex {
		var line [2*medium.FrameLen + 1]byte
		hex.Encode(line[:], frame)
		line[2*medium.FrameLen] = '\n'
		_, err := s.w.Write(line[:2*len(frame)+1])
		return err
	}
	_, err := s.w.Write(frame)
	return err
}

func (s *streamWriter) Poll() error { return s.w.Flush() }

func (s *streamWriter) Close() error {
	err := s.w.Flush()
	if s.f != nil {
		if cerr := s.f.Close(); err == nil {
			err = cerr
		}
	}
	return err
}

type udpOpts struct {
	dialect string
	bind    *net.UDPAddr
	peers   []*net.UDPAddr
	pair    bool
	flush   time.Duration
	tsNow   bool
	seq     uint32
}

func udpOptions(u uri) (*udpOpts, error) {
	o := &udpOpts{}
	host, port, err := uriHostPort(u.get("bind", "0.0.0.0:0"), "bind")
	if err != nil {
		return nil, err
	}
	var peers [][2]any
	for _, p := range multiValues(u.get("peer", "")) {
		if i := strings.LastIndexByte(p, '@'); i >= 0 { // legacy id@host:port
			p = p[i+1:]
		}
		h, pt, err := uriHostPort(p, "peer")
		if err != nil {
			return nil, err
		}
		peers = append(peers, [2]any{h, pt})
	}
	if o.dialect, err = uriChoice(u.get("dialect", "proto"), "dialect", "proto", "bare"); err != nil {
		return nil, err
	}
	if o.pair, err = uriBool(u.get("pair", "1"), "pair"); err != nil {
		return nil, err
	}
	ms, err := uriInt(u.get("flush_ms", "20"), "flush_ms")
	if err != nil {
		return nil, err
	}
	if ms < 0 {
		ms = 0
	}
	o.flush = time.Duration(ms) * time.Millisecond
	ts, err := uriChoice(u.get("ts", "0"), "ts", "0", "now")
	if err != nil {
		return nil, err
	}
	o.tsNow = ts == "now"
	seq, err := uriInt(u.get("seq_start", "1"), "seq_start")
	if err != nil {
		return nil, err
	}
	o.seq = uint32(seq)
	if o.bind, err = resolveUDP4(host, port); err != nil {
		return nil, err
	}
	for _, p := range peers {
		a, err := resolveUDP4(p[0].(string), p[1].(int))
		if err != nil {
			return nil, err
		}
		o.peers = append(o.peers, a)
	}
	return o, nil
}

// udpWriter: proto = one 34-B ProtoMessage(type 12, seq++, ts) per frame; bare = pairs of
// gated frames as one 32-B SuperPack, a lone frame raw after flush_ms or at close.
type udpWriter struct {
	o         *udpOpts
	conn      *net.UDPConn
	pending   []byte
	pendingAt time.Time
}

func (w *udpWriter) send(d []byte) error {
	if w.conn == nil {
		c, err := net.ListenUDP("udp4", w.o.bind)
		if err != nil {
			return err
		}
		w.conn = c
	}
	for _, p := range w.o.peers {
		if _, err := w.conn.WriteToUDP(d, p); err != nil {
			return err
		}
	}
	return nil
}

func (w *udpWriter) Write(frame []byte) error {
	if w.o.dialect == "proto" {
		var ts uint64
		if w.o.tsNow {
			ts = uint64(time.Now().UnixMicro())
		}
		d := medium.ProtoFrameEncode(frame, w.o.seq, ts)
		w.o.seq++
		return w.send(d)
	}
	if !w.o.pair || !medium.Gate(frame) {
		if err := w.flushPending(); err != nil { // keep order: a held frame goes first
			return err
		}
		return w.send(frame)
	}
	if w.pending == nil {
		w.pending = append([]byte(nil), frame...)
		w.pendingAt = time.Now()
		return nil
	}
	sp, err := dcf.PackSuper(w.pending, frame)
	w.pending = nil
	if err != nil {
		return err
	}
	return w.send(sp[:])
}

func (w *udpWriter) flushPending() error {
	if w.pending == nil {
		return nil
	}
	p := w.pending
	w.pending = nil
	return w.send(p)
}

func (w *udpWriter) Poll() error {
	if w.pending != nil && time.Since(w.pendingAt) >= w.o.flush {
		return w.flushPending()
	}
	return nil
}

func (w *udpWriter) Close() error {
	err := w.flushPending()
	if w.conn != nil {
		_ = w.conn.Close()
	}
	return err
}

// hydraWriter: the _DirMedium spool — one WAV per frame, written by frame_tx as
// .<name>-N.wav.tmp and atomically renamed to <name>-%08d.wav (N from 1).
type hydraWriter struct {
	c *hydraConf
	n int
}

func (w *hydraWriter) Write(frame []byte) error {
	w.n++
	tmp := filepath.Join(w.c.out, fmt.Sprintf(".%s-%d.wav.tmp", w.c.name, w.n))
	final := filepath.Join(w.c.out, fmt.Sprintf("%s-%08d.wav", w.c.name, w.n))
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	args := append([]string{hex.EncodeToString(frame), tmp, w.c.fecFlag}, w.c.prof...)
	if err := exec.CommandContext(ctx, w.c.tx, args...).Run(); err != nil {
		return fmt.Errorf("frame_tx %s: %w", w.c.tx, err)
	}
	return os.Rename(tmp, final) // atomic publish so the reader never sees a partial
}

func (w *hydraWriter) Poll() error  { return nil }
func (w *hydraWriter) Close() error { return nil }

func openWriter(spec string) (frameWriter, error) {
	u, err := parseURI(spec)
	if err != nil {
		return nil, err
	}
	switch u.scheme {
	case "stdio":
		return &streamWriter{w: bufio.NewWriterSize(os.Stdout, 65536)}, nil
	case "file", "hex":
		path := u.get("path", "")
		if path == "" && u.scheme == "file" {
			path = u.get("out", "")
		}
		if u.scheme == "file" && path == "" {
			return nil, usagef("file: needs path=")
		}
		appendMode, err := uriBool(u.get("append", "0"), "append")
		if err != nil {
			return nil, err
		}
		sw := &streamWriter{isHex: u.scheme == "hex"}
		if path == "" {
			sw.w = bufio.NewWriterSize(os.Stdout, 65536)
			return sw, nil
		}
		flags := os.O_WRONLY | os.O_CREATE | os.O_TRUNC
		if appendMode {
			flags = os.O_WRONLY | os.O_CREATE | os.O_APPEND
		}
		fh, err := os.OpenFile(path, flags, 0o644)
		if err != nil {
			return nil, err
		}
		sw.f, sw.w = fh, bufio.NewWriterSize(fh, 65536)
		return sw, nil
	case "udp":
		if u.get("peer", "") == "" {
			return nil, usagef("udp as output needs peer=host:port")
		}
		o, err := udpOptions(u)
		if err != nil {
			return nil, err
		}
		return &udpWriter{o: o}, nil
	case "hydra":
		if u.get("out", "") == "" {
			return nil, usagef("hydra as output needs out=<dir>")
		}
		c, err := hydraConfig(u)
		if err != nil {
			return nil, err
		}
		return &hydraWriter{c: c}, nil
	}
	for _, s := range []string{"afsk", "audio", "sdr", "janus"} {
		if u.scheme == s && u.get("out", "") == "" {
			return nil, usagef("%s as output needs out=<dir>", s)
		}
	}
	return nil, unsupportedMedium(u, "output")
}

// ── the bounded drop-oldest FIFO between a live reader and the writer ─────────────

type frameQueue struct {
	mu      sync.Mutex
	items   [][]byte
	head    int
	max     int
	dropped int
	notify  chan struct{}
}

func newFrameQueue(max int) *frameQueue {
	return &frameQueue{max: max, notify: make(chan struct{}, 1)}
}

func (q *frameQueue) put(f []byte) {
	q.mu.Lock()
	q.items = append(q.items, f)
	for len(q.items)-q.head > q.max { // shed oldest-first
		q.items[q.head] = nil
		q.head++
		q.dropped++
	}
	if q.head > 1024 && q.head*2 > len(q.items) {
		q.items = append([][]byte(nil), q.items[q.head:]...)
		q.head = 0
	}
	q.mu.Unlock()
	select {
	case q.notify <- struct{}{}:
	default:
	}
}

func (q *frameQueue) get() ([]byte, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.head >= len(q.items) {
		q.items, q.head = q.items[:0], 0
		return nil, false
	}
	f := q.items[q.head]
	q.items[q.head] = nil
	q.head++
	return f, true
}

func (q *frameQueue) droppedCount() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.dropped
}

// ── io ───────────────────────────────────────────────────────────────────────

type ioOptions struct {
	in, out  string
	count    int64 // -1 = unset
	seconds  float64
	hasSecs  bool
	expect   int64 // -1 = unset
	validate bool
	queue    int
}

type ioStats struct {
	framesIn, framesOut, invalid, skipped, bad, dropped int
	seconds                                             float64
}

// runIO is the ordered pipeline reader -> frame gate -> writer. Finite inputs run to EOF
// and never drop; infinite inputs run until --count frames were written (or --expect,
// without --count), --seconds elapsed, or SIGINT/SIGTERM, their frames crossing a bounded
// FIFO of --queue items (oldest shed, counted in dropped).
func runIO(o ioOptions) (*ioStats, error) {
	t0 := time.Now()
	fr, live, err := openReader(o.in)
	if err != nil {
		return nil, err
	}
	wr, err := openWriter(o.out)
	if err != nil {
		if fr != nil {
			fr.close()
		}
		if live != nil {
			live.stop()
		}
		return nil, err
	}
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(sigCh)

	st := &ioStats{}
	limit := o.count
	if limit < 0 && o.expect >= 0 && live != nil {
		limit = o.expect
	}
	var deadline time.Time
	var deadlineC <-chan time.Time
	if o.hasSecs {
		deadline = t0.Add(time.Duration(o.seconds * float64(time.Second)))
		deadlineC = time.After(time.Until(deadline))
	}
	pastDeadline := func() bool { return o.hasSecs && !time.Now().Before(deadline) }
	done := func() bool { return limit >= 0 && int64(st.framesOut) >= limit }
	handle := func(f []byte) error {
		st.framesIn++
		if o.validate && !medium.Gate(f) {
			st.invalid++
			return nil
		}
		if err := wr.Write(f); err != nil {
			return err
		}
		st.framesOut++
		return nil
	}

	var runErr error
	if fr != nil {
		runErr = runFinite(fr, handle, done, pastDeadline, sigCh, deadlineC)
		st.skipped, st.bad = fr.sc.Skipped(), fr.badLines
		fr.close()
	} else {
		q := newFrameQueue(o.queue)
		if err := live.start(q.put); err != nil {
			_ = wr.Close()
			return nil, err
		}
		interrupted := false
	loop:
		for !done() {
			if f, ok := q.get(); ok {
				if runErr = handle(f); runErr != nil {
					break
				}
				continue
			}
			if runErr = wr.Poll(); runErr != nil {
				break
			}
			if pastDeadline() || interrupted {
				break
			}
			select {
			case <-q.notify:
			case <-sigCh:
				interrupted = true
			case <-deadlineC:
			case <-time.After(2 * time.Millisecond):
			}
			if interrupted {
				break loop
			}
		}
		live.stop()
		for runErr == nil && !done() { // frames already received are not dropped
			f, ok := q.get()
			if !ok {
				break
			}
			runErr = handle(f)
		}
		st.dropped = q.droppedCount()
		var inv int
		st.skipped, st.bad, inv = live.counters()
		st.invalid += inv
	}
	if cerr := wr.Close(); runErr == nil {
		runErr = cerr
	}
	if runErr != nil {
		return nil, runErr
	}
	st.seconds = math.Round(time.Since(t0).Seconds()*1000) / 1000
	return st, nil
}

type chunkMsg struct {
	data []byte
	err  error
}

// runFinite drives a finite reader: a helper goroutine only moves raw bytes (so SIGINT and
// --seconds can interrupt a blocked stdin); decoding, gating and writing stay in order on
// this goroutine, and nothing is ever dropped.
func runFinite(fr *finiteReader, handle func([]byte) error, done, pastDeadline func() bool,
	sigCh chan os.Signal, deadlineC <-chan time.Time) error {
	if done() {
		return nil
	}
	chunks := make(chan chunkMsg, 1)
	go func() {
		defer close(chunks)
		for {
			buf := make([]byte, 65536)
			n, err := fr.r.Read(buf)
			if n > 0 {
				chunks <- chunkMsg{data: buf[:n]}
			}
			if err == io.EOF {
				return
			}
			if err != nil {
				chunks <- chunkMsg{err: err}
				return
			}
		}
	}()
	for {
		var frames [][]byte
		eof := false
		select {
		case m, ok := <-chunks:
			if !ok {
				frames, eof = fr.finish(), true
			} else if m.err != nil {
				return m.err
			} else {
				frames = fr.decode(m.data)
			}
		case <-sigCh:
			return nil
		case <-deadlineC:
			return nil
		}
		for _, f := range frames {
			if err := handle(f); err != nil {
				return err
			}
			if done() || pastDeadline() {
				return nil
			}
		}
		if eof {
			if !fr.isHex {
				fr.sc.Flush()
			}
			return nil
		}
	}
}

func cmdIO(args []string) (int, error) {
	a, err := parseArgs(args, []string{"in", "out", "count", "seconds", "expect", "queue"},
		[]string{"no-validate", "stats"}, nil)
	if err != nil {
		return 0, err
	}
	if len(a.pos) > 0 {
		return 0, usagef("unrecognized arguments: %s", strings.Join(a.pos, " "))
	}
	var missing []string
	for _, k := range []string{"in", "out"} {
		if _, ok := a.vals[k]; !ok {
			missing = append(missing, "--"+k)
		}
	}
	if len(missing) > 0 {
		return 0, usagef("the following arguments are required: %s", strings.Join(missing, ", "))
	}
	o := ioOptions{in: a.vals["in"], out: a.vals["out"], count: -1, expect: -1,
		validate: !a.flags["no-validate"], queue: 256}
	for _, k := range []string{"count", "expect"} {
		if s, ok := a.vals[k]; ok {
			v, err := pyInt0(s)
			if err != nil || v < 0 {
				return 0, usagef("argument --%s: invalid value: '%s' (must be an integer >= 0)", k, s)
			}
			if k == "count" {
				o.count = v
			} else {
				o.expect = v
			}
		}
	}
	if s, ok := a.vals["queue"]; ok {
		v, err := pyInt0(s)
		if err != nil || v < 1 {
			return 0, usagef("argument --queue: invalid value: '%s' (must be an integer >= 1)", s)
		}
		o.queue = int(v)
	}
	if s, ok := a.vals["seconds"]; ok {
		v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
		if err != nil {
			return 0, usagef("argument --seconds: invalid float value: '%s'", s)
		}
		o.seconds, o.hasSecs = v, true
	}
	st, err := runIO(o)
	if err != nil {
		return 0, err
	}
	if a.flags["stats"] {
		fmt.Fprintln(os.Stderr, pyJSON([]kv{{"in", o.in}, {"out", o.out},
			{"frames_in", st.framesIn}, {"frames_out", st.framesOut},
			{"invalid_frames", st.invalid}, {"skipped_bytes", st.skipped},
			{"bad_lines", st.bad}, {"dropped", st.dropped}, {"seconds", st.seconds}}))
	}
	if o.expect >= 0 && int64(st.framesOut) != o.expect {
		fmt.Fprintf(os.Stderr, "punctim io: expected %d frames, wrote %d\n", o.expect, st.framesOut)
		return exitExpect, nil
	}
	return exitOK, nil
}

// ── certify ──────────────────────────────────────────────────────────────────

// findVectors locates medium_vectors.json: an explicit dir/file, else $PUNCTIM_VECTORS,
// else Documentation/ found by walking up from the executable and from the working
// directory, else the python/MCP copy.
func findVectors(explicit string) (string, error) {
	var cands []string
	withName := func(base string) string {
		if strings.HasSuffix(base, ".json") {
			return base
		}
		return filepath.Join(base, "medium_vectors.json")
	}
	if explicit != "" {
		cands = append(cands, withName(explicit))
	} else {
		if env := os.Getenv("PUNCTIM_VECTORS"); env != "" {
			cands = append(cands, withName(env))
		}
		var roots []string
		if exe, err := os.Executable(); err == nil {
			if r, err := filepath.EvalSymlinks(exe); err == nil {
				exe = r
			}
			roots = append(roots, filepath.Dir(exe))
		}
		if wd, err := os.Getwd(); err == nil {
			roots = append(roots, wd)
		}
		for _, rel := range []string{"Documentation", filepath.Join("python", "MCP")} {
			for _, r := range roots {
				d := r
				for i := 0; i < 6; i++ {
					cands = append(cands, filepath.Join(d, rel, "medium_vectors.json"))
					d = filepath.Dir(d)
				}
			}
		}
	}
	for _, c := range cands {
		if fi, err := os.Stat(c); err == nil && !fi.IsDir() {
			return filepath.Clean(c), nil
		}
	}
	return "", errors.New("medium_vectors.json not found (use --vectors DIR or $PUNCTIM_VECTORS)")
}

// certVectors mirrors Documentation/medium_vectors.json (numbers kept exact).
type certVectors struct {
	Anchors map[string]any `json:"anchors"`
	Basis   []struct {
		ID      int    `json:"id"`
		Type    uint8  `json:"type"`
		Seq     uint16 `json:"seq"`
		Src     uint16 `json:"src"`
		Dst     uint16 `json:"dst"`
		Payload string `json:"payload"`
		Ts      uint32 `json:"ts"`
		Hex     string `json:"hex"`
	} `json:"basis"`
	Families map[string]json.RawMessage `json:"families"`
}

func hx(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(fmt.Sprintf("bad hex %q", s))
	}
	return b
}

func hxs(ss []string) [][]byte {
	out := make([][]byte, len(ss))
	for i, s := range ss {
		out[i] = hx(s)
	}
	return out
}

func sameFrames(a [][]byte, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if hex.EncodeToString(a[i]) != b[i] {
			return false
		}
	}
	return true
}

func certStream(raw json.RawMessage) (int, string) {
	var fam struct {
		Cases []struct {
			Name         string
			Input        string
			Frames       []string
			SkippedBytes int `json:"skipped_bytes"`
			TailBytes    int `json:"tail_bytes"`
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	for _, c := range fam.Cases {
		buf := hx(c.Input)
		fr, sk, tl := medium.StreamDecode(buf)
		if !sameFrames(fr, c.Frames) || sk != c.SkippedBytes || tl != c.TailBytes {
			return 0, c.Name + ": stream_decode mismatch"
		}
		for _, step := range []int{1, 7, 16, 17, 64} {
			var sc medium.StreamScanner
			var got [][]byte
			for i := 0; i < len(buf); i += step {
				got = append(got, sc.Feed(buf[i:min(i+step, len(buf))])...)
			}
			if !sameFrames(got, c.Frames) || sc.Skipped() != c.SkippedBytes || len(sc.Flush()) != c.TailBytes {
				return 0, fmt.Sprintf("%s: StreamScanner mismatch at chunk %d", c.Name, step)
			}
		}
		re, sk2, tl2 := medium.StreamDecode(medium.StreamEncode(hxs(c.Frames)))
		if !sameFrames(re, c.Frames) || sk2 != 0 || tl2 != 0 {
			return 0, c.Name + ": stream_encode round trip"
		}
	}
	return len(fam.Cases), ""
}

func certHex(raw json.RawMessage) (int, string) {
	var fam struct {
		Cases []struct {
			Name        string
			Text        string
			Frames      []string
			DecodeInput string   `json:"decode_input"`
			Decoded     []string `json:"decoded"`
			BadLines    int      `json:"bad_lines"`
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	for _, c := range fam.Cases {
		if medium.HexEncode(hxs(c.Frames)) != c.Text {
			return 0, c.Name + ": hex_encode mismatch"
		}
		fr, bad := medium.HexDecode(c.DecodeInput)
		if !sameFrames(fr, c.Decoded) || bad != c.BadLines {
			return 0, c.Name + ": hex_decode mismatch"
		}
	}
	return len(fam.Cases), ""
}

func certProto(raw json.RawMessage) (int, string) {
	var fam struct {
		HeaderLen int              `json:"header_len"`
		MsgFrame  int              `json:"msg_frame"`
		Types     map[string]uint8 `json:"types"`
		Cases     []struct {
			Name          string
			Type          uint8
			Seq           uint32
			Ts            json.Number
			TsHex         string `json:"ts_hex"`
			Payload       string
			Datagram      string
			AcceptAsFrame bool `json:"accept_as_frame"`
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	if fam.HeaderLen != medium.ProtoHeaderLen || fam.MsgFrame != int(medium.MsgFrame) ||
		len(fam.Types) != len(medium.MsgTypes) {
		return 0, "type registry / header mismatch"
	}
	for k, v := range fam.Types {
		if id, ok := medium.MsgTypes[k]; !ok || id != v {
			return 0, "type registry / header mismatch"
		}
	}
	for _, c := range fam.Cases {
		ts, err := strconv.ParseUint(c.Ts.String(), 10, 64)
		th, err2 := strconv.ParseUint(c.TsHex, 16, 64)
		if err != nil || err2 != nil || ts != th {
			return 0, c.Name + ": ts_hex != ts"
		}
		pl, dg := hx(c.Payload), hx(c.Datagram)
		if !bytes.Equal(medium.ProtoEncode(c.Type, c.Seq, ts, pl), dg) {
			return 0, c.Name + ": proto_encode mismatch"
		}
		t, seq, ts2, pl2, err := medium.ProtoDecode(dg)
		if err != nil || t != c.Type || seq != c.Seq || ts2 != ts || !bytes.Equal(pl2, pl) {
			return 0, c.Name + ": proto_decode mismatch"
		}
		fr, ok := medium.ProtoFrameDecode(dg)
		if ok != c.AcceptAsFrame {
			return 0, c.Name + ": accept_as_frame mismatch"
		}
		if ok && (!bytes.Equal(fr, pl) || !bytes.Equal(medium.ProtoFrameEncode(pl, c.Seq, ts), dg)) {
			return 0, c.Name + ": proto_frame round trip"
		}
	}
	return len(fam.Cases), ""
}

func certBare(raw json.RawMessage) (int, string) {
	var fam struct {
		Cases []struct {
			Name      string
			Frames    []string
			Datagrams []string
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	for _, c := range fam.Cases {
		if !sameFrames(medium.BareEncode(hxs(c.Frames), true), c.Datagrams) {
			return 0, c.Name + ": bare_encode mismatch"
		}
		var back [][]byte
		for _, d := range c.Datagrams {
			back = append(back, medium.BareDecode(hx(d))...)
		}
		if !sameFrames(back, c.Frames) {
			return 0, c.Name + ": bare_decode mismatch"
		}
	}
	return len(fam.Cases), ""
}

func certL2(raw json.RawMessage) (int, string) {
	var fam struct {
		Hdr    int
		Filler string
		Cases  []struct {
			Name    string
			Frames  []string
			Payload string
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	if fam.Filler != hex.EncodeToString(medium.L2Filler) || fam.Hdr != medium.L2Hdr {
		return 0, "filler / header mismatch"
	}
	for _, c := range fam.Cases {
		pl, err := medium.L2Batch(hxs(c.Frames))
		if err != nil || hex.EncodeToString(pl) != c.Payload {
			return 0, c.Name + ": l2_batch mismatch"
		}
		fr, err := medium.L2Unbatch(hx(c.Payload))
		if err != nil || !sameFrames(fr, c.Frames) {
			return 0, c.Name + ": l2_unbatch mismatch"
		}
	}
	return len(fam.Cases), ""
}

func certHydra(raw json.RawMessage) (int, string) {
	var fam struct {
		Profiles map[string]map[string]float64
		Cases    []struct {
			Name             string
			Profile          string
			Fec              string
			Interleave       int
			NTones           int `json:"n_tones"`
			Frame            string
			CodedBits        int `json:"coded_bits"`
			InterleaveStride int `json:"interleave_stride"`
			TotalSyms        int `json:"total_syms"`
			Symbols          string
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	if len(fam.Profiles) != 2 {
		return 0, "profiles mismatch"
	}
	for name, vp := range fam.Profiles {
		p, err := medium.HydraProfileByName(name)
		if err != nil {
			return 0, fmt.Sprintf("profile %s mismatch", name)
		}
		il := 0.0
		if p.Interleave {
			il = 1
		}
		want := map[string]float64{"sample_rate": p.SampleRate, "baud": p.Baud,
			"n_tones": float64(p.NTones), "base_freq": p.BaseFreq, "tone_spacing": p.ToneSpacing,
			"preamble_syms": float64(p.PreambleSyms), "sync_word": float64(p.SyncWord),
			"fec_mode": float64(p.FecMode), "interleave": il, "tx_gain": p.TxGain}
		if len(vp) != len(want) {
			return 0, fmt.Sprintf("profile %s mismatch", name)
		}
		for k, v := range want {
			if got, ok := vp[k]; !ok || got != v {
				return 0, fmt.Sprintf("profile %s mismatch", name)
			}
		}
	}
	for _, c := range fam.Cases {
		p, err := medium.HydraProfileByName(c.Profile)
		fm, ok := medium.FecNames[c.Fec]
		if err != nil || !ok {
			return 0, c.Name + ": derived profile fields mismatch"
		}
		p.FecMode, p.Interleave, p.NTones = fm, c.Interleave != 0, c.NTones
		if err := p.Init(); err != nil || p.CodedBits != c.CodedBits ||
			p.InterleaveStride != c.InterleaveStride || p.TotalSyms != c.TotalSyms {
			return 0, c.Name + ": derived profile fields mismatch"
		}
		f := hx(c.Frame)
		if s, err := medium.HydraSymbolsEncode(&p, f); err != nil || s != c.Symbols {
			return 0, c.Name + ": symbols mismatch"
		}
		if back, ok := medium.HydraSymbolsDecode(&p, c.Symbols); !ok || !bytes.Equal(back, f) {
			return 0, c.Name + ": decode mismatch"
		}
	}
	return len(fam.Cases), ""
}

func certAfsk(raw json.RawMessage) (int, string) {
	var fam struct {
		Profiles map[string]map[string]float64
		Cases    []struct {
			Name    string
			Profile string
			Fec     bool
			Frame   string
			NBits   int `json:"n_bits"`
			Bits    string
		}
	}
	if err := json.Unmarshal(raw, &fam); err != nil {
		return 0, err.Error()
	}
	if len(fam.Profiles) != len(medium.AfskProfiles) {
		return 0, "profiles mismatch"
	}
	for name, vp := range fam.Profiles {
		p, ok := medium.AfskProfiles[name]
		if !ok || len(vp) != 4 || vp["mark"] != p.Mark || vp["space"] != p.Space ||
			vp["baud"] != float64(p.Baud) || vp["preamble_bits"] != float64(p.PreambleBits) {
			return 0, "profiles mismatch"
		}
	}
	for _, c := range fam.Cases {
		f := hx(c.Frame)
		b, err := medium.AfskBitsEncode(f, c.Profile, c.Fec)
		if err != nil || b != c.Bits || len(b) != c.NBits {
			return 0, c.Name + ": bits mismatch"
		}
		if back, ok := medium.AfskBitsDecode(b, c.Profile, c.Fec); !ok || !bytes.Equal(back, f) {
			return 0, c.Name + ": decode mismatch"
		}
	}
	return len(fam.Cases), ""
}

var certFns = map[string]func(json.RawMessage) (int, string){
	"stream": certStream, "hex": certHex, "udp_proto": certProto, "udp_bare": certBare,
	"l2eth": certL2, "hydra_symbols": certHydra, "afsk_bits": certAfsk,
}

func anchorNum(a map[string]any, k string) float64 {
	if v, ok := a[k].(float64); ok {
		return v
	}
	return math.NaN()
}

func certAnchors(v *certVectors) string {
	a := v.Anchors
	nums := []struct {
		got float64
		key string
	}{
		{float64(dcf.Crc16CCITT([]byte("123456789"))), "crc_123456789"},
		{float64(dcf.Crc16CCITT(make([]byte, 15))), "crc_zero15"},
		{medium.FrameLen, "frame_len"}, {medium.ProtoHeaderLen, "proto_header_len"},
		{float64(medium.MsgFrame), "msg_frame"}, {dcf.SuperSize, "super_len"},
		{medium.L2Hdr, "l2_hdr"}, {medium.HydraSyncWord, "hydra_sync_word"},
		{medium.AfskSyncWord, "afsk_sync"},
		{float64(medium.AfskCRC8([]byte("123456789"))), "afsk_crc8_123456789"},
	}
	bad := ""
	for _, n := range nums {
		if anchorNum(a, n.key) != n.got {
			bad = "anchor mismatch"
		}
	}
	if s, _ := a["l2_filler"].(string); s != hex.EncodeToString(medium.L2Filler) {
		bad = "anchor mismatch"
	}
	for _, b := range v.Basis {
		var pl [4]byte
		p, err := hex.DecodeString(b.Payload)
		if err != nil || len(p) != 4 {
			return fmt.Sprintf("basis %d mismatch", b.ID)
		}
		copy(pl[:], p)
		f := dcf.Frame{Version: dcf.Version, Type: dcf.FrameType(b.Type), Seq: b.Seq, Src: b.Src,
			Dst: b.Dst, Payload: pl, TsUs: b.Ts & 0xFFFFFF}.Encode()
		if hex.EncodeToString(f[:]) != b.Hex {
			bad = fmt.Sprintf("basis %d mismatch", b.ID)
		}
	}
	return bad
}

func cmdCertify(args []string) (int, error) {
	a, err := parseArgs(args, []string{"vectors"}, nil, []string{"family"})
	if err != nil {
		return 0, err
	}
	if len(a.pos) > 0 {
		return 0, usagef("unrecognized arguments: %s", strings.Join(a.pos, " "))
	}
	fams := families
	if f, ok := a.multi["family"]; ok {
		fams = f
	}
	for _, f := range fams {
		if certFns[f] == nil {
			return 0, usagef("unknown family '%s' (one of: %s)", f, strings.Join(families, ", "))
		}
	}
	path, err := findVectors(a.vals["vectors"])
	if err != nil {
		return 0, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	var v certVectors
	if err := json.Unmarshal(data, &v); err != nil {
		return 0, fmt.Errorf("%s: %w", path, err)
	}
	fmt.Printf("vectors: %s\n", path)
	failed, total := 0, 0
	report := func(fam string, n int, msg, what string) {
		if msg == "" {
			fmt.Printf("PASS %s (%d %s)\n", fam, n, what)
		} else {
			failed++
			fmt.Printf("FAIL %s: %s\n", fam, msg)
		}
	}
	report("anchors", len(v.Basis), certAnchors(&v), "basis frames")
	for _, f := range fams {
		raw, ok := v.Families[f]
		if !ok {
			report(f, 0, "family missing from vectors", "cases")
			continue
		}
		n, msg := func() (n int, msg string) {
			defer func() {
				if r := recover(); r != nil {
					n, msg = 0, fmt.Sprint(r)
				}
			}()
			return certFns[f](raw)
		}()
		if msg == "" {
			total += n
		}
		report(f, n, msg, "cases")
	}
	if failed > 0 {
		s := "ies"
		if failed == 1 {
			s = "y"
		}
		fmt.Printf("CERTIFICATION FAILED (%d famil%s)\n", failed, s)
		return exitCert, nil
	}
	fmt.Printf("ALL MEDIUM VECTORS PASS (%d cases, impl %s)\n", total, impl)
	return exitOK, nil
}
