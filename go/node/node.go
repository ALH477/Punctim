// SPDX-License-Identifier: LGPL-3.0-only

package node

import (
	"errors"
	"fmt"
	"math"
	"math/rand"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ALH477/Punctim/go/audio"
	"github.com/ALH477/Punctim/go/game"
	"github.com/ALH477/Punctim/go/text"
)

// peerAddr is a peer's host/port plus a best-effort resolved IP (for addr matching
// when peers share a port, e.g. :7777 across Docker containers).
type peerAddr struct {
	host string
	port uint16
	ip   string // resolved IP of host (empty if unresolved; lazily filled)
}

// ErrNotInitialized is returned when the UDP endpoint is absent (non-UDP transport).
var ErrNotInitialized = errors.New("node: UDP endpoint not initialized")

// DcfNode is a stdlib-only UDP node mirroring the working subset of the Rust DcfNode. It owns
// a UdpEndpoint, a peer table with per-peer RTT stats, a sequence counter, and three
// background goroutines (receiver, ping scheduler, reliable handler) started by Start and
// joined by Stop.
type DcfNode struct {
	config DcfConfig
	nodeID string
	ep     *UdpEndpoint

	mu        sync.RWMutex
	peerOrder []string                 // insertion order, mirrors Rust's peers Vec
	peers     map[string]peerAddr      // peer_id -> addr
	peerStats map[string]*NetworkStats // peer_id -> per-peer RTT/jitter

	mesh *MeshRuntime // optional self-healing runtime (nil unless EnableMesh)

	seq     atomic.Uint32
	running atomic.Bool
	stop    chan struct{}
	wg      sync.WaitGroup
}

// New builds a DcfNode from cfg (nil => DefaultConfig). For a UDP transport it binds the
// endpoint immediately (so an ephemeral port is resolvable via LocalPort before Start).
func New(cfg *DcfConfig) (*DcfNode, error) {
	c := DefaultConfig()
	if cfg != nil {
		c = *cfg
	}
	nodeID := c.NodeID
	if nodeID == "" {
		nodeID = fmt.Sprintf("node-%d", rand.Intn(10000))
	}

	n := &DcfNode{
		config:    c,
		nodeID:    nodeID,
		peers:     make(map[string]peerAddr),
		peerStats: make(map[string]*NetworkStats),
		stop:      make(chan struct{}),
	}

	if c.Transport == "UDP" || c.Transport == "udp" {
		ep, err := NewUdpEndpoint(c.Host, c.UDPPort)
		if err != nil {
			return nil, err
		}
		n.ep = ep
	}
	return n, nil
}

// NodeID returns this node's identifier.
func (n *DcfNode) NodeID() string { return n.nodeID }

// Config returns a copy of the node configuration.
func (n *DcfNode) Config() DcfConfig { return n.config }

// LocalPort returns the actual bound UDP port (resolved if 0 was requested), or 0 for a
// non-UDP node.
func (n *DcfNode) LocalPort() uint16 {
	if n.ep == nil {
		return 0
	}
	return n.ep.Port()
}

// nextSeq returns the next monotonically increasing sequence number.
func (n *DcfNode) nextSeq() uint32 { return n.seq.Add(1) - 1 }

// ── Peer management (mirrors the Rust peer API) ─────────────────────────────

// AddPeer registers (or updates) a peer's address.
func (n *DcfNode) AddPeer(id, host string, port uint16) {
	ip := ""
	if addrs, err := net.LookupHost(host); err == nil && len(addrs) > 0 {
		ip = addrs[0]
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	if _, ok := n.peers[id]; !ok {
		n.peerOrder = append(n.peerOrder, id)
	}
	n.peers[id] = peerAddr{host: host, port: port, ip: ip}
}

// RemovePeer drops a peer and its stats.
func (n *DcfNode) RemovePeer(id string) {
	n.mu.Lock()
	defer n.mu.Unlock()
	delete(n.peers, id)
	delete(n.peerStats, id)
	for i, p := range n.peerOrder {
		if p == id {
			n.peerOrder = append(n.peerOrder[:i], n.peerOrder[i+1:]...)
			break
		}
	}
}

// ListPeers returns the peer ids in insertion order.
func (n *DcfNode) ListPeers() []string {
	n.mu.RLock()
	defer n.mu.RUnlock()
	out := make([]string, len(n.peerOrder))
	copy(out, n.peerOrder)
	return out
}

// ListPeersDetailed returns each peer with its address and latest per-peer RTT/jitter.
func (n *DcfNode) ListPeersDetailed() []PeerDetail {
	n.mu.RLock()
	defer n.mu.RUnlock()
	out := make([]PeerDetail, 0, len(n.peerOrder))
	for _, id := range n.peerOrder {
		addr := n.peers[id]
		var st NetworkStats
		if s, ok := n.peerStats[id]; ok {
			st = *s
		}
		out = append(out, PeerDetail{ID: id, Host: addr.host, Port: addr.port, Stats: st})
	}
	return out
}

// peerInfo returns a peer's address.
func (n *DcfNode) peerInfo(id string) (peerAddr, bool) {
	n.mu.RLock()
	defer n.mu.RUnlock()
	a, ok := n.peers[id]
	return a, ok
}

// recordPeerRTT resolves the peer for a source address (matched by port, then refined by ip)
// and folds an RTT sample into its per-peer stats. Called from the PONG path. Mirrors the
// Rust record_peer_rtt.
func (n *DcfNode) recordPeerRTT(from *net.UDPAddr, rttMs float64) {
	n.mu.Lock()
	defer n.mu.Unlock()
	match := n.matchPeerLocked(from)
	if match == "" {
		return
	}
	st, ok := n.peerStats[match]
	if !ok {
		st = &NetworkStats{}
		n.peerStats[match] = st
	}
	st.UpdateRTT(rttMs)
}

// matchPeerLocked resolves the peer id for a source address by IP+port (peers may share a
// port, so port-only would collide), falling back to port-only as a last resort. The caller
// must hold n.mu (read or write).
func (n *DcfNode) matchPeerLocked(from *net.UDPAddr) string {
	fromIP := from.IP.String()
	for _, id := range n.peerOrder {
		a := n.peers[id]
		if int(a.port) != from.Port {
			continue
		}
		if a.ip == fromIP || a.host == fromIP || a.host == "localhost" || a.host == "127.0.0.1" || a.host == "0.0.0.0" {
			return id
		}
	}
	for _, id := range n.peerOrder {
		if int(n.peers[id].port) == from.Port {
			return id
		}
	}
	return ""
}

// peerIDByAddr resolves a peer id from a source address (used by the mesh runtime's PONG hook).
func (n *DcfNode) peerIDByAddr(from *net.UDPAddr) string {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return n.matchPeerLocked(from)
}

// refreshPeerIPs re-resolves any peer whose IP is still unknown (a DNS name not yet
// resolvable when AddPeer ran — e.g. a Docker peer that started slightly later). The mesh
// loop calls this each tick so addr matching becomes correct within a tick or two.
func (n *DcfNode) refreshPeerIPs() {
	n.mu.RLock()
	hosts := map[string]string{}
	for _, id := range n.peerOrder {
		if n.peers[id].ip == "" {
			hosts[id] = n.peers[id].host
		}
	}
	n.mu.RUnlock()
	if len(hosts) == 0 {
		return
	}
	resolved := map[string]string{}
	for id, h := range hosts {
		if a, err := net.LookupHost(h); err == nil && len(a) > 0 {
			resolved[id] = a[0]
		}
	}
	if len(resolved) == 0 {
		return
	}
	n.mu.Lock()
	for id, ip := range resolved {
		if p, ok := n.peers[id]; ok {
			p.ip = ip
			n.peers[id] = p
		}
	}
	n.mu.Unlock()
}

// peerRTTms returns a peer's smoothed RTT in whole milliseconds (0 if no sample yet).
func (n *DcfNode) peerRTTms(id string) int {
	n.mu.RLock()
	defer n.mu.RUnlock()
	if st := n.peerStats[id]; st != nil && st.AvgRTT > 0 {
		return int(st.AvgRTT + 0.5)
	}
	return 0
}

// EnableMesh attaches a self-healing runtime; Start then runs its loop. Mesh() returns it.
func (n *DcfNode) EnableMesh(m *MeshRuntime) { n.mesh = m }
func (n *DcfNode) Mesh() *MeshRuntime        { return n.mesh }

// ── Plain send API (mirrors the Rust gaming API) ────────────────────────────

// SendPosition broadcasts an unreliable position update to every peer.
func (n *DcfNode) SendPosition(x, y, z float32) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	msg := NewProtoMessage(MsgPosition, n.nextSeq(), Position{X: x, Y: y, Z: z}.Encode())
	return n.broadcast(msg, false)
}

// SendAudio broadcasts a raw (already-encoded) audio payload as an unreliable MsgAudio to
// every peer.
func (n *DcfNode) SendAudio(data []byte) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	msg := NewProtoMessage(MsgAudio, n.nextSeq(), data)
	return n.broadcast(msg, false)
}

// SendPing sends an unreliable MsgPing to one peer (the PONG it elicits drives RTT).
func (n *DcfNode) SendPing(peerID string) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	a, ok := n.peerInfo(peerID)
	if !ok {
		return fmt.Errorf("node: peer not found: %s", peerID)
	}
	msg := NewProtoMessage(MsgPing, n.nextSeq(), nil)
	return n.ep.SendMessage(msg, a.host, a.port, false)
}

// BenchmarkResult is the outcome of a Benchmark run (RTTs in milliseconds).
type BenchmarkResult struct {
	Count  int
	AvgRTT float64
	MinRTT float64
	MaxRTT float64
}

// Benchmark sends count pings to peerID, sampling local round-trip wall time between each
// send and a short settle, mirroring the Rust benchmark's coarse local timing.
func (n *DcfNode) Benchmark(peerID string, count int) (BenchmarkResult, error) {
	if n.ep == nil {
		return BenchmarkResult{}, ErrNotInitialized
	}
	if _, ok := n.peerInfo(peerID); !ok {
		return BenchmarkResult{}, fmt.Errorf("node: peer not found: %s", peerID)
	}
	rtts := make([]float64, 0, count)
	for i := 0; i < count; i++ {
		start := time.Now()
		if err := n.SendPing(peerID); err != nil {
			return BenchmarkResult{}, err
		}
		time.Sleep(10 * time.Millisecond)
		rtts = append(rtts, float64(time.Since(start).Microseconds())/1000.0)
	}
	res := BenchmarkResult{Count: len(rtts), MinRTT: math.Inf(1)}
	if len(rtts) == 0 {
		res.MinRTT = 0
		return res, nil
	}
	var sum float64
	for _, r := range rtts {
		sum += r
		if r < res.MinRTT {
			res.MinRTT = r
		}
		if r > res.MaxRTT {
			res.MaxRTT = r
		}
	}
	res.AvgRTT = sum / float64(len(rtts))
	return res, nil
}

// broadcast sends one ProtoMessage to every peer (used for fan-out sends).
func (n *DcfNode) broadcast(msg *ProtoMessage, reliable bool) error {
	for _, id := range n.ListPeers() {
		a, ok := n.peerInfo(id)
		if !ok {
			continue
		}
		if err := n.ep.SendMessage(msg, a.host, a.port, reliable); err != nil {
			return err
		}
	}
	return nil
}

// ── DCF adapter hookups (the Phase A packages over the wire) ────────────────

// SendGameDCF packetizes a game body with the certified DCF-Game L2 framing and ships each
// 17-byte DeModFrame to every peer as a MsgGameDCF ProtoMessage. The descriptor flags carry
// RELIABLE|ORDERED when reliable, and reliable also routes each frame through the SDK ARQ.
// Mirrors the Rust send_game_dcf.
func (n *DcfNode) SendGameDCF(msgTypeID uint8, body []byte, packetID uint16, tsUs uint32, src, channel uint16, reliable bool) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	var flags uint8
	if reliable {
		flags = game.FlagReliable | game.FlagOrdered
	}
	frames, err := game.Packetize(msgTypeID, body, packetID, tsUs, src, channel, flags)
	if err != nil {
		return fmt.Errorf("node: game packetize failed: %w", err)
	}
	for _, id := range n.ListPeers() {
		a, ok := n.peerInfo(id)
		if !ok {
			continue
		}
		for i := range frames {
			f := frames[i]
			msg := NewProtoMessage(MsgGameDCF, n.nextSeq(), f[:])
			if err := n.ep.SendMessage(msg, a.host, a.port, reliable); err != nil {
				return err
			}
		}
	}
	return nil
}

// SendAudioDCF packetizes an encoded audio block with the certified DCF-Audio L2 framing and
// ships each 17-byte DeModFrame to every peer as an unreliable MsgAudio ProtoMessage (src=0,
// as in the Rust send_audio_dcf).
func (n *DcfNode) SendAudioDCF(codecID uint8, encoded []byte, packetID uint16, tsUs uint32, channel uint16) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	frames, err := audio.Packetize(codecID, encoded, packetID, tsUs, 0, channel, 0)
	if err != nil {
		return fmt.Errorf("node: audio packetize failed: %w", err)
	}
	for _, id := range n.ListPeers() {
		a, ok := n.peerInfo(id)
		if !ok {
			continue
		}
		for i := range frames {
			f := frames[i]
			msg := NewProtoMessage(MsgAudio, n.nextSeq(), f[:])
			if err := n.ep.SendMessage(msg, a.host, a.port, false); err != nil {
				return err
			}
		}
	}
	return nil
}

// SendTextDCF packetizes a UTF-8 message with the certified DCF-Text L2 framing and ships
// each 17-byte DeModFrame to every peer as a MsgTextDCF ProtoMessage. Go extension — not yet
// in the Rust SDK; back-port for parity.
func (n *DcfNode) SendTextDCF(textBytes []byte, packetID uint16, tsUs uint32, src, channel uint16, flags uint8, reliable bool) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	frames, err := text.Packetize(textBytes, packetID, tsUs, src, channel, flags)
	if err != nil {
		return fmt.Errorf("node: text packetize failed: %w", err)
	}
	for _, id := range n.ListPeers() {
		a, ok := n.peerInfo(id)
		if !ok {
			continue
		}
		for i := range frames {
			f := frames[i]
			msg := NewProtoMessage(MsgTextDCF, n.nextSeq(), f[:])
			if err := n.ep.SendMessage(msg, a.host, a.port, reliable); err != nil {
				return err
			}
		}
	}
	return nil
}

// ReassembleGamePayload feeds one received GAME_DCF payload (a single 17-byte DeModFrame)
// into r, returning a completed packet once the whole message has arrived. Mirrors the Rust
// reassemble_game_payload.
func (n *DcfNode) ReassembleGamePayload(r *game.GameReassembler, payload []byte) *game.GamePacket {
	if len(payload) != 17 {
		return nil
	}
	var arr [17]byte
	copy(arr[:], payload)
	return r.Push(&arr)
}

// ReassembleAudioPayload feeds one received AUDIO payload (a single 17-byte DeModFrame) into
// r, returning a completed block once it has fully arrived. Mirrors reassemble_audio_payload.
func (n *DcfNode) ReassembleAudioPayload(r *audio.AudioReassembler, payload []byte) *audio.AudioPacket {
	if len(payload) != 17 {
		return nil
	}
	var arr [17]byte
	copy(arr[:], payload)
	return r.Push(&arr)
}

// ReassembleTextPayload feeds one received TEXT_DCF payload (a single 17-byte DeModFrame)
// into r, returning a completed message once it has fully arrived.
func (n *DcfNode) ReassembleTextPayload(r *text.TextReassembler, payload []byte) *text.TextPacket {
	if len(payload) != 17 {
		return nil
	}
	var arr [17]byte
	copy(arr[:], payload)
	return r.Push(&arr)
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

// Start marks the node running and launches the receiver, ping scheduler, and reliable
// handler goroutines. It is a no-op (returns ErrNotInitialized) for a non-UDP node.
func (n *DcfNode) Start(h MessageHandler) error {
	if n.ep == nil {
		return ErrNotInitialized
	}
	n.running.Store(true)

	n.wg.Add(1)
	go func() {
		defer n.wg.Done()
		RunUDPReceiver(n, h)
	}()

	n.wg.Add(1)
	go func() {
		defer n.wg.Done()
		RunPingScheduler(n, 1*time.Second)
	}()

	n.wg.Add(1)
	go func() {
		defer n.wg.Done()
		RunReliableHandler(n)
	}()

	if n.mesh != nil {
		n.wg.Add(1)
		go func() {
			defer n.wg.Done()
			n.mesh.run(n)
		}()
	}

	return nil
}

// Stop signals the goroutines to exit, waits for them, and closes the socket. Safe to call
// once; the stop channel is closed exactly once.
func (n *DcfNode) Stop() error {
	if !n.running.Swap(false) {
		return nil
	}
	close(n.stop)
	n.wg.Wait()
	if n.ep != nil {
		return n.ep.Close()
	}
	return nil
}

// IsRunning reports whether the node is running.
func (n *DcfNode) IsRunning() bool { return n.running.Load() }
