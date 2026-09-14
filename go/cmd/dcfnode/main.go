// SPDX-License-Identifier: LGPL-3.0-only

// Command dcfnode is a runnable DCF node CLI over the stdlib-only Go SDK
// (github.com/ALH477/Punctim/go/node) — the Go analogue of the Rust `dcf`
// binary. It exposes the same capabilities the SDK has: run a UDP DeModFrame
// node, peer with others, measure per-peer RTT, and send every adapter
// (position, audio, DCF-Game, DCF-Text) over the certified 17-byte wire.
//
// Usage:
//
//	dcfnode version
//	dcfnode start   [--bind 0.0.0.0:7777] [--peer id@host:port ...] [--config c.json]
//	dcfnode send-text     --peer host:port [--id n] --channel N --text "hi"
//	dcfnode send-game     --peer host:port --channel N --hex DEADBEEF [--type 2]
//	dcfnode send-audio    --peer host:port --channel N --hex 0011..  [--codec 1]
//	dcfnode send-position --peer host:port --x 1 --y 2 --z 3
//	dcfnode benchmark     --peer host:port [--count 100]
package main

import (
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ALH477/Punctim/go/audio"
	"github.com/ALH477/Punctim/go/game"
	"github.com/ALH477/Punctim/go/node"
	"github.com/ALH477/Punctim/go/text"
)

const version = "0.3.0"

func main() {
	log.SetFlags(log.Ltime)
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version", "-v", "--version":
		fmt.Printf("dcfnode %s — DCF Go SDK node (17-byte DeModFrame, UDP, stdlib-only)\n", version)
	case "start":
		cmdStart(os.Args[2:])
	case "send-text":
		cmdSendText(os.Args[2:])
	case "send-game":
		cmdSendGame(os.Args[2:])
	case "send-audio":
		cmdSendAudio(os.Args[2:])
	case "send-position":
		cmdSendPosition(os.Args[2:])
	case "benchmark":
		cmdBenchmark(os.Args[2:])
	case "help", "-h", "--help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `dcfnode — DCF Go SDK node CLI

commands:
  version                                  print version + wire info
  start         [--bind h:p] [--peer ...]  run a node (receiver + ping + ARQ), block
  send-text     --peer h:p --channel N --text S
  send-game     --peer h:p --channel N --hex BYTES [--type T]
  send-audio    --peer h:p --channel N --hex BYTES [--codec C]
  send-position --peer h:p --x X --y Y --z Z
  benchmark     --peer h:p [--count N]

--peer accepts host:port or id@host:port (repeatable on start).
`)
}

// peerSpec parses "host:port" or "id@host:port".
func peerSpec(s string) (id, host string, port uint16, err error) {
	id = "peer"
	if at := strings.IndexByte(s, '@'); at >= 0 {
		id, s = s[:at], s[at+1:]
	}
	host, portStr, err := net.SplitHostPort(s)
	if err != nil {
		return "", "", 0, err
	}
	p, err := strconv.ParseUint(portStr, 10, 16)
	if err != nil {
		return "", "", 0, err
	}
	return id, host, uint16(p), nil
}

// newNode builds a node bound to bindAddr ("host:port"), or DefaultConfig if empty.
func newNode(bindAddr, configPath string) (*node.DcfNode, error) {
	var cfg node.DcfConfig
	if configPath != "" {
		c, err := node.InitFromConfigFile(configPath)
		if err != nil {
			return nil, err
		}
		cfg = *c
	} else {
		cfg = node.DefaultConfig()
	}
	if bindAddr != "" {
		host, portStr, err := net.SplitHostPort(bindAddr)
		if err != nil {
			return nil, fmt.Errorf("--bind %q: %w", bindAddr, err)
		}
		p, err := strconv.ParseUint(portStr, 10, 16)
		if err != nil {
			return nil, fmt.Errorf("--bind port: %w", err)
		}
		cfg.Host, cfg.UDPPort = host, uint16(p)
	}
	return node.New(&cfg)
}

// logHandler logs every decoded message and reassembles DCF-Game/Text payloads.
type logHandler struct {
	node.DefaultMessageHandler
	n      *node.DcfNode
	gReasm *game.GameReassembler
	tReasm *text.TextReassembler
}

func (h *logHandler) HandlePosition(p node.Position, from *net.UDPAddr) {
	log.Printf("position from %s: (%.2f, %.2f, %.2f)", from, p.X, p.Y, p.Z)
}
func (h *logHandler) HandleAudio(d []byte, from *net.UDPAddr) {
	log.Printf("audio from %s: %d bytes", from, len(d))
}
func (h *logHandler) HandleGame(payload []byte, from *net.UDPAddr) {
	if pkt := h.n.ReassembleGamePayload(h.gReasm, payload); pkt != nil {
		log.Printf("game from %s: type=%d %d bytes (packet %d)", from, pkt.MsgTypeID, len(pkt.Payload), pkt.PacketID)
	}
}
func (h *logHandler) HandleText(payload []byte, from *net.UDPAddr) {
	if pkt := h.n.ReassembleTextPayload(h.tReasm, payload); pkt != nil {
		log.Printf("text from %s on ch %d: %q", from, pkt.Dst, pkt.Text)
	}
}

func cmdStart(args []string) {
	fs := flag.NewFlagSet("start", flag.ExitOnError)
	bind := fs.String("bind", "", "bind address host:port (default 0.0.0.0:7777)")
	config := fs.String("config", "", "JSON config file")
	mode := fs.String("mode", "p2p", "mesh mode: p2p | auto | master (self-healing runtime)")
	nodeID := fs.Uint("node-id", 0, "this node's numeric mesh id (uint16; required for auto/master)")
	master := fs.String("master", "", "peer id to REPORT to (auto mode)")
	var peers multiFlag
	fs.Var(&peers, "peer", "peer id@host:port (repeatable; id must be numeric in mesh modes)")
	_ = fs.Parse(args)

	n, err := newNode(*bind, *config)
	if err != nil {
		log.Fatal(err)
	}
	for _, ps := range peers {
		id, host, port, err := peerSpec(ps)
		if err != nil {
			log.Fatalf("--peer %q: %v", ps, err)
		}
		n.AddPeer(id, host, port)
		log.Printf("added peer %s at %s:%d", id, host, port)
	}
	if *mode != "p2p" {
		n.EnableMesh(node.NewMeshRuntime(*mode, uint16(*nodeID), *master, int(n.Config().GroupRTTThreshold)))
		log.Printf("self-healing mesh: mode=%s node-id=%d master=%q", *mode, *nodeID, *master)
	}

	h := &logHandler{n: n, gReasm: game.NewGameReassembler(), tReasm: text.NewTextReassembler()}
	if err := n.Start(h); err != nil {
		log.Fatal(err)
	}
	defer n.Stop()
	log.Printf("dcfnode %s listening as %s on :%d (Ctrl-C to stop)", version, n.NodeID(), n.LocalPort())

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("shutting down")
}

// withPeerNode spins up an ephemeral node, adds one peer, runs fn, then stops.
func withPeerNode(peer string, fn func(n *node.DcfNode, peerID string)) {
	id, host, port, err := peerSpec(peer)
	if err != nil {
		log.Fatalf("--peer %q: %v", peer, err)
	}
	cfg := node.DefaultConfig()
	cfg.Host, cfg.UDPPort = "0.0.0.0", 0 // ephemeral source port
	n, err := node.New(&cfg)
	if err != nil {
		log.Fatal(err)
	}
	if err := n.Start(&logHandler{n: n, gReasm: game.NewGameReassembler(), tReasm: text.NewTextReassembler()}); err != nil {
		log.Fatal(err)
	}
	defer n.Stop()
	n.AddPeer(id, host, port)
	fn(n, id)
	time.Sleep(300 * time.Millisecond) // let the datagram flush
}

func nowMicros() uint32 { return uint32(time.Now().UnixMicro()) }

func cmdSendText(args []string) {
	fs := flag.NewFlagSet("send-text", flag.ExitOnError)
	peer := fs.String("peer", "", "peer host:port")
	channel := fs.Uint("channel", 1, "channel id (frame dst)")
	id := fs.Uint("id", 1, "packet id")
	msg := fs.String("text", "hello over DeModFrame", "message text")
	_ = fs.Parse(args)
	requirePeer(*peer)
	withPeerNode(*peer, func(n *node.DcfNode, _ string) {
		if err := n.SendTextDCF([]byte(*msg), uint16(*id), nowMicros(), 1, uint16(*channel), 0, true); err != nil {
			log.Fatal(err)
		}
		log.Printf("sent %d-byte text on ch %d to %s", len(*msg), *channel, *peer)
	})
}

func cmdSendGame(args []string) {
	fs := flag.NewFlagSet("send-game", flag.ExitOnError)
	peer := fs.String("peer", "", "peer host:port")
	channel := fs.Uint("channel", 1, "channel id (frame dst)")
	typ := fs.Uint("type", 2, "msg_type_id (0=SNAPSHOT,1=INPUT,2=EVENT,3=JOIN)")
	hx := fs.String("hex", "deadbeef", "message body hex")
	_ = fs.Parse(args)
	requirePeer(*peer)
	body := mustHex(*hx)
	withPeerNode(*peer, func(n *node.DcfNode, _ string) {
		if err := n.SendGameDCF(uint8(*typ), body, 1, nowMicros(), 1, uint16(*channel), true); err != nil {
			log.Fatal(err)
		}
		log.Printf("sent %d-byte game msg (type %d) on ch %d to %s", len(body), *typ, *channel, *peer)
	})
}

func cmdSendAudio(args []string) {
	fs := flag.NewFlagSet("send-audio", flag.ExitOnError)
	peer := fs.String("peer", "", "peer host:port")
	channel := fs.Uint("channel", 1, "channel id (frame dst)")
	codec := fs.Uint("codec", uint(audio.CodecPcmDiag), "codec_id (0=Opus,1=PCM-diag,2=PM)")
	hx := fs.String("hex", "00112233", "encoded audio block hex")
	_ = fs.Parse(args)
	requirePeer(*peer)
	block := mustHex(*hx)
	withPeerNode(*peer, func(n *node.DcfNode, _ string) {
		if err := n.SendAudioDCF(uint8(*codec), block, 1, nowMicros(), uint16(*channel)); err != nil {
			log.Fatal(err)
		}
		log.Printf("sent %d-byte audio block (codec %d) on ch %d to %s", len(block), *codec, *channel, *peer)
	})
}

func cmdSendPosition(args []string) {
	fs := flag.NewFlagSet("send-position", flag.ExitOnError)
	peer := fs.String("peer", "", "peer host:port")
	x := fs.Float64("x", 0, "x")
	y := fs.Float64("y", 0, "y")
	z := fs.Float64("z", 0, "z")
	_ = fs.Parse(args)
	requirePeer(*peer)
	withPeerNode(*peer, func(n *node.DcfNode, _ string) {
		if err := n.SendPosition(float32(*x), float32(*y), float32(*z)); err != nil {
			log.Fatal(err)
		}
		log.Printf("sent position (%.2f, %.2f, %.2f) to %s", *x, *y, *z, *peer)
	})
}

func cmdBenchmark(args []string) {
	fs := flag.NewFlagSet("benchmark", flag.ExitOnError)
	peer := fs.String("peer", "", "peer host:port")
	count := fs.Int("count", 100, "ping count")
	_ = fs.Parse(args)
	requirePeer(*peer)
	withPeerNode(*peer, func(n *node.DcfNode, peerID string) {
		res, err := n.Benchmark(peerID, *count)
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("benchmark to %s: %+v", *peer, res)
	})
}

func requirePeer(p string) {
	if p == "" {
		log.Fatal("--peer host:port is required")
	}
}

func mustHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		log.Fatalf("bad --hex %q: %v", s, err)
	}
	return b
}

// multiFlag collects repeated --peer flags.
type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error {
	*m = append(*m, v)
	return nil
}
