// SPDX-License-Identifier: LGPL-3.0-only

package node

import (
	"net"
	"sync"
	"testing"
	"time"

	"github.com/ALH477/Punctim/go/game"
	"github.com/ALH477/Punctim/go/text"
)

// eventually polls cond every step until it is true or the deadline elapses, failing the test
// on timeout. No fixed sleeps — the test makes progress as soon as the condition holds.
func eventually(t *testing.T, total, step time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(total)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(step)
	}
	if !cond() {
		t.Fatalf("condition not met within %s", total)
	}
}

// capturingHandler records reassembled game and text packets via per-source reassemblers,
// guarded by a mutex so the receiver goroutine and the test can both touch it.
type capturingHandler struct {
	DefaultMessageHandler
	node *DcfNode

	mu        sync.Mutex
	gameReasm map[uint16]*game.GameReassembler
	textReasm map[uint16]*text.TextReassembler
	games     []*game.GamePacket
	texts     []*text.TextPacket
}

func newCapturingHandler(n *DcfNode) *capturingHandler {
	return &capturingHandler{
		node:      n,
		gameReasm: make(map[uint16]*game.GameReassembler),
		textReasm: make(map[uint16]*text.TextReassembler),
	}
}

func (h *capturingHandler) HandleGame(payload []byte, from *net.UDPAddr) {
	if len(payload) != 17 {
		return
	}
	// Key the reassembler by frame src so concurrent senders don't collide.
	src := uint16(payload[4])<<8 | uint16(payload[5])
	h.mu.Lock()
	defer h.mu.Unlock()
	r, ok := h.gameReasm[src]
	if !ok {
		r = game.NewGameReassembler()
		h.gameReasm[src] = r
	}
	if pkt := h.node.ReassembleGamePayload(r, payload); pkt != nil {
		h.games = append(h.games, pkt)
	}
}

func (h *capturingHandler) HandleText(payload []byte, from *net.UDPAddr) {
	if len(payload) != 17 {
		return
	}
	src := uint16(payload[4])<<8 | uint16(payload[5])
	h.mu.Lock()
	defer h.mu.Unlock()
	r, ok := h.textReasm[src]
	if !ok {
		r = text.NewTextReassembler()
		h.textReasm[src] = r
	}
	if pkt := h.node.ReassembleTextPayload(r, payload); pkt != nil {
		h.texts = append(h.texts, pkt)
	}
}

func (h *capturingHandler) gamePackets() []*game.GamePacket {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]*game.GamePacket, len(h.games))
	copy(out, h.games)
	return out
}

func (h *capturingHandler) textPackets() []*text.TextPacket {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]*text.TextPacket, len(h.texts))
	copy(out, h.texts)
	return out
}

// newLoopbackNode binds an ephemeral UDP port on 127.0.0.1.
func newLoopbackNode(t *testing.T, id string) *DcfNode {
	t.Helper()
	cfg := DefaultConfig()
	cfg.Host = "127.0.0.1"
	cfg.UDPPort = 0
	cfg.NodeID = id
	n, err := New(&cfg)
	if err != nil {
		t.Fatalf("New(%s): %v", id, err)
	}
	return n
}

// TestLoopbackGameTextAndRTT exercises the full UDP path between two nodes: a DCF-Game
// snapshot, a DCF-Text message, and PING/PONG-driven per-peer RTT.
func TestLoopbackGameTextAndRTT(t *testing.T) {
	a := newLoopbackNode(t, "A")
	b := newLoopbackNode(t, "B")

	ha := newCapturingHandler(a)
	hb := newCapturingHandler(b)

	if err := a.Start(ha); err != nil {
		t.Fatalf("A.Start: %v", err)
	}
	defer a.Stop()
	if err := b.Start(hb); err != nil {
		t.Fatalf("B.Start: %v", err)
	}
	defer b.Stop()

	// Cross-register peers on their actual ephemeral ports.
	a.AddPeer("B", "127.0.0.1", b.LocalPort())
	b.AddPeer("A", "127.0.0.1", a.LocalPort())

	// (a) Game: A sends a SNAPSHOT body; B reassembles it back to the same bytes.
	snap := game.Snapshot{X: 1.5, Y: -2.25, Z: 3.0, Vx: 0.5, Vy: 0, Vz: -1.0, Yaw: 0x1234}.Pack()
	snapBytes := snap[:]
	if err := a.SendGameDCF(game.MsgSnapshot, snapBytes, 1, 1000, 7, text.Broadcast, false); err != nil {
		t.Fatalf("A.SendGameDCF: %v", err)
	}
	eventually(t, 2*time.Second, 10*time.Millisecond, func() bool {
		for _, p := range hb.gamePackets() {
			if p.MsgTypeID == game.MsgSnapshot && bytesEqual(p.Payload, snapBytes) {
				return true
			}
		}
		return false
	})

	// (b) Text: A sends a string; B reassembles it back to the same string.
	const greeting = "hello over DeModFrame"
	if err := a.SendTextDCF([]byte(greeting), 2, 2000, 7, text.ChannelID("duet"), text.FlagAgent, false); err != nil {
		t.Fatalf("A.SendTextDCF: %v", err)
	}
	eventually(t, 2*time.Second, 10*time.Millisecond, func() bool {
		for _, p := range hb.textPackets() {
			if p.Text == greeting {
				return true
			}
		}
		return false
	})

	// (c) RTT: the ping scheduler drives PING->PONG->recordPeerRTT; B's view of A warms up.
	eventually(t, 2*time.Second, 10*time.Millisecond, func() bool {
		for _, d := range b.ListPeersDetailed() {
			if d.ID == "A" && d.Stats.AvgRTT > 0 {
				return true
			}
		}
		return false
	})
}

func bytesEqual(a, b []byte) bool {
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
