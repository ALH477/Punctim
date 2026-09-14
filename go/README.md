<!-- SPDX-License-Identifier: LGPL-3.0-only -->
# Punctim / DCF — Go SDK

A **stdlib-only** Go implementation of the DeMoD Communication Framework: the certified 17-byte
`DeModFrame` wire quantum, the three adapters that ride over it (game / audio / text), and a UDP
node that ties them together. No third-party dependencies — `go.mod` has no `require` block and
there is no `go.sum`.

Module: `github.com/ALH477/Punctim/go` (Go 1.21+).

## Packages

| Package | What it is | Certified against |
|---------|-----------|-------------------|
| `dcf`   | The 17-byte `DeModFrame` codec (`Encode`/`Decode`/`Crc16CCITT`/`Syndrome`). | `Documentation/golden_vectors.json` (246 vectors) |
| `game`  | DCF-Game L2 adapter over **DATA** frames (11/5 packet/frag split); `Packetize` + `GameReassembler` + SNAPSHOT/INPUT/JOIN bodies. | `Documentation/game_vectors.json` |
| `audio` | DCF-Audio L2 adapter over **CTRL** frames (11/5 split); `Packetize` + `AudioReassembler` + PCM-diag + PM params. | `Documentation/audio_vectors.json`, `pm_param_vectors.json` |
| `text`  | DCF-Text L2 adapter over **DATA** frames (10/6 split, `[len_hi,len_lo,flags,0]` descriptor); `Packetize` + `TextReassembler` + `ChannelID`. | `Documentation/text_vectors.json` |
| `node`  | UDP `DcfNode`: `ProtoMessage` transport, peer table + per-peer RTT, PING/PONG, reliable-ARQ, `MessageHandler`, and the `Send*DCF`/`Reassemble*` adapter hookups. | two-node loopback + `ProtoMessage` byte-vector |

Each adapter package self-certifies its framing anchor in `init()` (it panics on import if the
bytes diverge from the reference), mirroring `dcf/frame.go`.

## Test / certify

```sh
cd go
go test ./...            # dcf (246) + game + audio + text adapters + node (proto + loopback)
go test -race ./node/    # concurrency check on the receiver / ping / ARQ goroutines
go vet ./... && go build ./...
```

CI runs all of the above in the `certify-go` job of `.github/workflows/wire-certify.yml`.

## Quickstart — a real node

```go
package main

import (
    "log"
    "net"
    "time"

    "github.com/ALH477/Punctim/go/node"
    "github.com/ALH477/Punctim/go/text"
)

// Embed DefaultMessageHandler; override only the arms you care about.
type app struct {
    node.DefaultMessageHandler
    n     *node.DcfNode
    reasm *text.TextReassembler
}

func (a *app) HandleText(payload []byte, from *net.UDPAddr) {
    if pkt := a.n.ReassembleTextPayload(a.reasm, payload); pkt != nil {
        log.Printf("text from %s on ch %d: %q", from, pkt.Dst, pkt.Text)
    }
}

func main() {
    cfg := node.DefaultConfig() // UDP, p2p, 0.0.0.0:7777
    n, err := node.New(&cfg)
    if err != nil {
        log.Fatal(err)
    }
    if err := n.Start(&app{n: n, reasm: text.NewTextReassembler()}); err != nil {
        log.Fatal(err) // launches the receiver + ping scheduler + ARQ goroutines
    }
    defer n.Stop()

    n.AddPeer("peer1", "192.168.1.50", 7777)
    ch := text.ChannelID("lobby") // crc16 of the channel name (0xFFFF = broadcast)
    // One message -> 1 + ceil(len/4) certified DeModFrame DATA frames over UDP.
    n.SendTextDCF([]byte("hello over DeModFrame"), 1, uint32(time.Now().UnixMicro()), 1, ch, 0, true)
    time.Sleep(2 * time.Second)
}
```

Game and audio work the same way: `SendGameDCF` (DATA frames, opponents multiplexed by frame
`src`) and `SendAudioDCF` (CTRL frames), reassembled per-source with `ReassembleGamePayload` /
`ReassembleAudioPayload` against a `game.GameReassembler` / `audio.AudioReassembler`.

## Scope

This SDK implements the **working subset** of the Rust reference (`rust/src/lib.rs`) — enough to
run real DCF peers and exchange certified frames. Intentionally **not** implemented (aspirational
in the wider repo, not working code): gRPC, master/AUTO orchestration, Dijkstra routing, and mDNS
discovery. `ProtoMessage` is byte-identical to the Rust transport envelope but is not yet covered
by cross-language golden vectors (only a hand-computed Go byte-vector + Go↔Go loopback); a shared
`proto_vectors.json` is the recommended follow-up. `node.TEXT_DCF` (msg type 10) is a Go addition
ahead of the Rust SDK, which wires `GAME_DCF`/audio but not text.

## License

LGPL-3.0-only (see repository `LICENSE`/`LICENSING.md`). Every source file carries an SPDX header.
