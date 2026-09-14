# Thunderbolt/USB4 Transport Plugin

**Version 1.0.0** | Rust Implementation for DCF

A high-performance transport plugin for the DeMoD Communications Framework (DCF) that provides ultra-low-latency communication over Thunderbolt/USB4 connections using IPv6 link-local addresses.

## Features

- **UDP Control Channel**: Low-latency control messages (10-100μs typical)
- **TCP Data Streams**: High-throughput bulk data transfers (20-40 Gbps)
- **IPv6 Link-Local**: Direct peer-to-peer via `fe80::/10` addresses
- **Interface Binding**: Dedicated to specific Thunderbolt interfaces
- **RTT Measurement**: Built-in round-trip time ping/pong protocol
- **Statistics Tracking**: Messages, bytes, connections, errors
- **Thread-Safe**: All operations are safe for concurrent access
- **Non-Blocking I/O**: Efficient polling with background threads

## Quick Start

### Basic Setup

```rust
use thunderbolt_transport::{ThunderboltTransportBuilder, MessageType, MessageData, LogLevel};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let transport = ThunderboltTransportBuilder::new()
        .interface("thunderbolt0")
        .control_port(5000)
        .data_port(5001)
        .message_handler(|msg_type, data| {
            match msg_type {
                MessageType::ControlMessage => {
                    if let MessageData::Bytes(bytes) = data {
                        println!("Control: {} bytes", bytes.len());
                    }
                }
                MessageType::DataStream => {
                    if let MessageData::Stream(stream) = data {
                        println!("Data stream received");
                    }
                }
            }
        })
        .logger(|level, msg| {
            println!("[{:?}] {}", level, msg);
        })
        .build()?;

    // Transport is now running with background threads
    
    Ok(())
}
```

### Send Control Message

```rust
// Send UDP control message (low latency, unreliable)
transport.send_message(
    &[1, 2, 3, 4, 5],
    "fe80::1%thunderbolt0"
)?;
```

### Open Data Stream

```rust
use std::io::{Read, Write};

// Open TCP data stream (high throughput, reliable)
let mut stream = transport.open_data_stream("fe80::1%thunderbolt0")?;

// Write data
stream.write_all(b"Hello, Thunderbolt!")?;
stream.flush()?;

// Read response
let mut response = [0u8; 1024];
let n = stream.read(&mut response)?;
```

### Measure RTT

```rust
use std::time::Duration;

// Measure round-trip time
if let Some(rtt) = transport.measure_rtt("fe80::1%thunderbolt0", Duration::from_secs(5))? {
    println!("RTT: {:.3} ms", rtt * 1000.0);
}
```

### Get Statistics

```rust
let stats = transport.get_stats();
println!("Messages sent: {}", stats.messages_sent);
println!("Bytes sent: {}", stats.bytes_sent);
println!("Errors: {}", stats.errors);
```

## Configuration

### ThunderboltConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `interface` | `String` | `"thunderbolt0"` | Network interface name |
| `control_port` | `u16` | `5000` | UDP port for control messages |
| `data_port` | `u16` | `5001` | TCP port for data streams |
| `buffer_size` | `usize` | `64 MiB` | Socket buffer size |
| `tcp_nodelay` | `bool` | `true` | Enable TCP_NODELAY |
| `tcp_keepalive` | `bool` | `true` | Enable SO_KEEPALIVE |

## Protocol Details

### Control Messages (UDP)

- Maximum payload: 65,507 bytes
- Low latency: ~10-100μs on Thunderbolt
- No delivery guarantee
- Use for: heartbeats, coordination, RTT pings

### Data Streams (TCP)

- Reliable, ordered delivery
- High throughput: 20-40 Gbps typical
- 64 MiB buffers by default
- Use for: large transfers, model weights, checkpoints

### RTT Ping Protocol

```
Ping: [MAGIC: 8 bytes] [TIMESTAMP: 8 bytes]
       0x5254545054      nanoseconds (big-endian)

Pong: [MAGIC: 8 bytes] [TIMESTAMP: 8 bytes]  
       0x5254545053      echo back same timestamp
```

### IPv6 Address Format

```
fe80::1%thunderbolt0    # With interface name
fe80::1%2               # With interface index
::1%lo                  # Loopback (for testing)
```

## Performance Characteristics

| Metric | Typical Value |
|--------|---------------|
| RTT | 5-50 μs |
| UDP Latency | 10-100 μs |
| TCP Throughput | 20-40 Gbps |
| Buffer Size | 64 MiB |

*Values depend on hardware, CPU, and system configuration.*

## Threading Model

- **TCP Acceptor Thread**: Accepts incoming TCP connections
- **UDP Receiver Thread**: Receives incoming UDP messages
- **Message Handler**: Called from background threads (must be thread-safe)
- **All public methods**: Thread-safe for concurrent access

## Error Handling

All errors are returned as `TransportError`:

```rust
pub enum TransportError {
    Io(io::Error),
    InvalidAddress(String),
    InterfaceNotFound(String),
    MessageTooLarge { size: usize, max: usize },
    ConnectionFailed(String),
    NotInitialized,
    Shutdown,
    InvalidMessage(String),
    Timeout,
    SocketOption(String),
}
```

## Linux Configuration

### Check Thunderbolt Interface

```bash
# List interfaces
ip link show | grep thunderbolt

# Check IPv6 address
ip -6 addr show thunderbolt0
```

### Create Bond for Multiple Ports

```bash
# Create 802.3ad bond for aggregated bandwidth
ip link add bond0 type bond mode 802.3ad
ip link set thunderbolt0 master bond0
ip link set thunderbolt1 master bond0
ip link set bond0 up

# Then use interface "bond0"
```

### Tune TCP Performance

```bash
# Increase buffer sizes
sysctl -w net.core.rmem_max=134217728
sysctl -w net.core.wmem_max=134217728
sysctl -w net.ipv4.tcp_rmem="4096 87380 134217728"
sysctl -w net.ipv4.tcp_wmem="4096 65536 134217728"
```

## Compatibility

### With Lisp Punctim

This Rust implementation is wire-compatible with the Lisp `thunderbolt-transport.lisp`:

- Same RTT ping/pong protocol
- Same IPv6 address format with scope
- Same default ports (5000/5001)
- Same message framing

### Cross-Language Example

```lisp
;; Lisp: Send to Rust
(send-message *transport* #(1 2 3 4 5) "fe80::1%thunderbolt0")
```

```rust
// Rust: Receive from Lisp
transport.message_handler(|msg_type, data| {
    if let MessageData::Bytes(bytes) = data {
        assert_eq!(bytes, vec![1, 2, 3, 4, 5]);
    }
});
```

## Testing

```bash
# Run all tests
cargo test

# Run with output
cargo test -- --nocapture

# Run specific test
cargo test test_udp_loopback_echo

# Run benchmarks
cargo test benchmark -- --nocapture
```

## Building

```bash
# Debug build
cargo build

# Release build (optimized)
cargo build --release

# With async feature
cargo build --features async
```

## License

LGPL-3.0 - Part of the DeMoD Communications Framework.

## Related

- [DCF Rust SDK](../dcf_rust_sdk/) - Main framework
- [Punctim Lisp](../lisp/punctim.lisp) - Lisp implementation
- [DCF Design Spec](../docs/dcf_design_spec.md) - Full specification
