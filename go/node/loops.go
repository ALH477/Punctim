// SPDX-License-Identifier: LGPL-3.0-only

package node

import (
	"errors"
	"fmt"
	"net"
	"time"
)

// recvReadTimeout bounds each blocking read so the receiver periodically wakes to observe the
// stop signal for a clean shutdown.
const recvReadTimeout = 250 * time.Millisecond

// RunUDPReceiver reads datagrams until the node stops, deserialises each as a ProtoMessage,
// and dispatches by message type (mirrors the Rust run_udp_receiver + handle_incoming_message).
// SetReadDeadline makes the read wake periodically so a closed stop channel is observed.
func RunUDPReceiver(n *DcfNode, h MessageHandler) {
	buf := make([]byte, 65536)
	for n.IsRunning() {
		_ = n.ep.conn.SetReadDeadline(time.Now().Add(recvReadTimeout))
		size, from, err := n.ep.conn.ReadFromUDP(buf)
		if err != nil {
			var nerr net.Error
			if errors.As(err, &nerr) && nerr.Timeout() {
				select {
				case <-n.stop:
					return
				default:
					continue
				}
			}
			// Socket closed during shutdown, or a transient error: re-check running.
			select {
			case <-n.stop:
				return
			default:
				continue
			}
		}
		if size == 0 {
			continue
		}
		msg, derr := Deserialize(buf[:size])
		if derr != nil {
			continue
		}
		n.dispatch(h, &msg, from)
	}
}

// dispatch routes one decoded ProtoMessage exactly like the Rust handle_incoming_message.
func (n *DcfNode) dispatch(h MessageHandler, msg *ProtoMessage, from *net.UDPAddr) {
	n.ep.addRecvStats(len(msg.Payload))

	switch msg.MsgType {
	case MsgAck:
		if len(msg.Payload) >= 4 {
			seq := uint32(msg.Payload[0])<<24 | uint32(msg.Payload[1])<<16 | uint32(msg.Payload[2])<<8 | uint32(msg.Payload[3])
			n.ep.markAcked(seq)
		}
	case MsgPing:
		_ = n.ep.SendPong(msg.Sequence, msg.Timestamp, from)
	case MsgPong:
		nowMicros := uint64(time.Now().UnixMicro())
		var rtt float64
		if nowMicros > msg.Timestamp {
			rtt = float64(nowMicros-msg.Timestamp) / 1000.0
		}
		n.ep.recordRTT(rtt)
		n.recordPeerRTT(from, rtt)
		if n.mesh != nil {
			if id := n.peerIDByAddr(from); id != "" {
				n.mesh.markAnswered(id)
			}
		}
	case MsgMesh:
		if n.mesh != nil {
			n.mesh.handleControl(msg.Payload, from)
		}
	case MsgReliable:
		_ = n.ep.SendACK(msg.Sequence, from)
		// Inner payload delivery would dispatch on an embedded type if used.
	case MsgPosition:
		if pos, err := DecodePosition(msg.Payload); err == nil {
			h.HandlePosition(pos, from)
		}
	case MsgAudio:
		h.HandleAudio(msg.Payload, from)
	case MsgGameEvent:
		if ev, err := DecodeGameEvent(msg.Payload); err == nil {
			_ = n.ep.SendACK(msg.Sequence, from)
			h.HandleGameEvent(ev, from)
		}
	case MsgGameDCF:
		h.HandleGame(msg.Payload, from)
	case MsgTextDCF:
		h.HandleText(msg.Payload, from)
	case MsgFrame:
		// The udp:dialect=proto medium: exactly one raw 17-byte DeModFrame.
		if len(msg.Payload) == 17 {
			h.HandleFrame(msg.Payload, from)
		}
	default:
		// Unknown message type: ignore.
	}
}

// RunPingScheduler pings every peer on each tick so per-peer RTT (ListPeersDetailed) stays
// fresh, exiting when the node stops (mirrors the Rust run_ping_scheduler).
func RunPingScheduler(n *DcfNode, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-n.stop:
			return
		case <-ticker.C:
			if !n.IsRunning() {
				return
			}
			for _, id := range n.ListPeers() {
				_ = n.SendPing(id)
			}
		}
	}
}

// reliableTick is how often the reliable handler scans for timed-out reliable packets.
const reliableTick = 100 * time.Millisecond

// RunReliableHandler retransmits unacked reliable packets after the reliable timeout, up to
// the retry limit, then drops them as lost (mirrors the Rust run_reliable_handler).
func RunReliableHandler(n *DcfNode) {
	ticker := time.NewTicker(reliableTick)
	defer ticker.Stop()
	timeout := time.Duration(n.config.UDPReliableTimeout) * time.Millisecond
	maxRetries := n.config.RetryMax

	for {
		select {
		case <-n.stop:
			return
		case <-ticker.C:
			if !n.IsRunning() {
				return
			}
			n.scanReliable(timeout, maxRetries)
		}
	}
}

// scanReliable performs one pass over in-flight reliable packets: clear ACKed ones, retry
// timed-out ones that have retries left (bump attempts, resend, count a retransmit), and drop
// those that have exhausted retries (count a loss).
func (n *DcfNode) scanReliable(timeout time.Duration, maxRetries uint32) {
	type retry struct {
		data []byte
		addr *net.UDPAddr
	}
	var retries []retry

	n.ep.relMu.Lock()
	for seq, pkt := range n.ep.reliable {
		if n.ep.acked[seq] {
			delete(n.ep.reliable, seq)
			continue
		}
		if time.Since(pkt.sentTime) > timeout {
			if pkt.attempts < maxRetries {
				pkt.attempts++
				pkt.sentTime = time.Now()
				addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", pkt.host, pkt.port))
				if err == nil {
					retries = append(retries, retry{data: pkt.msg.Serialize(), addr: addr})
				}
			} else {
				delete(n.ep.reliable, seq)
				n.ep.statsMu.Lock()
				n.ep.stats.PacketsLost++
				n.ep.statsMu.Unlock()
			}
		}
	}
	n.ep.relMu.Unlock()

	for _, r := range retries {
		_ = n.ep.SendRaw(r.data, r.addr)
		n.ep.statsMu.Lock()
		n.ep.stats.Retransmits++
		n.ep.statsMu.Unlock()
	}
}
