// SPDX-License-Identifier: LGPL-3.0-only
//! DCF Rust Server - Punctim Compatible
//! 
//! This is the main entry point for the DCF Rust server, providing:
//! - gRPC service for DCF protocol
//! - UDP transport for low-latency gaming/audio
//! - P2P peer discovery
//! - Backward compatible with Lisp Punctim
//! - Universal Shim integration for 120Hz interpolation

use std::sync::{Arc, Mutex};
use std::net::SocketAddr;
use std::time::{SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand};
use tonic::transport::Server;
use uuid::Uuid;
use tokio::net::UdpSocket; 

use dcf_rust_sdk::{
    DcfConfig, DcfNode, DcfError, Result,
    MyDcfService, DefaultMessageHandler,
    run_udp_receiver, run_reliable_handler, run_mesh_runtime,
    Position, GameEvent, MessageHandler,
};
use dcf_rust_sdk::proto::dcf_service_server::DcfServiceServer;

// Include the Universal Shim module
// Ensure universal_shim.rs is in the same directory and methods are 'pub'
mod universal_shim;
use universal_shim::AdaptiveBuffer;

// ============================================================================
// CLI Definition
// ============================================================================

#[derive(Parser)]
#[command(name = "dcf")]
#[command(author = "DeMoD LLC")]
#[command(version = "2.2.0")]
#[command(about = "DeMoD Communications Framework - Rust Implementation")]
struct Cli {
    /// Configuration file path
    #[arg(short, long, default_value = "dcf_config.toml")]
    config: String,

    /// Target address for the shim playback loop (Output target)
    /// Defaults to 127.0.0.1:7777 to feed the local binary port internally
    #[arg(long, default_value = "127.0.0.1:7777")]
    shim_target: String,

    /// Port to listen for incoming legacy game packets (Shim Ingress)
    /// This is the bridge port (e.g., 8888) that Zandronum connects to
    #[arg(long, default_value = "8888")]
    shim_port: u16,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the DCF server
    Start,
    
    /// Show server status
    Status,
    
    /// Show version information
    Version,
    
    /// Add a peer
    AddPeer {
        #[arg(long)]
        peer_id: String,
        #[arg(long)]
        host: String,
        #[arg(long)]
        port: u16,
    },
    
    /// Remove a peer
    RemovePeer {
        #[arg(long)]
        peer_id: String,
    },
    
    /// List all peers
    ListPeers,
    
    /// Send a position update
    SendPosition {
        #[arg(long)]
        player_id: String,
        #[arg(long)]
        x: f32,
        #[arg(long)]
        y: f32,
        #[arg(long)]
        z: f32,
    },
    
    /// Send an audio packet
    SendAudio {
        #[arg(long)]
        data: String,
    },
    
    /// Send a game event
    SendEvent {
        #[arg(long)]
        event_type: u8,
        #[arg(long)]
        data: String,
    },
    
    /// Run benchmark against a peer
    Benchmark {
        #[arg(long)]
        peer_id: String,
        #[arg(long, default_value = "100")]
        count: usize,
    },
    
    /// Get metrics
    Metrics,
    
    /// Begin a transaction
    BeginTx {
        #[arg(long)]
        tx_id: String,
    },
    
    /// Commit a transaction
    CommitTx {
        #[arg(long)]
        tx_id: String,
    },
    
    /// Rollback a transaction
    RollbackTx {
        #[arg(long)]
        tx_id: String,
    },
    
    /// Show detailed help information
    Info,

    /// Run as a self-healing mesh node (peer health FSM + AUTO/master role
    /// assignment + decentralized failover). Meshes with the Go/C/Python nodes.
    Mesh {
        /// Bind address host:port (defaults to the config udp_port on 0.0.0.0)
        #[arg(long)]
        bind: Option<String>,
        /// Mesh mode: p2p | auto | master
        #[arg(long, default_value = "p2p")]
        mode: String,
        /// This node's numeric mesh id (u16; required for auto/master)
        #[arg(long, default_value_t = 0)]
        node_id: u16,
        /// Peer id to REPORT to (auto mode)
        #[arg(long, default_value = "")]
        master: String,
        /// Peer as id@host:port (repeatable)
        #[arg(long)]
        peer: Vec<String>,
    },
}

// ============================================================================
// Shim Message Handler
// ============================================================================

/// Custom handler that feeds incoming positions into the AdaptiveBuffer
struct ShimMessageHandler {
    buffer: Arc<Mutex<AdaptiveBuffer>>,
    default_handler: DefaultMessageHandler,
}

impl ShimMessageHandler {
    fn new(buffer: Arc<Mutex<AdaptiveBuffer>>) -> Self {
        Self {
            buffer,
            default_handler: DefaultMessageHandler,
        }
    }
}

impl MessageHandler for ShimMessageHandler {
    fn handle_position(&self, position: Position, from: SocketAddr) {
        // 1. Log debug info (optional)
        // log::debug!("Shim received pos from {}: {:?}", from, position);

        // 2. Capture precise receive time
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_micros() as u64;

        // 3. Push to the Adaptive Buffer for Dead Reckoning
        if let Ok(mut buf) = self.buffer.lock() {
            buf.push(position, now);
        }
    }

    fn handle_audio(&self, data: &[u8], from: SocketAddr) {
        // Delegate audio to default handler (or implement custom audio mixer)
        self.default_handler.handle_audio(data, from);
    }

    fn handle_game_event(&self, event: GameEvent, from: SocketAddr) {
        // Delegate events to default handler
        self.default_handler.handle_game_event(event, from);
    }
}

// ============================================================================
// Main Entry Point
// ============================================================================

#[tokio::main]
async fn main() -> std::result::Result<(), Box<dyn std::error::Error>> {
    // Initialize logging
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format_timestamp_millis()
        .init();

    let cli = Cli::parse();

    // Commands that don't need any node initialization
    match &cli.command {
        Some(Commands::Version) => {
            let version = DcfNode::version();
            println!("{}", serde_json::to_string_pretty(&version)?);
            return Ok(());
        }
        Some(Commands::Info) => {
            print_info();
            return Ok(());
        }
        Some(Commands::Mesh { bind, mode, node_id, master, peer }) => {
            run_mesh(&cli.config, bind.clone(), mode, *node_id, master, peer).await?;
            return Ok(());
        }
        _ => {}
    }

    // Load configuration
    let config = load_config(&cli.config)?;
    
    // Determine if we need UDP socket binding
    let needs_network = matches!(
        &cli.command,
        None 
        | Some(Commands::Start)
        | Some(Commands::SendPosition { .. })
        | Some(Commands::SendAudio { .. })
        | Some(Commands::SendEvent { .. })
        | Some(Commands::Benchmark { .. })
    );

    // Create node with appropriate config
    let node_config = if needs_network {
        config.clone()
    } else {
        // For non-network commands, disable UDP to avoid port binding
        let mut cfg = config.clone();
        cfg.transport = "none".to_string();
        cfg
    };
    
    let node = Arc::new(DcfNode::new(node_config)?);

    match cli.command {
        Some(Commands::Start) | None => {
            run_server(node, config, cli.shim_target, cli.shim_port).await?;
        }
        Some(Commands::Status) => {
            let status = node.status();
            println!("{}", serde_json::to_string_pretty(&status)?);
        }
        Some(Commands::Version) => unreachable!(),
        Some(Commands::AddPeer { peer_id, host, port }) => {
            node.add_peer(&peer_id, &host, port)?;
            println!("{{\"status\": \"peer-added\", \"peer_id\": \"{}\"}}", peer_id);
        }
        Some(Commands::RemovePeer { peer_id }) => {
            node.remove_peer(&peer_id)?;
            println!("{{\"status\": \"peer-removed\", \"peer_id\": \"{}\"}}", peer_id);
        }
        Some(Commands::ListPeers) => {
            let peers = node.list_peers();
            println!("{}", serde_json::to_string_pretty(&peers)?);
        }
        Some(Commands::SendPosition { player_id, x, y, z }) => {
            node.start()?;
            node.send_position(&player_id, x, y, z)?;
            println!("{{\"status\": \"position-sent\", \"player\": \"{}\"}}", player_id);
        }
        Some(Commands::SendAudio { data }) => {
            node.start()?;
            let bytes = hex::decode(&data).unwrap_or_else(|_| data.as_bytes().to_vec());
            node.send_audio(&bytes)?;
            println!("{{\"status\": \"audio-sent\"}}");
        }
        Some(Commands::SendEvent { event_type, data }) => {
            node.start()?;
            node.send_game_event(event_type, &data)?;
            println!("{{\"status\": \"event-sent\"}}");
        }
        Some(Commands::Benchmark { peer_id, count }) => {
            node.start()?;
            let result = node.benchmark(&peer_id, count).await?;
            println!("{}", serde_json::to_string_pretty(&result)?);
        }
        Some(Commands::Metrics) => {
            let metrics = node.get_full_metrics();
            println!("{}", serde_json::to_string_pretty(&metrics)?);
        }
        Some(Commands::BeginTx { tx_id }) => {
            node.begin_transaction(&tx_id)?;
            println!("{{\"status\": \"transaction-started\", \"tx_id\": \"{}\"}}", tx_id);
        }
        Some(Commands::CommitTx { tx_id }) => {
            node.commit_transaction(&tx_id)?;
            println!("{{\"status\": \"transaction-committed\", \"tx_id\": \"{}\"}}", tx_id);
        }
        Some(Commands::RollbackTx { tx_id }) => {
            node.rollback_transaction(&tx_id)?;
            println!("{{\"status\": \"transaction-rolled-back\", \"tx_id\": \"{}\"}}", tx_id);
        }
        Some(Commands::Info) => unreachable!(),
        Some(Commands::Mesh { .. }) => unreachable!(),
    }

    Ok(())
}

// ============================================================================
// Server Runner
// ============================================================================

async fn run_server(
    node: Arc<DcfNode>, 
    config: DcfConfig,
    shim_target: String,
    shim_port: u16
) -> std::result::Result<(), Box<dyn std::error::Error>> {
    // Start the node
    node.start()?;

    // Create the gRPC service
    let grpc_service = MyDcfService::new(node.clone());
    let grpc_addr = format!("[::1]:{}", config.grpc_port).parse()?;

    // --- INTEGRATION: Initialize Universal Shim ---
    log::info!("Initializing Universal Shim buffer...");
    let buffer = Arc::new(Mutex::new(AdaptiveBuffer::new()));

    // --- INTEGRATION: Spawn Shim Playback Loop (OUTPUT) ---
    // This runs at ~125Hz to smooth out jitter
    let playback_node = node.clone();
    let playback_buffer = buffer.clone();
    let playback_target = shim_target.clone();
    
    let playback_task = tokio::spawn(async move {
        log::info!("Starting Shim Playback Loop targeting {}", playback_target);
        universal_shim::run_playback_loop(playback_buffer, playback_node, playback_target).await;
    });

    // --- INTEGRATION: Spawn Shim Ingress Listener (INPUT) ---
    // This binds to the Shim Port (8888) to accept Zandronum packets
    let shim_listener_buffer = buffer.clone();
    let shim_listener_task = tokio::spawn(async move {
        let addr = format!("0.0.0.0:{}", shim_port);
        match UdpSocket::bind(&addr).await {
            Ok(socket) => {
                log::info!("[Shim Ingress] Listening for game packets on {}", addr);
                let mut buf = vec![0u8; 2048];
                loop {
                    match socket.recv_from(&mut buf).await {
                        Ok((size, _src)) => {
                            if size > 0 {
                                // For debugging, log activity
                                log::debug!("[Shim Ingress] Received {} bytes from {}", size, _src);
                                
                                // Here we would parse legacy packets.
                                // For now, we update buffer to keep connection alive.
                                // In a full implementation, you'd decode Zandronum packets here.
                                let now = SystemTime::now()
                                    .duration_since(UNIX_EPOCH)
                                    .unwrap()
                                    .as_micros() as u64;
                                    
                                if let Ok(mut b) = shim_listener_buffer.lock() {
                                    // Inject dummy position for keepalive or parsed data
                                    b.push(Position { x: 0.0, y: 0.0, z: 0.0 }, now);
                                }
                            }
                        }
                        Err(e) => log::error!("[Shim Ingress] Receive error: {}", e),
                    }
                }
            }
            Err(e) => {
                log::error!("[Shim Ingress] FAILED TO BIND: {}", e);
            }
        }
    });

    // --- INTEGRATION: Use Custom Handler for Punctim ---
    // Instead of DefaultMessageHandler, use ShimMessageHandler to feed the buffer
    let udp_node = node.clone();
    let handler = Arc::new(ShimMessageHandler::new(buffer.clone())) as Arc<dyn dcf_rust_sdk::MessageHandler>;
    let udp_handler = handler.clone();
    
    let udp_task = tokio::spawn(async move {
        if let Err(e) = run_udp_receiver(udp_node, udp_handler).await {
            log::error!("UDP receiver error: {}", e);
        }
    });

    // Start reliable message handler in background
    let reliable_node = node.clone();
    let reliable_task = tokio::spawn(async move {
        if let Err(e) = run_reliable_handler(reliable_node).await {
            log::error!("Reliable handler error: {}", e);
        }
    });

    // Start P2P discovery if enabled
    if config.p2p_discovery {
        let discovery_node = node.clone();
        tokio::spawn(async move {
            if let Err(e) = run_peer_discovery(discovery_node).await {
                log::error!("Peer discovery error: {}", e);
            }
        });
    }

    log::info!("╔══════════════════════════════════════════════════════════════════════════╗");
    log::info!("║         DCF Rust Server v2.2.0 - Punctim Compatible                   ║");
    log::info!("╚══════════════════════════════════════════════════════════════════════════╝");
    log::info!("gRPC server listening on {}", grpc_addr);
    log::info!("UDP server listening on port {}", config.udp_port);
    log::info!("Shim Ingress listening on port {}", shim_port);
    log::info!("Shim Playback Active: {}", shim_target);
    log::info!("Node ID: {}", node.node_id());
    log::info!("Mode: {}", config.mode);

    // Run gRPC server
    Server::builder()
        .add_service(DcfServiceServer::new(grpc_service))
        .serve(grpc_addr)
        .await?;

    // Cleanup
    node.stop()?;
    udp_task.abort();
    reliable_task.abort();
    playback_task.abort();
    shim_listener_task.abort();

    Ok(())
}

// ============================================================================
// Self-healing mesh node
// ============================================================================

/// Parse a `id@host:port` peer spec.
fn parse_peer_spec(s: &str) -> std::result::Result<(String, String, u16), String> {
    let (id, addr) = s.split_once('@').ok_or_else(|| format!("expected id@host:port, got {:?}", s))?;
    let (host, port) = addr.rsplit_once(':').ok_or_else(|| format!("expected host:port, got {:?}", addr))?;
    let port: u16 = port.parse().map_err(|_| format!("bad port in {:?}", s))?;
    Ok((id.to_string(), host.to_string(), port))
}

/// Run as a self-healing mesh node until Ctrl-C. Mirrors `go/cmd/dcfnode start`.
async fn run_mesh(
    config_path: &str,
    bind: Option<String>,
    mode: &str,
    node_id: u16,
    master: &str,
    peers: &[String],
) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let mut config = load_config(config_path)?;
    config.transport = "UDP".to_string();
    config.node_id = Some(node_id.to_string());
    if let Some(b) = bind {
        let (host, port) = b.rsplit_once(':').ok_or("--bind expects host:port")?;
        config.host = host.to_string();
        config.udp_port = port.parse().map_err(|_| "bad --bind port")?;
    }
    let group_thr = config.group_rtt_threshold as i32;

    let node = Arc::new(DcfNode::new(config)?);
    for ps in peers {
        let (id, host, port) = parse_peer_spec(ps).map_err(|e| format!("--peer {}: {}", ps, e))?;
        node.add_peer(&id, &host, port)?;
    }
    node.enable_mesh(mode, node_id, master, group_thr);
    node.start()?;
    log::info!(
        "self-healing mesh: mode={} node-id={} master={:?} listening on {}:{}",
        mode, node_id, master, node.config().host, node.config().udp_port
    );

    // UDP receiver (drives PING/PONG + MsgMesh dispatch) + the mesh tick loop.
    let recv_node = node.clone();
    let handler = Arc::new(DefaultMessageHandler) as Arc<dyn MessageHandler>;
    let recv_task = tokio::spawn(async move {
        if let Err(e) = run_udp_receiver(recv_node, handler).await {
            log::error!("UDP receiver error: {}", e);
        }
    });
    run_mesh_runtime(node.clone());

    log::info!("dcf mesh node ready (Ctrl-C to stop)");
    tokio::signal::ctrl_c().await?;
    log::info!("shutting down");
    node.stop()?;
    recv_task.abort();
    Ok(())
}

// ============================================================================
// Peer Discovery (mDNS)
// ============================================================================

async fn run_peer_discovery(node: Arc<DcfNode>) -> Result<()> {
    use mdns_sd::{ServiceDaemon, ServiceEvent};

    let mdns = ServiceDaemon::new()
        .map_err(|e| DcfError::Network(format!("mDNS init failed: {}", e)))?;

    let service_type = "_dcf._tcp.local.";
    
    // Register our service
    let service_name = format!("dcf-{}", node.node_id());
    let host = format!("{}.local.", node.node_id());
    let service_info = mdns_sd::ServiceInfo::new(
        service_type,
        &service_name,
        &host,
        "",
        node.config().udp_port,
        None,
    ).map_err(|e| DcfError::Network(format!("Service info error: {}", e)))?;

    mdns.register(service_info)
        .map_err(|e| DcfError::Network(format!("mDNS register failed: {}", e)))?;

    // Browse for peers
    let receiver = mdns.browse(service_type)
        .map_err(|e| DcfError::Network(format!("mDNS browse failed: {}", e)))?;

    let rtt_threshold = node.config().group_rtt_threshold;

    while node.is_running() {
        match receiver.recv_async().await {
            Ok(event) => {
                if let ServiceEvent::ServiceResolved(info) = event {
                    if let Some(addr) = info.get_addresses().iter().next() {
                        let addr_str = addr.to_string();
                        let port = info.get_port();
                        
                        // Measure RTT (simple placeholder)
                        let rtt = measure_rtt(&addr_str).await;
                        
                        if rtt < rtt_threshold as i64 {
                            let peer_id = format!("discovered-{}", Uuid::new_v4());
                            if let Err(e) = node.add_peer(&peer_id, &addr_str, port) {
                                log::warn!("Failed to add discovered peer: {}", e);
                            } else {
                                log::info!("Discovered peer: {} at {}:{} (RTT: {}ms)", 
                                          peer_id, addr_str, port, rtt);
                            }
                        }
                    }
                }
            }
            Err(e) => {
                log::debug!("mDNS receive timeout: {}", e);
            }
        }
    }

    Ok(())
}

async fn measure_rtt(_address: &str) -> i64 {
    // Placeholder - real implementation would use TCP or ICMP ping
    rand::random::<i64>().abs() % 100
}

// ============================================================================
// Configuration Loading
// ============================================================================

fn load_config(path: &str) -> std::result::Result<DcfConfig, Box<dyn std::error::Error>> {
    if std::path::Path::new(path).exists() {
        let content = std::fs::read_to_string(path)?;
        
        if path.ends_with(".json") {
            Ok(serde_json::from_str(&content)?)
        } else if path.ends_with(".toml") {
            Ok(toml::from_str(&content)?)
        } else {
            // Try TOML first, then JSON
            toml::from_str(&content)
                .or_else(|_| serde_json::from_str(&content))
                .map_err(|e| e.into())
        }
    } else {
        log::warn!("Config file not found: {}, using defaults", path);
        Ok(DcfConfig::default())
    }
}

// ============================================================================
// Info Text (renamed from Help to avoid conflict)
// ============================================================================

fn print_info() {
    println!(r#"
╔══════════════════════════════════════════════════════════════════════════╗
║         DCF Rust Server v2.2.0 - Punctim Compatible                   ║
╚══════════════════════════════════════════════════════════════════════════╝

**Quick Start for Gaming:**
1. dcf --config config.json start
2. dcf add-peer --peer-id player2 --host 192.168.1.100 --port 7777
3. dcf send-position --player-id player1 --x 100.0 --y 50.0 --z 25.0
4. dcf metrics

**Shim Integration:**
  --shim-target <addr>  Target address for smoothed 120Hz output (default: 127.0.0.1:7777)
  --shim-port <port>    Listener port for legacy game packets (default: 8888)

**Key Features:**
- UDP with unreliable (<5ms) / reliable channels
- Universal Shim for Dead Reckoning & Jitter Compensation
- Binary Protobuf: 10-100x faster than JSON
- Position (12B), Audio (raw), Events (reliable)
- RTT/Jitter stats, auto-retry
- gRPC for backward compatibility

**Commands:**
  start              Start the DCF server
  status             Show server status
  version            Show version information
  add-peer           Add a peer
  remove-peer        Remove a peer
  list-peers         List all peers
  send-position      Send position update
  send-audio         Send audio packet
  send-event         Send game event
  benchmark          Run RTT benchmark
  metrics            Get metrics
  begin-tx           Begin transaction
  commit-tx          Commit transaction
  rollback-tx        Rollback transaction
  info               Show this detailed help
  help               Show command help (built-in)

**Configuration:**
  --config <file>    Config file (JSON or TOML)

**Example Config (dcf_config.toml):**
  transport = "UDP"
  host = "0.0.0.0"
  grpc_port = 50051
  udp_port = 7777
  mode = "p2p"
  p2p_discovery = true

Repo: https://github.com/ALH477/DeMoD-Communication-Framework
"#);
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = DcfConfig::default();
        assert_eq!(config.udp_port, 7777);
        assert_eq!(config.grpc_port, 50051);
    }

    #[tokio::test]
    async fn test_node_creation() {
        let config = DcfConfig::default();
        let node = DcfNode::new(config).unwrap();
        assert!(!node.is_running());
    }

    #[tokio::test]
    async fn test_peer_management() {
        let config = DcfConfig::default();
        let node = DcfNode::new(config).unwrap();
        
        node.add_peer("test-peer", "127.0.0.1", 7778).unwrap();
        assert_eq!(node.list_peers().len(), 1);
        
        node.remove_peer("test-peer").unwrap();
        assert_eq!(node.list_peers().len(), 0);
    }
}
