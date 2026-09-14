# DCF Rust SDK - Punctim Compatible

**Version 2.2.0** | Compatible with D-LISP Punctim v2.2.0

A production-ready Rust implementation of the DeMoD Communications Framework (DCF), optimized for gaming and real-time audio with UDP transport, binary Protobuf, unreliable/reliable channels, and network statistics.

## Features

- **UDP Transport**: Low-latency (<5ms) unreliable and reliable channels
- **Binary Protocol**: 10-100x faster than JSON serialization
- **Gaming Optimized**: Position updates (12 bytes), audio streaming, game events
- **Network Statistics**: RTT, jitter, packet loss tracking
- **gRPC Support**: Backward compatible with TCP/gRPC for reliable operations
- **Peer Management**: Dynamic peer discovery via mDNS
- **Transaction Support**: Begin/commit/rollback for batched operations
- **Lisp Interoperability**: Wire-compatible with Punctim Lisp SDK

## Quick Start

### Installation

```bash
cargo install dcf-rust-sdk
```

### Starting the Server

```bash
# Using default configuration
dcf start

# With custom config
dcf --config my_config.toml start
```

### Adding Peers

```bash
dcf add-peer --peer-id player2 --host 192.168.1.100 --port 7777
```

### Sending Position Updates

```bash
dcf send-position --player-id player1 --x 100.0 --y 50.0 --z 25.0
```

### Getting Metrics

```bash
dcf metrics
```

## Library Usage

```rust
use dcf_rust_sdk::{DcfConfig, DcfNode, Position};
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Create node with default config
    let config = DcfConfig::default();
    let node = Arc::new(DcfNode::new(config)?);
    
    // Start the node
    node.start()?;
    
    // Add a peer
    node.add_peer("player2", "192.168.1.100", 7777)?;
    
    // Send position update (unreliable, low latency)
    node.send_position("player1", 100.0, 50.0, 25.0)?;
    
    // Send game event (reliable, with acknowledgment)
    node.send_game_event(1, "SHOOT|player1|rifle")?;
    
    // Get metrics
    let metrics = node.get_full_metrics();
    println!("Positions sent: {}", metrics.gaming.positions_sent);
    println!("Avg RTT: {}ms", metrics.network.avg_rtt);
    
    // Stop the node
    node.stop()?;
    
    Ok(())
}
```

## Configuration

### TOML Format (Rust preferred)

```toml
transport = "UDP"
host = "0.0.0.0"
grpc_port = 50051
udp_port = 7777
mode = "p2p"
p2p_discovery = true
group_rtt_threshold = 50
```

### JSON Format (Lisp compatible)

```json
{
  "transport": "UDP",
  "host": "0.0.0.0",
  "port": 50051,
  "udp-port": 7777,
  "mode": "p2p",
  "group-rtt-threshold": 50
}
```

## Wire Protocol

The binary protocol is wire-compatible with the Lisp Punctim implementation:

### Message Header (17 bytes minimum)
```
| Type (1) | Sequence (4) | Timestamp (8) | PayloadLen (4) | Payload (N) |
```

All multi-byte values use **big-endian** encoding for cross-platform compatibility.

### Message Types

| Type | Value | Description | Reliable |
|------|-------|-------------|----------|
| POSITION | 1 | Player position (12 bytes: x,y,z float32) | No |
| AUDIO | 2 | Audio packet (raw bytes) | No |
| GAME_EVENT | 3 | Game event (type + data) | Yes |
| STATE_SYNC | 4 | Full state synchronization | Yes |
| RELIABLE | 5 | Generic reliable message | Yes |
| ACK | 6 | Acknowledgment | No |
| PING | 7 | RTT measurement | No |
| PONG | 8 | RTT response | No |

### Position Payload (12 bytes)
```
| X (float32) | Y (float32) | Z (float32) |
```

### Game Event Payload
```
| EventType (1) | Data (UTF-8 string) |
```

## Interoperability with Lisp

### Lisp Client → Rust Server

```lisp
;; Lisp: Send position to Rust server
(dcf-add-peer "rust-server" "192.168.1.50" 7777)
(dcf-send-position "player1" 100.0 50.0 25.0)
```

### Rust Client → Lisp Server

```rust
// Rust: Send position to Lisp server
node.add_peer("lisp-server", "192.168.1.60", 7777)?;
node.send_position("player1", 100.0, 50.0, 25.0)?;
```

## API Reference

### Peer Management

| Method | Description |
|--------|-------------|
| `add_peer(id, host, port)` | Add a peer |
| `remove_peer(id)` | Remove a peer |
| `list_peers()` | List all peers |
| `get_peer_info(id)` | Get peer host/port |

### Gaming API

| Method | Description |
|--------|-------------|
| `send_position(player, x, y, z)` | Send position (unreliable) |
| `send_audio(data)` | Send audio packet (unreliable) |
| `send_game_event(type, data)` | Send event (reliable) |
| `send_udp(data, peer, reliable, type)` | Send raw UDP |
| `send_ping(peer)` | Measure RTT |

### Transactions

| Method | Description |
|--------|-------------|
| `begin_transaction(tx_id)` | Start transaction |
| `commit_transaction(tx_id)` | Commit transaction |
| `rollback_transaction(tx_id)` | Rollback transaction |

### Metrics

| Method | Description |
|--------|-------------|
| `get_metrics()` | Gaming metrics |
| `get_network_stats()` | Network statistics |
| `get_full_metrics()` | Combined metrics |
| `benchmark(peer, count)` | RTT benchmark |

## Building from Source

```bash
# Clone the repository
git clone https://github.com/ALH477/DeMoD-Communication-Framework
cd rust

# Build
cargo build --release

# Run tests
cargo test

# Install
cargo install --path .
```

## License

LGPL-3.0-only - Compatible with the main DCF mono repo.

## Related Projects

- [D-LISP Punctim](../lisp/) - Lisp implementation
- [DCF Design Spec](../docs/dcf_design_spec.md) - Full specification
- [StreamDB](../streamdb/) - Persistent storage backend

## Changelog

### v2.2.0
- Wire-compatible binary protocol with Lisp Punctim
- UDP transport with unreliable/reliable channels
- Network statistics (RTT, jitter, packet loss)
- Gaming-optimized APIs (position, audio, events)
- mDNS peer discovery
- Transaction support
- gRPC backward compatibility
