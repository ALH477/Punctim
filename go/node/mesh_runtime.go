// SPDX-License-Identifier: LGPL-3.0-only

package node

// MeshRuntime drives the certified self-healing algorithms (github.com/ALH477/Punctim/go/mesh)
// from live PING/PONG and the DCF-Mesh control adapter (MsgMesh). Each tick it folds per-peer
// PONG/timeout into the liveness FSM (status Healthy/Degraded/Unreachable), and — by mode —
// runs the AUTO/master loop: AUTO nodes REPORT to the master, the master elects + broadcasts
// ROLE. If a node's master goes Unreachable it locally re-elects (decentralized failover). The
// runtime logs a one-line mesh status periodically and on role changes (the demo asserts on it).

import (
	"log"
	"net"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/ALH477/Punctim/go/mesh"
)

const (
	meshWindowCap = 5 // FSM health-window length
	meshFailThr   = 3 // consecutive timeouts -> Unreachable
	meshOkThr     = 2 // consecutive oks -> recover
)

type peerHealth struct {
	answered bool
	window   []byte
	status   int
}

// MeshRuntime is the per-node self-healing state machine.
type MeshRuntime struct {
	mu         sync.Mutex
	mode       string // "p2p" | "auto" | "master"
	nodeID     uint16
	role       int
	master     uint16
	masterPeer string // peer id to REPORT to (auto mode); "" if none
	groupThr   int
	interval   time.Duration
	health     map[string]*peerHealth   // peer id (numeric string) -> health
	reports    map[uint16][][3]int      // master side: reporter node id -> its peers
	reportAddr map[uint16]*net.UDPAddr  // master side: reporter -> source addr (for ROLE)
}

// NewMeshRuntime builds a runtime. masterPeer is the peer id an AUTO node reports to.
func NewMeshRuntime(mode string, nodeID uint16, masterPeer string, groupThr int) *MeshRuntime {
	r := &MeshRuntime{
		mode: mode, nodeID: nodeID, masterPeer: masterPeer, groupThr: groupThr,
		role: mesh.Leaf, interval: time.Second,
		health:     map[string]*peerHealth{},
		reports:    map[uint16][][3]int{},
		reportAddr: map[uint16]*net.UDPAddr{},
	}
	if mode == "master" {
		r.role = mesh.Master
		r.master = nodeID
	}
	return r
}

// Role / Master expose the current assignment (used by mesh-status).
func (m *MeshRuntime) Role() int     { m.mu.Lock(); defer m.mu.Unlock(); return m.role }
func (m *MeshRuntime) Master() uint16 { m.mu.Lock(); defer m.mu.Unlock(); return m.master }

func (m *MeshRuntime) markAnswered(peerID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.peerLocked(peerID).answered = true
}

func (m *MeshRuntime) peerLocked(id string) *peerHealth {
	h := m.health[id]
	if h == nil {
		h = &peerHealth{status: mesh.Healthy}
		m.health[id] = h
	}
	return h
}

// healthTick folds each peer's answered/timeout into the FSM (call once per interval).
func (m *MeshRuntime) healthTick(peerIDs []string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, id := range peerIDs {
		h := m.peerLocked(id)
		ev := byte(0)
		if h.answered {
			ev = 1
		}
		h.answered = false
		h.window = append(h.window, ev)
		if len(h.window) > meshWindowCap {
			h.window = h.window[len(h.window)-meshWindowCap:]
		}
		h.status = mesh.PeerStatus(h.window, meshFailThr, meshOkThr)
	}
}

func (m *MeshRuntime) statusOf(peerID string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	if h := m.health[peerID]; h != nil {
		return h.status
	}
	return mesh.Healthy
}

func statusName(s int) string {
	switch s {
	case mesh.Healthy:
		return "healthy"
	case mesh.Degraded:
		return "degraded"
	default:
		return "unreachable"
	}
}

func roleName(r int) string {
	switch r {
	case mesh.Master:
		return "master"
	case mesh.Relay:
		return "relay"
	default:
		return "leaf"
	}
}

// handleControl processes an inbound MsgMesh payload (REPORT to a master, ROLE to a node).
func (m *MeshRuntime) handleControl(payload []byte, from *net.UDPAddr) {
	switch mesh.MeshMsgType(payload) {
	case mesh.MeshReport:
		nid, peers, ok := mesh.UnpackReport(payload)
		if !ok {
			return
		}
		m.mu.Lock()
		m.reports[uint16(nid)] = peers
		m.reportAddr[uint16(nid)] = from
		m.mu.Unlock()
	case mesh.MeshRole:
		nid, role, master, ok := mesh.UnpackRole(payload)
		if !ok || uint16(nid) != m.nodeID {
			return
		}
		m.mu.Lock()
		changed := m.role != role || m.master != uint16(master)
		m.role, m.master = role, uint16(master)
		m.mu.Unlock()
		if changed {
			log.Printf("mesh: role assigned -> %s (master %d)", roleName(role), master)
		}
	}
}

// sendReport (AUTO): report this node's healthy/degraded peer view to the master.
func (m *MeshRuntime) sendReport(n *DcfNode, peerIDs []string) {
	if m.masterPeer == "" {
		return
	}
	var peers [][3]int
	for _, id := range peerIDs {
		pid, err := strconv.Atoi(id)
		if err != nil {
			continue
		}
		peers = append(peers, [3]int{pid, m.statusOf(id), n.peerRTTms(id)})
	}
	payload := mesh.PackReport(int(m.nodeID), peers)
	a, ok := n.peerInfo(m.masterPeer)
	if !ok {
		return
	}
	_ = n.ep.SendMessage(NewProtoMessage(MsgMesh, n.nextSeq(), payload), a.host, a.port, false)
}

// runElection (MASTER): assemble the reported topology, elect, and broadcast ROLE.
func (m *MeshRuntime) runElection(n *DcfNode) {
	m.mu.Lock()
	// node id set: self + reporters + their peers. Edges are canonicalised (a<b) and
	// deduplicated so a bidirectional link reported by both ends counts once.
	idset := map[uint16]bool{m.nodeID: true}
	type edgeKey struct{ a, b uint16 }
	dedup := map[edgeKey]int{} // canonical edge -> weight (min RTT seen)
	for reporter, peers := range m.reports {
		idset[reporter] = true
		for _, p := range peers {
			peerID := uint16(p[0])
			idset[peerID] = true
			if p[1] == mesh.Unreachable { // skip dead links
				continue
			}
			a, b := reporter, peerID
			if a > b {
				a, b = b, a
			}
			k := edgeKey{a, b}
			if w, ok := dedup[k]; !ok || p[2] < w {
				dedup[k] = p[2]
			}
		}
	}
	addrs := m.reportAddr
	m.mu.Unlock()

	// map node ids -> 0..N-1 (sorted for determinism)
	ids := make([]int, 0, len(idset))
	for id := range idset {
		ids = append(ids, int(id))
	}
	sort.Ints(ids)
	index := map[uint16]int{}
	for i, id := range ids {
		index[uint16(id)] = i
	}
	var edges [][3]int
	for k, w := range dedup {
		edges = append(edges, [3]int{index[k.a], index[k.b], w})
	}
	masterIdx, roles := mesh.Elect(len(ids), edges, 2)
	masterID := uint16(ids[masterIdx])

	// adopt own role + master
	m.mu.Lock()
	if self, ok := index[m.nodeID]; ok {
		m.role = roles[self]
	}
	m.master = masterID
	m.mu.Unlock()

	// broadcast ROLE to each reporter
	for reporter, addr := range addrs {
		idx, ok := index[reporter]
		if !ok {
			continue
		}
		payload := mesh.PackRole(int(reporter), roles[idx], int(masterID))
		_ = n.ep.SendMessage(NewProtoMessage(MsgMesh, n.nextSeq(), payload), addr.IP.String(), uint16(addr.Port), false)
	}
}

// checkMasterFailover (AUTO): if the master peer is Unreachable, locally re-elect the lowest
// node id among self + healthy peers (decentralized failover, no central authority).
func (m *MeshRuntime) checkMasterFailover(n *DcfNode, peerIDs []string) {
	if m.masterPeer == "" || m.statusOf(m.masterPeer) != mesh.Unreachable {
		return
	}
	best := int(m.nodeID)
	for _, id := range peerIDs {
		if m.statusOf(id) == mesh.Unreachable {
			continue
		}
		if pid, err := strconv.Atoi(id); err == nil && pid < best {
			best = pid
		}
	}
	m.mu.Lock()
	changed := m.master != uint16(best)
	prev := m.master
	m.master = uint16(best)
	if best == int(m.nodeID) {
		m.role = mesh.Master
	}
	m.mu.Unlock()
	if changed {
		log.Printf("mesh: master %d unreachable -> local re-election, new master %d", prev, best)
	}
}

func (m *MeshRuntime) logStatus(n *DcfNode, peerIDs []string) {
	parts := make([]string, 0, len(peerIDs))
	for _, id := range peerIDs {
		st := m.statusOf(id)
		grp := mesh.GroupOf(n.peerRTTms(id), m.groupThr, st)
		parts = append(parts, id+":"+statusName(st)+":g"+strconv.Itoa(grp))
	}
	m.mu.Lock()
	role, master := m.role, m.master
	m.mu.Unlock()
	log.Printf("mesh-status node=%d mode=%s role=%s master=%d peers=[%v]",
		m.nodeID, m.mode, roleName(role), master, parts)
}

// run is the mesh loop (launched by Start when a runtime is attached).
func (m *MeshRuntime) run(n *DcfNode) {
	t := time.NewTicker(m.interval)
	defer t.Stop()
	tick := 0
	for {
		select {
		case <-n.stop:
			return
		case <-t.C:
			if !n.IsRunning() {
				return
			}
			n.refreshPeerIPs()
			peers := n.ListPeers()
			m.healthTick(peers)
			for _, id := range peers {
				_ = n.SendPing(id)
			}
			switch m.mode {
			case "auto":
				m.sendReport(n, peers)
				m.checkMasterFailover(n, peers)
			case "master":
				m.runElection(n)
			}
			tick++
			if tick%3 == 0 {
				m.logStatus(n, peers)
			}
		}
	}
}
