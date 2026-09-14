// SPDX-License-Identifier: LGPL-3.0-only
//! DCF Rust SDK - Punctim Compatible
//! Version 2.2.1 | Production Grade with Enhanced Shim Visibility
//! 
//! This SDK provides a production-ready Rust implementation for DCF,
//! optimized for gaming and real-time audio with UDP transport, binary Protobuf,
//! unreliable/reliable channels, and network stats.

use std::collections::HashMap;
use std::io::{self, Cursor, Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, ToSocketAddrs, UdpSocket};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use byteorder::{BigEndian, LittleEndian, ReadBytesExt, WriteBytesExt};
use parking_lot::{Mutex, RwLock};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::mpsc;
use uuid::Uuid;

/// Generated protocol buffer definitions
/// In production, uncomment the tonic::include_proto! line and remove the module include
/// after running `cargo build` which invokes build.rs to generate the code.
// pub mod proto {
//     tonic::include_proto!("dcf");
// }

pub mod generated;
pub use generated::dcf as proto;

/// DCF-Audio: collaborative audio over the DeModFrame wire (re-exported codec crate).
/// Provides `packetize`, `AudioReassembler`, `codec_for`, etc. See DCF_AUDIO_SPEC.md.
pub use dcf_wire_codec::audio;

/// Feed a received AUDIO payload that is a single 17-byte DeModFrame into a
/// reassembler; returns `Some(packet)` once a full audio block has arrived. An
/// application's `handle_audio` can own an [`audio::AudioReassembler`] and call this
/// to turn the per-frame AUDIO stream back into 20 ms codec blocks.
pub fn reassemble_audio_payload(
    reasm: &mut audio::AudioReassembler,
    payload: &[u8],
) -> Option<audio::AudioPacket> {
    let frame: [u8; 17] = payload.try_into().ok()?;
    reasm.push(&frame)
}

/// DCF-Mesh: the self-healing runtime (peer health FSM + AUTO/master role
/// assignment + decentralized failover), driving the certified
/// `dcf_wire_codec::mesh` algorithms from live PING/PONG + the REPORT/ROLE control
/// adapter. Meshes with the Go and C nodes. See [`MeshRuntime`].
pub mod mesh_runtime;
pub use mesh_runtime::MeshRuntime;

/// DCF-Game: multiplayer state/events over the DeModFrame wire (re-exported codec).
/// Provides `packetize`, `GameReassembler`, body packers, etc. See DCF_GAME_SPEC.md.
pub use dcf_wire_codec::game;

/// Feed a received GAME payload that is a single 17-byte DeModFrame into a
/// reassembler; returns `Some(packet)` once a full game message has arrived. An
/// application's `handle_game` can own a [`game::GameReassembler`] (one per `src_id`)
/// and call this to turn the per-frame GAME stream back into whole messages.
pub fn reassemble_game_payload(
    reasm: &mut game::GameReassembler,
    payload: &[u8],
) -> Option<game::GamePacket> {
    let frame: [u8; 17] = payload.try_into().ok()?;
    reasm.push(&frame)
}

// Re-exports for convenience
pub use proto::{
    DcfMessage, DcfResponse, Empty, Metrics, PeerInfo, RoleAssignment,
};

// ============================================================================
// Constants - Compatible with Lisp Punctim
// ============================================================================

/// Message types matching Lisp implementation
pub mod msg_type {
    pub const POSITION: u8 = 1;
    pub const AUDIO: u8 = 2; // Used by Shim for raw game data
    pub const GAME_EVENT: u8 = 3;
    pub const STATE_SYNC: u8 = 4;
    pub const RELIABLE: u8 = 5;
    pub const ACK: u8 = 6;
    pub const PING: u8 = 7;
    pub const PONG: u8 = 8;
    /// DCF-Game L2 frame (one 17-byte DeModFrame DATA frame). See DCF_GAME_SPEC.md.
    pub const GAME_DCF: u8 = 9;
    /// DCF-Text L2 frame (Go extension; back-port for parity).
    pub const TEXT_DCF: u8 = 10;
    /// DCF-Mesh control message (REPORT/ROLE) for self-healing AUTO/master roles.
    pub const MESH: u8 = 11;
}

const DEFAULT_UDP_PORT: u16 = 7777;
const DEFAULT_GRPC_PORT: u16 = 50051;
const DEFAULT_UDP_MTU: usize = 1400;
const DEFAULT_RELIABLE_TIMEOUT_MS: u64 = 500;
const DEFAULT_MAX_RETRIES: u32 = 3;
const DEFAULT_RTT_ALPHA: f64 = 0.125;

// ============================================================================
// Error Types
// ============================================================================

#[derive(Error, Debug)]
pub enum DcfError {
    #[error("IO error: {0}")]
    Io(#[from] io::Error),
    
    #[error("Not initialized: {0}")]
    NotInitialized(String),
    
    #[error("Peer not found: {0}")]
    PeerNotFound(String),
    
    #[error("Invalid message: {0}")]
    InvalidMessage(String),
    
    #[error("Network error: {0}")]
    Network(String),
    
    #[error("Serialization error: {0}")]
    Serialization(String),
    
    #[error("Database error: {0}")]
    Database(String),
    
    #[error("Transaction error: {0}")]
    Transaction(String),
    
    #[error("Configuration error: {0}")]
    Config(String),
}

pub type Result<T> = std::result::Result<T, DcfError>;

// ============================================================================
// Binary Protocol - Compatible with Lisp Punctim
// ============================================================================

/// Protocol message header (17 bytes minimum)
/// Layout: type(1) + sequence(4) + timestamp(8) + payload_len(4) + payload(N)
#[derive(Debug, Clone)]
pub struct ProtoMessage {
    pub msg_type: u8,
    pub sequence: u32,
    pub timestamp: u64,
    pub payload: Vec<u8>,
}

impl ProtoMessage {
    pub fn new(msg_type: u8, sequence: u32, payload: Vec<u8>) -> Self {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_micros() as u64)
            .unwrap_or(0);
        Self { msg_type, sequence, timestamp, payload }
    }

    /// Serialize to bytes (big-endian, matching Lisp)
    pub fn serialize(&self) -> Vec<u8> {
        let total_len = 1 + 4 + 8 + 4 + self.payload.len();
        let mut vec = Vec::with_capacity(total_len);
        vec.push(self.msg_type);
        vec.extend_from_slice(&self.sequence.to_be_bytes());
        vec.extend_from_slice(&self.timestamp.to_be_bytes());
        vec.extend_from_slice(&(self.payload.len() as u32).to_be_bytes());
        vec.extend_from_slice(&self.payload);
        vec
    }

    /// Deserialize from bytes
    pub fn deserialize(data: &[u8]) -> Result<Self> {
        if data.len() < 17 {
            return Err(DcfError::InvalidMessage("Message too short".to_string()));
        }
        
        let mut cursor = Cursor::new(data);
        let msg_type = cursor.read_u8()?;
        let sequence = cursor.read_u32::<BigEndian>()?;
        let timestamp = cursor.read_u64::<BigEndian>()?;
        let payload_len = cursor.read_u32::<BigEndian>()? as usize;
        
        if data.len() < 17 + payload_len {
            return Err(DcfError::InvalidMessage("Payload length exceeds message size".to_string()));
        }
        
        let payload = data[17..17 + payload_len].to_vec();
        Ok(Self { msg_type, sequence, timestamp, payload })
    }
}

// ============================================================================
// Position Encoding (12 bytes: x, y, z as float32)
// ============================================================================

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct Position {
    pub x: f32,
    pub y: f32,
    pub z: f32,
}

impl Position {
    pub fn new(x: f32, y: f32, z: f32) -> Self {
        Self { x, y, z }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut vec = Vec::with_capacity(12);
        vec.extend_from_slice(&self.x.to_be_bytes());
        vec.extend_from_slice(&self.y.to_be_bytes());
        vec.extend_from_slice(&self.z.to_be_bytes());
        vec
    }

    pub fn decode(data: &[u8]) -> Result<Self> {
        if data.len() < 12 {
            return Err(DcfError::InvalidMessage("Position payload too short".to_string()));
        }
        let mut cursor = Cursor::new(data);
        let x = cursor.read_f32::<BigEndian>()?;
        let y = cursor.read_f32::<BigEndian>()?;
        let z = cursor.read_f32::<BigEndian>()?;
        Ok(Self { x, y, z })
    }
}

// ============================================================================
// Game Event Encoding
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GameEvent {
    pub event_type: u8,
    pub data: String,
}

impl GameEvent {
    pub fn new(event_type: u8, data: impl Into<String>) -> Self {
        Self { event_type, data: data.into() }
    }

    pub fn encode(&self) -> Vec<u8> {
        let data_bytes = self.data.as_bytes();
        let mut vec = Vec::with_capacity(1 + data_bytes.len());
        vec.push(self.event_type);
        vec.extend_from_slice(data_bytes);
        vec
    }

    pub fn decode(data: &[u8]) -> Result<Self> {
        if data.is_empty() {
            return Err(DcfError::InvalidMessage("Event payload too short".to_string()));
        }
        let event_type = data[0];
        let data_str = String::from_utf8(data[1..].to_vec())
            .map_err(|e| DcfError::InvalidMessage(e.to_string()))?;
        Ok(Self { event_type, data: data_str })
    }
}

// ============================================================================
// Network Statistics - Matching Lisp Implementation
// ============================================================================

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct NetworkStats {
    pub packets_sent: u64,
    pub packets_received: u64,
    pub bytes_sent: u64,
    pub bytes_received: u64,
    pub packets_lost: u64,
    pub retransmits: u64,
    pub last_rtt: f64,
    pub avg_rtt: f64,
    pub jitter: f64,
}

impl NetworkStats {
    pub fn update_rtt(&mut self, rtt: f64) {
        let prev_rtt = self.last_rtt;
        self.last_rtt = rtt;
        self.avg_rtt = DEFAULT_RTT_ALPHA * rtt + (1.0 - DEFAULT_RTT_ALPHA) * self.avg_rtt;
        let jitter = (rtt - prev_rtt).abs();
        self.jitter = DEFAULT_RTT_ALPHA * jitter + (1.0 - DEFAULT_RTT_ALPHA) * self.jitter;
    }
}

// ============================================================================
// Reliable Packet Tracking
// ============================================================================

#[derive(Debug, Clone)]
struct ReliablePacket {
    msg: ProtoMessage,
    host: String,
    port: u16,
    attempts: u32,
    sent_time: Instant,
}

// ============================================================================
// UDP Endpoint
// ============================================================================

pub struct UdpEndpoint {
    socket: Arc<UdpSocket>,
    port: u16,
    running: Arc<AtomicBool>,
    stats: Arc<RwLock<NetworkStats>>,
    reliable_packets: Arc<Mutex<HashMap<u32, ReliablePacket>>>,
    ack_received: Arc<Mutex<HashMap<u32, bool>>>,
    reliable_timeout_ms: u64,
    max_retries: u32,
}

impl UdpEndpoint {
    pub fn new(port: u16) -> Result<Self> {
        let addr = format!("0.0.0.0:{}", port);
        let socket = UdpSocket::bind(&addr)?;
        socket.set_nonblocking(true)?;
        
        Ok(Self {
            socket: Arc::new(socket),
            port,
            running: Arc::new(AtomicBool::new(false)),
            stats: Arc::new(RwLock::new(NetworkStats::default())),
            reliable_packets: Arc::new(Mutex::new(HashMap::new())),
            ack_received: Arc::new(Mutex::new(HashMap::new())),
            reliable_timeout_ms: DEFAULT_RELIABLE_TIMEOUT_MS,
            max_retries: DEFAULT_MAX_RETRIES,
        })
    }

    pub fn send_raw(&self, data: &[u8], addr: &SocketAddr) -> Result<()> {
        self.socket.send_to(data, addr)?;
        let mut stats = self.stats.write();
        stats.packets_sent += 1;
        stats.bytes_sent += data.len() as u64;
        Ok(())
    }

    pub fn send_message(&self, msg: &ProtoMessage, host: &str, port: u16, reliable: bool) -> Result<()> {
        // Resolve via getaddrinfo so hostname peers (e.g. "localhost", Docker
        // container names) work, not just IP literals — `SocketAddr::parse` rejects
        // hostnames. Prefer an IPv4 result since the node socket binds IPv4 (else a
        // host that resolves to ::1 first is unreachable). Matches the Go node.
        let resolved: Vec<SocketAddr> = (host, port)
            .to_socket_addrs()
            .map_err(|e| DcfError::Network(format!("resolve {}:{}: {}", host, port, e)))?
            .collect();
        let addr: SocketAddr = resolved
            .iter()
            .find(|a| a.is_ipv4())
            .or_else(|| resolved.first())
            .copied()
            .ok_or_else(|| DcfError::Network(format!("no address for {}:{}", host, port)))?;

        let data = msg.serialize();
        
        if reliable {
            let packet = ReliablePacket {
                msg: msg.clone(),
                host: host.to_string(),
                port,
                attempts: 1,
                sent_time: Instant::now(),
            };
            self.reliable_packets.lock().insert(msg.sequence, packet);
        }
        
        self.send_raw(&data, &addr)
    }

    pub fn send_ack(&self, seq: u32, addr: &SocketAddr) -> Result<()> {
        let mut payload = Vec::with_capacity(4);
        payload.extend_from_slice(&seq.to_be_bytes());
        let msg = ProtoMessage::new(msg_type::ACK, 0, payload);
        self.send_raw(&msg.serialize(), addr)
    }

    pub fn send_pong(&self, seq: u32, timestamp: u64, addr: &SocketAddr) -> Result<()> {
        let msg = ProtoMessage {
            msg_type: msg_type::PONG,
            sequence: seq,
            timestamp,
            payload: vec![],
        };
        self.send_raw(&msg.serialize(), addr)
    }

    pub fn get_stats(&self) -> NetworkStats {
        self.stats.read().clone()
    }

    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    pub fn start(&self) {
        self.running.store(true, Ordering::SeqCst);
    }

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
    }
}

// ============================================================================
// DCF Configuration
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DcfConfig {
    #[serde(default = "default_transport")]
    pub transport: String,
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_grpc_port")]
    pub grpc_port: u16,
    #[serde(default = "default_udp_port")]
    pub udp_port: u16,
    #[serde(default = "default_mode")]
    pub mode: String,
    #[serde(default)]
    pub node_id: Option<String>,
    #[serde(default)]
    pub peers: Vec<String>,
    #[serde(default = "default_rtt_threshold")]
    pub group_rtt_threshold: u64,
    #[serde(default = "default_storage")]
    pub storage: String,
    #[serde(default)]
    pub streamdb_path: Option<String>,
    #[serde(default = "default_optimization")]
    pub optimization_level: u8,
    #[serde(default = "default_retry_max")]
    pub retry_max: u32,
    #[serde(default = "default_udp_mtu")]
    pub udp_mtu: usize,
    #[serde(default = "default_reliable_timeout")]
    pub udp_reliable_timeout: u64,
    #[serde(default = "default_audio_priority")]
    pub audio_priority: bool,
    #[serde(default)]
    pub p2p_discovery: bool,
    #[serde(default)]
    pub streamdb: StreamDbConfig,
}

fn default_transport() -> String { "UDP".to_string() }
fn default_host() -> String { "0.0.0.0".to_string() }
fn default_grpc_port() -> u16 { DEFAULT_GRPC_PORT }
fn default_udp_port() -> u16 { DEFAULT_UDP_PORT }
fn default_mode() -> String { "p2p".to_string() }
fn default_rtt_threshold() -> u64 { 50 }
fn default_storage() -> String { "in-memory".to_string() }
fn default_optimization() -> u8 { 2 }
fn default_retry_max() -> u32 { 3 }
fn default_udp_mtu() -> usize { DEFAULT_UDP_MTU }
fn default_reliable_timeout() -> u64 { DEFAULT_RELIABLE_TIMEOUT_MS }
fn default_audio_priority() -> bool { true }

impl Default for DcfConfig {
    fn default() -> Self {
        Self {
            transport: default_transport(),
            host: default_host(),
            grpc_port: default_grpc_port(),
            udp_port: default_udp_port(),
            mode: default_mode(),
            node_id: None,
            peers: vec![],
            group_rtt_threshold: default_rtt_threshold(),
            storage: default_storage(),
            streamdb_path: None,
            optimization_level: default_optimization(),
            retry_max: default_retry_max(),
            udp_mtu: default_udp_mtu(),
            udp_reliable_timeout: default_reliable_timeout(),
            audio_priority: default_audio_priority(),
            p2p_discovery: false,
            streamdb: StreamDbConfig::default(),
        }
    }
}

// ============================================================================
// StreamDB Configuration
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StreamDbConfig {
    #[serde(default = "default_page_size")]
    pub page_size: u64,
    #[serde(default = "default_max_db_size")]
    pub max_db_size: u64,
    #[serde(default = "default_cache_size")]
    pub page_cache_size: usize,
    #[serde(default = "default_versions_to_keep")]
    pub versions_to_keep: i32,
    #[serde(default = "default_use_mmap")]
    pub use_mmap: bool,
    #[serde(default = "default_use_compression")]
    pub use_compression: bool,
}

fn default_page_size() -> u64 { 8192 }
fn default_max_db_size() -> u64 { 8_000 * 1024 * 1024 * 1024 }
fn default_cache_size() -> usize { 2048 }
fn default_versions_to_keep() -> i32 { 2 }
fn default_use_mmap() -> bool { true }
fn default_use_compression() -> bool { true }

impl Default for StreamDbConfig {
    fn default() -> Self {
        Self {
            page_size: default_page_size(),
            max_db_size: default_max_db_size(),
            page_cache_size: default_cache_size(),
            versions_to_keep: default_versions_to_keep(),
            use_mmap: default_use_mmap(),
            use_compression: default_use_compression(),
        }
    }
}

// ============================================================================
// Gaming Metrics
// ============================================================================

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GamingMetrics {
    pub positions_sent: u64,
    pub positions_received: u64,
    pub audio_packets_sent: u64,
    pub audio_packets_received: u64,
    pub events_sent: u64,
    pub events_received: u64,
}

/// A peer with its address and latest network stats — for UI/recorder consumption.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PeerDetail {
    pub id: String,
    pub host: String,
    pub port: u16,
    pub stats: NetworkStats,
}

// ============================================================================
// DCF Node - Main Service
// ============================================================================

pub struct DcfNode {
    config: DcfConfig,
    node_id: String,
    udp_endpoint: Option<Arc<UdpEndpoint>>,
    peers: Arc<RwLock<Vec<String>>>,
    peer_map: Arc<RwLock<HashMap<String, (String, u16)>>>,
    metrics: Arc<RwLock<GamingMetrics>>,
    network_stats: Arc<RwLock<NetworkStats>>,
    /// Per-peer network stats (RTT/jitter), keyed by peer_id; updated on PONG.
    per_peer_stats: Arc<RwLock<HashMap<String, NetworkStats>>>,
    sequence_counter: AtomicU32,
    running: AtomicBool,
    role: Arc<RwLock<String>>,
    master_address: Arc<RwLock<String>>,
    tx_cache: Arc<Mutex<HashMap<String, TxContext>>>,
    /// Optional self-healing mesh runtime, attached via [`DcfNode::enable_mesh`].
    mesh: Arc<Mutex<Option<Arc<MeshRuntime>>>>,
}

#[derive(Debug, Clone)]
struct TxContext {
    tx_id: String,
    start_time: u64,
}

impl DcfNode {
    pub fn new(config: DcfConfig) -> Result<Self> {
        let node_id = config.node_id.clone()
            .unwrap_or_else(|| format!("node-{}", rand::random::<u32>() % 10000));
        
        let udp_endpoint = if config.transport == "UDP" || config.transport == "udp" {
            Some(Arc::new(UdpEndpoint::new(config.udp_port)?))
        } else {
            None
        };

        Ok(Self {
            config,
            node_id,
            udp_endpoint,
            peers: Arc::new(RwLock::new(vec![])),
            peer_map: Arc::new(RwLock::new(HashMap::new())),
            metrics: Arc::new(RwLock::new(GamingMetrics::default())),
            network_stats: Arc::new(RwLock::new(NetworkStats::default())),
            per_peer_stats: Arc::new(RwLock::new(HashMap::new())),
            sequence_counter: AtomicU32::new(0),
            running: AtomicBool::new(false),
            role: Arc::new(RwLock::new("p2p".to_string())),
            master_address: Arc::new(RwLock::new(String::new())),
            tx_cache: Arc::new(Mutex::new(HashMap::new())),
            mesh: Arc::new(Mutex::new(None)),
        })
    }

    pub fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn config(&self) -> &DcfConfig {
        &self.config
    }

    fn next_sequence(&self) -> u32 {
        self.sequence_counter.fetch_add(1, Ordering::SeqCst)
    }

    // ========================================================================
    // Peer Management - Matching Lisp API
    // ========================================================================

    pub fn add_peer(&self, peer_id: &str, host: &str, port: u16) -> Result<()> {
        let mut peers = self.peers.write();
        if !peers.contains(&peer_id.to_string()) {
            peers.push(peer_id.to_string());
        }
        self.peer_map.write().insert(peer_id.to_string(), (host.to_string(), port));
        log::info!("Peer added: {} at {}:{}", peer_id, host, port);
        Ok(())
    }

    pub fn remove_peer(&self, peer_id: &str) -> Result<()> {
        self.peers.write().retain(|p| p != peer_id);
        self.peer_map.write().remove(peer_id);
        log::info!("Peer removed: {}", peer_id);
        Ok(())
    }

    pub fn list_peers(&self) -> Vec<String> {
        self.peers.read().clone()
    }

    pub fn get_peer_info(&self, peer_id: &str) -> Option<(String, u16)> {
        self.peer_map.read().get(peer_id).cloned()
    }

    /// Peers with their host/port and latest per-peer network stats (RTT/jitter).
    pub fn list_peers_detailed(&self) -> Vec<PeerDetail> {
        let map = self.peer_map.read();
        let stats = self.per_peer_stats.read();
        self.peers
            .read()
            .iter()
            .map(|id| {
                let (host, port) = map.get(id).cloned().unwrap_or_default();
                PeerDetail {
                    id: id.clone(),
                    host,
                    port,
                    stats: stats.get(id).cloned().unwrap_or_default(),
                }
            })
            .collect()
    }

    /// Resolve the peer_id for a source address (matched by port, then ip). Shared by
    /// the PONG RTT path and the mesh runtime's PONG->mark_answered hook.
    pub(crate) fn peer_id_by_addr(&self, from: SocketAddr) -> Option<String> {
        let map = self.peer_map.read();
        map.iter()
            .find(|(_, (host, port))| {
                *port == from.port()
                    && (host == &from.ip().to_string()
                        || host == "localhost"
                        || host == "0.0.0.0")
            })
            .or_else(|| map.iter().find(|(_, (_, port))| *port == from.port()))
            .map(|(id, _)| id.clone())
    }

    /// Smoothed per-peer RTT in whole milliseconds (0 if unknown). Used by the mesh
    /// runtime for REPORT weights and status-line grouping.
    pub(crate) fn peer_rtt_ms(&self, id: &str) -> i32 {
        match self.per_peer_stats.read().get(id) {
            Some(s) if s.avg_rtt > 0.0 => (s.avg_rtt + 0.5) as i32,
            _ => 0,
        }
    }

    /// Resolve the peer_id for a source address and fold a fresh RTT sample into its
    /// per-peer stats. Called from the PONG handler.
    fn record_peer_rtt(&self, from: SocketAddr, rtt_ms: f64) {
        if let Some(id) = self.peer_id_by_addr(from) {
            let mut stats = self.per_peer_stats.write();
            stats.entry(id).or_default().update_rtt(rtt_ms);
        }
    }

    /// Send a DCF-Mesh control payload (REPORT/ROLE) as a `MsgMesh` ProtoMessage.
    pub(crate) fn send_mesh(&self, payload: Vec<u8>, host: &str, port: u16) {
        if let Some(ep) = &self.udp_endpoint {
            let msg = ProtoMessage::new(msg_type::MESH, self.next_sequence(), payload);
            let _ = ep.send_message(&msg, host, port, false);
        }
    }

    /// As [`Self::send_mesh`], addressed by `SocketAddr` (master ROLE unicast).
    pub(crate) fn send_mesh_addr(&self, payload: Vec<u8>, addr: SocketAddr) {
        self.send_mesh(payload, &addr.ip().to_string(), addr.port());
    }

    /// Attach a self-healing mesh runtime. Spawn its loop via [`run_mesh_runtime`]
    /// after [`Self::start`]. `mode` is "p2p" | "auto" | "master".
    pub fn enable_mesh(&self, mode: &str, node_id: u16, master_peer: &str, group_thr: i32) {
        *self.mesh.lock() = Some(Arc::new(MeshRuntime::new(mode, node_id, master_peer, group_thr)));
    }

    /// The attached mesh runtime, if any.
    pub fn mesh(&self) -> Option<Arc<MeshRuntime>> {
        self.mesh.lock().clone()
    }

    // ========================================================================
    // Gaming API - Matching Lisp Punctim
    // ========================================================================

    /// Send position update (unreliable, high frequency)
    pub fn send_position(&self, player_id: &str, x: f32, y: f32, z: f32) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let position = Position::new(x, y, z);
        let msg = ProtoMessage::new(msg_type::POSITION, self.next_sequence(), position.encode());

        let peer_map = self.peer_map.read();
        for peer_id in self.peers.read().iter() {
            if let Some((host, port)) = peer_map.get(peer_id) {
                endpoint.send_message(&msg, host, *port, false)?;
            }
        }

        self.metrics.write().positions_sent += 1;
        log::debug!("Position sent for {}: ({}, {}, {})", player_id, x, y, z);
        Ok(())
    }

    /// Send audio packet (unreliable, priority)
    pub fn send_audio(&self, audio_data: &[u8]) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let msg = ProtoMessage::new(msg_type::AUDIO, self.next_sequence(), audio_data.to_vec());

        let peer_map = self.peer_map.read();
        for peer_id in self.peers.read().iter() {
            if let Some((host, port)) = peer_map.get(peer_id) {
                endpoint.send_message(&msg, host, *port, false)?;
            }
        }

        self.metrics.write().audio_packets_sent += 1;
        log::debug!("Audio packet sent ({} bytes)", audio_data.len());
        Ok(())
    }

    /// Send one encoded audio block over the mesh using the certified DCF-Audio L2
    /// framing: the codec output (<=124 bytes for one 20 ms block) is packetized into
    /// 17-byte DeModFrame CTRL frames, each shipped as an unreliable AUDIO message.
    /// The receiver feeds these payloads to [`reassemble_audio_payload`]. See
    /// `dcf_wire_codec::audio` and Documentation/DCF_AUDIO_SPEC.md.
    ///
    /// `channel` is the rendezvous channel placed in the frame `dst` field
    /// (`0xFFFF` = broadcast). Peers tuned to the same channel hear each other; this
    /// is pure `dst` semantics, so the emitted frames remain ordinary certified
    /// DeModFrames.
    pub fn send_audio_dcf(
        &self,
        codec_id: u8,
        encoded: &[u8],
        packet_id: u16,
        ts_us: u32,
        channel: u16,
    ) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let frames = audio::packetize(codec_id, encoded, packet_id, ts_us, 0, channel, 0)
            .map_err(|e| DcfError::InvalidMessage(format!("audio packetize failed: {:?}", e)))?;

        let peer_map = self.peer_map.read();
        for peer_id in self.peers.read().iter() {
            if let Some((host, port)) = peer_map.get(peer_id) {
                for f in &frames {
                    let msg = ProtoMessage::new(msg_type::AUDIO, self.next_sequence(), f.to_vec());
                    endpoint.send_message(&msg, host, *port, false)?;
                }
            }
        }

        self.metrics.write().audio_packets_sent += 1;
        log::debug!("DCF audio block sent: codec={} packet_id={} ({} frames)",
                    codec_id, packet_id, frames.len());
        Ok(())
    }

    /// Send one game message over the mesh using the certified DCF-Game L2 framing:
    /// the body (<=124 bytes) is packetized into 17-byte DeModFrame DATA frames, each
    /// shipped as a [`msg_type::GAME_DCF`] message. The receiver feeds these payloads to
    /// [`reassemble_game_payload`]. See `dcf_wire_codec::game` and DCF_GAME_SPEC.md.
    ///
    /// `src` is the sender's player id, placed in the frame `src` field so receivers can
    /// multiplex opponents (unlike `send_audio_dcf`, which leaves `src=0`). `channel` is
    /// the rendezvous channel in the frame `dst` field (`0xFFFF` = broadcast to the
    /// lobby). `reliable` requests the transport's ARQ (use it for EVENT/JOIN; leave it
    /// off for SNAPSHOT/INPUT, which are dead-reckoned). The descriptor `flags` mirror
    /// this so receivers see the intended delivery class.
    pub fn send_game_dcf(
        &self,
        msg_type_id: u8,
        body: &[u8],
        packet_id: u16,
        ts_us: u32,
        src: u16,
        channel: u16,
        reliable: bool,
    ) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let flags = if reliable {
            game::FLAG_RELIABLE | game::FLAG_ORDERED
        } else {
            0
        };
        let frames = game::packetize(msg_type_id, body, packet_id, ts_us, src, channel, flags)
            .map_err(|e| DcfError::InvalidMessage(format!("game packetize failed: {:?}", e)))?;

        let peer_map = self.peer_map.read();
        for peer_id in self.peers.read().iter() {
            if let Some((host, port)) = peer_map.get(peer_id) {
                for f in &frames {
                    let msg = ProtoMessage::new(msg_type::GAME_DCF, self.next_sequence(), f.to_vec());
                    endpoint.send_message(&msg, host, *port, reliable)?;
                }
            }
        }

        self.metrics.write().events_sent += 1;
        log::debug!("DCF game message sent: msg_type={} packet_id={} ({} frames, reliable={})",
                    msg_type_id, packet_id, frames.len(), reliable);
        Ok(())
    }

    /// Send game event (reliable, critical)
    pub fn send_game_event(&self, event_type: u8, data: &str) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let event = GameEvent::new(event_type, data);
        let msg = ProtoMessage::new(msg_type::GAME_EVENT, self.next_sequence(), event.encode());

        let peer_map = self.peer_map.read();
        for peer_id in self.peers.read().iter() {
            if let Some((host, port)) = peer_map.get(peer_id) {
                endpoint.send_message(&msg, host, *port, true)?;
            }
        }

        self.metrics.write().events_sent += 1;
        log::info!("Game event sent: {} {}", event_type, data);
        Ok(())
    }

    /// Send UDP message to specific peer
    pub fn send_udp(&self, data: &[u8], recipient: &str, reliable: bool, msg_type: u8) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let (host, port) = self.peer_map.read()
            .get(recipient)
            .cloned()
            .ok_or_else(|| DcfError::PeerNotFound(recipient.to_string()))?;

        let msg = ProtoMessage::new(msg_type, self.next_sequence(), data.to_vec());
        endpoint.send_message(&msg, &host, port, reliable)?;

        Ok(())
    }

    /// Send ping for RTT measurement
    pub fn send_ping(&self, peer_id: &str) -> Result<()> {
        let endpoint = self.udp_endpoint.as_ref()
            .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

        let (host, port) = self.peer_map.read()
            .get(peer_id)
            .cloned()
            .ok_or_else(|| DcfError::PeerNotFound(peer_id.to_string()))?;

        let msg = ProtoMessage::new(msg_type::PING, self.next_sequence(), vec![]);
        endpoint.send_message(&msg, &host, port, false)?;

        Ok(())
    }

    // ========================================================================
    // Lifecycle
    // ========================================================================

    pub fn start(&self) -> Result<()> {
        if let Some(endpoint) = &self.udp_endpoint {
            endpoint.start();
        }
        self.running.store(true, Ordering::SeqCst);
        log::info!("DCF Node started: {} on UDP port {}", self.node_id, self.config.udp_port);
        Ok(())
    }

    pub fn stop(&self) -> Result<()> {
        if let Some(endpoint) = &self.udp_endpoint {
            endpoint.stop();
        }
        self.running.store(false, Ordering::SeqCst);
        log::info!("DCF Node stopped: {}", self.node_id);
        Ok(())
    }

    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    // ========================================================================
    // Metrics
    // ========================================================================

    pub fn get_metrics(&self) -> GamingMetrics {
        self.metrics.read().clone()
    }

    pub fn get_network_stats(&self) -> NetworkStats {
        if let Some(endpoint) = &self.udp_endpoint {
            endpoint.get_stats()
        } else {
            self.network_stats.read().clone()
        }
    }

    pub fn get_full_metrics(&self) -> FullMetrics {
        let gaming = self.get_metrics();
        let network = self.get_network_stats();
        FullMetrics { gaming, network }
    }

    // ========================================================================
    // Transactions - Matching Lisp API
    // ========================================================================

    pub fn begin_transaction(&self, tx_id: &str) -> Result<()> {
        let context = TxContext {
            tx_id: tx_id.to_string(),
            start_time: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0),
        };
        self.tx_cache.lock().insert(tx_id.to_string(), context);
        log::debug!("Transaction started: {}", tx_id);
        Ok(())
    }

    pub fn commit_transaction(&self, tx_id: &str) -> Result<()> {
        if self.tx_cache.lock().remove(tx_id).is_none() {
            return Err(DcfError::Transaction(format!("Transaction not found: {}", tx_id)));
        }
        log::debug!("Transaction committed: {}", tx_id);
        Ok(())
    }

    pub fn rollback_transaction(&self, tx_id: &str) -> Result<()> {
        self.tx_cache.lock().remove(tx_id);
        log::debug!("Transaction rolled back: {}", tx_id);
        Ok(())
    }

    // ========================================================================
    // Status and Version - Matching Lisp API
    // ========================================================================

    pub fn status(&self) -> Status {
        Status {
            running: self.is_running(),
            mode: self.role.read().clone(),
            udp_port: self.config.udp_port,
            peer_count: self.peers.read().len(),
            udp_active: self.udp_endpoint.as_ref().map(|e| e.is_running()).unwrap_or(false),
        }
    }

    pub fn version() -> Version {
        Version {
            sdk_version: "2.2.0".to_string(),
            dcf_version: "5.0.0".to_string(),
            transport: "UDP".to_string(),
            protocol: "binary-protobuf".to_string(),
        }
    }

    // ========================================================================
    // Role Assignment
    // ========================================================================

    pub fn assign_role(&self, role: &str, master_address: &str) -> Result<()> {
        *self.role.write() = role.to_string();
        *self.master_address.write() = master_address.to_string();
        log::info!("Role assigned: {} (master: {})", role, master_address);
        Ok(())
    }

    pub fn get_role(&self) -> String {
        self.role.read().clone()
    }

    // ========================================================================
    // Benchmark - Matching Lisp API
    // ========================================================================

    pub async fn benchmark(&self, peer_id: &str, count: usize) -> Result<BenchmarkResult> {
        let mut rtts = Vec::with_capacity(count);
        
        for _ in 0..count {
            let start = Instant::now();
            self.send_ping(peer_id)?;
            tokio::time::sleep(Duration::from_millis(10)).await;
            let rtt = start.elapsed().as_secs_f64() * 1000.0;
            rtts.push(rtt);
        }

        let avg_rtt = if rtts.is_empty() { 0.0 } else { rtts.iter().sum::<f64>() / rtts.len() as f64 };
        let min_rtt = rtts.iter().cloned().fold(f64::INFINITY, f64::min);
        let max_rtt = rtts.iter().cloned().fold(0.0, f64::max);

        Ok(BenchmarkResult {
            peer_id: peer_id.to_string(),
            count,
            avg_rtt,
            min_rtt: if min_rtt.is_infinite() { 0.0 } else { min_rtt },
            max_rtt,
        })
    }
}

// ============================================================================
// Response Types
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FullMetrics {
    pub gaming: GamingMetrics,
    pub network: NetworkStats,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Status {
    pub running: bool,
    pub mode: String,
    pub udp_port: u16,
    pub peer_count: usize,
    pub udp_active: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Version {
    pub sdk_version: String,
    pub dcf_version: String,
    pub transport: String,
    pub protocol: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchmarkResult {
    pub peer_id: String,
    pub count: usize,
    pub avg_rtt: f64,
    pub min_rtt: f64,
    pub max_rtt: f64,
}

// ============================================================================
// DCF Events - For Logging
// ============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum DcfEvent {
    Send { msg_id: String, timestamp: i64, content_summary: String },
    Receive { msg_id: String, timestamp: i64, content_summary: String },
    Failure { error: String, timestamp: i64, context: String },
}

// ============================================================================
// gRPC Service Implementation
// ============================================================================

use crate::proto::dcf_service_server::DcfService;
use tonic::{Request, Response, Status as TonicStatus};

pub struct MyDcfService {
    node: Arc<DcfNode>,
}

impl MyDcfService {
    pub fn new(node: Arc<DcfNode>) -> Self {
        Self { node }
    }
}

#[tonic::async_trait]
impl DcfService for MyDcfService {
    async fn send_message(
        &self,
        request: Request<DcfMessage>,
    ) -> std::result::Result<Response<DcfResponse>, TonicStatus> {
        let msg = request.into_inner();
        
        // Log the message
        log::info!("Received message: {} at {}", msg.id, msg.timestamp);
        
        // Update metrics
        {
            let mut metrics = self.node.metrics.write();
            metrics.events_received += 1;
        }

        Ok(Response::new(DcfResponse {
            success: true,
            message: "Message received".to_string(),
        }))
    }

    async fn get_metrics(
        &self,
        _request: Request<Empty>,
    ) -> std::result::Result<Response<Metrics>, TonicStatus> {
        let gaming = self.node.get_metrics();
        let network = self.node.get_network_stats();
        
        Ok(Response::new(Metrics {
            sends: gaming.positions_sent as i64 + gaming.audio_packets_sent as i64 + gaming.events_sent as i64,
            receives: gaming.positions_received as i64 + gaming.audio_packets_received as i64 + gaming.events_received as i64,
            failures: network.packets_lost as i64,
        }))
    }

    async fn assign_role(
        &self,
        request: Request<RoleAssignment>,
    ) -> std::result::Result<Response<DcfResponse>, TonicStatus> {
        let assign = request.into_inner();
        
        self.node.assign_role(&assign.role, &assign.master_address)
            .map_err(|e| TonicStatus::internal(e.to_string()))?;

        Ok(Response::new(DcfResponse {
            success: true,
            message: "Role assigned".to_string(),
        }))
    }

    async fn report_peer(
        &self,
        request: Request<PeerInfo>,
    ) -> std::result::Result<Response<DcfResponse>, TonicStatus> {
        let peer = request.into_inner();
        
        // Parse address and add peer
        let parts: Vec<&str> = peer.address.split(':').collect();
        if parts.len() == 2 {
            let host = parts[0];
            let port: u16 = parts[1].parse().unwrap_or(DEFAULT_UDP_PORT);
            let peer_id = format!("peer-{}", Uuid::new_v4());
            
            self.node.add_peer(&peer_id, host, port)
                .map_err(|e| TonicStatus::internal(e.to_string()))?;
        }

        Ok(Response::new(DcfResponse {
            success: true,
            message: "Peer reported".to_string(),
        }))
    }
}

// ============================================================================
// Message Handler Trait
// ============================================================================

pub trait MessageHandler: Send + Sync {
    fn handle_position(&self, position: Position, from: SocketAddr);
    fn handle_audio(&self, data: &[u8], from: SocketAddr);
    fn handle_game_event(&self, event: GameEvent, from: SocketAddr);
    /// One DCF-Game L2 frame (a single 17-byte DeModFrame DATA frame). Feed `data` to
    /// a per-`src_id` [`game::GameReassembler`] via [`reassemble_game_payload`] to
    /// recover whole messages. Defaults to a no-op so existing handlers still compile.
    fn handle_game(&self, _data: &[u8], _from: SocketAddr) {}
}

/// Default handler that just logs messages
pub struct DefaultMessageHandler;

impl MessageHandler for DefaultMessageHandler {
    fn handle_position(&self, position: Position, from: SocketAddr) {
        log::debug!("Position from {}: {:?}", from, position);
    }

    fn handle_audio(&self, data: &[u8], from: SocketAddr) {
        log::debug!("Audio from {} ({} bytes)", from, data.len());
    }

    fn handle_game_event(&self, event: GameEvent, from: SocketAddr) {
        log::info!("Game event from {}: {:?}", from, event);
    }

    fn handle_game(&self, data: &[u8], from: SocketAddr) {
        log::debug!("Game frame from {} ({} bytes)", from, data.len());
    }
}

// ============================================================================
// UDP Receiver Task
// ============================================================================

/// Periodically ping every peer so per-peer RTT (`list_peers_detailed`) stays fresh.
/// Spawn alongside `run_udp_receiver`.
pub async fn run_ping_scheduler(node: Arc<DcfNode>, every: Duration) {
    while node.is_running() {
        for peer_id in node.list_peers() {
            let _ = node.send_ping(&peer_id);
        }
        tokio::time::sleep(every).await;
    }
}

/// Spawn the attached self-healing mesh runtime's tick loop (no-op if none). The
/// loop drives its own per-tick PINGs, so do not also run [`run_ping_scheduler`]
/// when the mesh runtime is active. Call after [`DcfNode::start`].
pub fn run_mesh_runtime(node: Arc<DcfNode>) {
    if let Some(m) = node.mesh() {
        tokio::spawn(async move { m.run(node).await });
    }
}

pub async fn run_udp_receiver(
    node: Arc<DcfNode>,
    handler: Arc<dyn MessageHandler>,
) -> Result<()> {
    let endpoint = node.udp_endpoint.as_ref()
        .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;
    
    let socket = endpoint.socket.clone();
    let mut buf = vec![0u8; 65536];

    while node.is_running() {
        match socket.recv_from(&mut buf) {
            Ok((size, from)) => {
                if size > 0 {
                    let data = &buf[..size];
                    match ProtoMessage::deserialize(data) {
                        Ok(msg) => {
                            handle_incoming_message(&node, &endpoint, &handler, msg, from).await;
                        }
                        Err(e) => {
                            log::warn!("Failed to deserialize message from {}: {}", from, e);
                        }
                    }
                }
            }
            Err(ref e) if e.kind() == io::ErrorKind::WouldBlock => {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
            Err(e) => {
                log::error!("UDP receive error: {}", e);
            }
        }
    }

    Ok(())
}

async fn handle_incoming_message(
    node: &DcfNode,
    endpoint: &UdpEndpoint,
    handler: &Arc<dyn MessageHandler>,
    msg: ProtoMessage,
    from: SocketAddr,
) {
    // Update receive stats
    {
        let mut stats = endpoint.stats.write();
        stats.packets_received += 1;
        stats.bytes_received += (17 + msg.payload.len()) as u64;
    }

    match msg.msg_type {
        msg_type::ACK => {
            if msg.payload.len() >= 4 {
                let seq = u32::from_be_bytes([
                    msg.payload[0], msg.payload[1], msg.payload[2], msg.payload[3]
                ]);
                endpoint.ack_received.lock().insert(seq, true);
                endpoint.reliable_packets.lock().remove(&seq);
            }
        }
        msg_type::PING => {
            let _ = endpoint.send_pong(msg.sequence, msg.timestamp, &from);
        }
        msg_type::PONG => {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_micros() as u64)
                .unwrap_or(0);
            let rtt = (now.saturating_sub(msg.timestamp)) as f64 / 1000.0;
            endpoint.stats.write().update_rtt(rtt);
            node.record_peer_rtt(from, rtt);
            // Feed the mesh runtime's liveness FSM.
            if let Some(m) = node.mesh() {
                if let Some(id) = node.peer_id_by_addr(from) {
                    m.mark_answered(&id);
                }
            }
        }
        msg_type::MESH => {
            // DCF-Mesh control (REPORT/ROLE) for the self-healing runtime.
            if let Some(m) = node.mesh() {
                m.handle_control(&msg.payload, from);
            }
        }
        msg_type::RELIABLE => {
            let _ = endpoint.send_ack(msg.sequence, &from);
            // Process payload based on embedded type if needed
        }
        msg_type::POSITION => {
            if let Ok(pos) = Position::decode(&msg.payload) {
                node.metrics.write().positions_received += 1;
                handler.handle_position(pos, from);
            }
        }
        msg_type::AUDIO => {
            // PRODUCTION FIX: Promoted to INFO level to ensure Shim traffic is visible in Docker logs
            log::info!("[Shim Ingress] Raw payload received: {} bytes from {}", msg.payload.len(), from);
            node.metrics.write().audio_packets_received += 1;
            handler.handle_audio(&msg.payload, from);
        }
        msg_type::GAME_EVENT => {
            if let Ok(event) = GameEvent::decode(&msg.payload) {
                node.metrics.write().events_received += 1;
                let _ = endpoint.send_ack(msg.sequence, &from);
                handler.handle_game_event(event, from);
            }
        }
        msg_type::GAME_DCF => {
            node.metrics.write().events_received += 1;
            handler.handle_game(&msg.payload, from);
        }
        _ => {
            log::debug!("Unknown message type {} from {}", msg.msg_type, from);
        }
    }
}

// ============================================================================
// Reliable Message Retransmission Task
// ============================================================================

pub async fn run_reliable_handler(node: Arc<DcfNode>) -> Result<()> {
    let endpoint = node.udp_endpoint.as_ref()
        .ok_or_else(|| DcfError::NotInitialized("UDP endpoint not initialized".to_string()))?;

    let timeout = Duration::from_millis(node.config.udp_reliable_timeout);
    let max_retries = node.config.retry_max;

    while node.is_running() {
        tokio::time::sleep(Duration::from_millis(100)).await;

        let mut packets_to_retry = vec![];
        let mut packets_to_remove = vec![];

        {
            let mut reliable = endpoint.reliable_packets.lock();
            let acks = endpoint.ack_received.lock();

            for (seq, packet) in reliable.iter_mut() {
                if acks.contains_key(seq) {
                    packets_to_remove.push(*seq);
                    continue;
                }

                if packet.sent_time.elapsed() > timeout {
                    if packet.attempts < max_retries {
                        packet.attempts += 1;
                        packet.sent_time = Instant::now();
                        packets_to_retry.push(packet.clone());
                    } else {
                        packets_to_remove.push(*seq);
                        endpoint.stats.write().packets_lost += 1;
                    }
                }
            }

            for seq in packets_to_remove {
                reliable.remove(&seq);
            }
        }

        for packet in packets_to_retry {
            let addr: std::result::Result<SocketAddr, _> = 
                format!("{}:{}", packet.host, packet.port).parse();
            if let Ok(addr) = addr {
                let _ = endpoint.send_raw(&packet.msg.serialize(), &addr);
                endpoint.stats.write().retransmits += 1;
            }
        }
    }

    Ok(())
}

// ============================================================================
// Initialization Helper
// ============================================================================

pub fn init_from_config_file(path: &str) -> Result<DcfNode> {
    let config_str = std::fs::read_to_string(path)
        .map_err(|e| DcfError::Config(format!("Failed to read config: {}", e)))?;
    
    let config: DcfConfig = if path.ends_with(".json") {
        serde_json::from_str(&config_str)
            .map_err(|e| DcfError::Config(format!("JSON parse error: {}", e)))?
    } else if path.ends_with(".toml") {
        toml::from_str(&config_str)
            .map_err(|e| DcfError::Config(format!("TOML parse error: {}", e)))?
    } else {
        return Err(DcfError::Config("Unknown config format".to_string()));
    };
    
    DcfNode::new(config)
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_position_encode_decode() {
        let pos = Position::new(1.0, 2.0, 3.0);
        let encoded = pos.encode();
        let decoded = Position::decode(&encoded).unwrap();
        
        assert!((decoded.x - 1.0).abs() < 0.001);
        assert!((decoded.y - 2.0).abs() < 0.001);
        assert!((decoded.z - 3.0).abs() < 0.001);
    }

    #[test]
    fn test_proto_message_serialize() {
        let payload = vec![1, 2, 3];
        let msg = ProtoMessage::new(msg_type::POSITION, 42, payload.clone());
        let serialized = msg.serialize();
        let deserialized = ProtoMessage::deserialize(&serialized).unwrap();
        
        assert_eq!(msg.msg_type, deserialized.msg_type);
        assert_eq!(msg.sequence, deserialized.sequence);
        assert_eq!(payload.len(), deserialized.payload.len());
    }

    #[test]
    fn test_game_event_encode_decode() {
        let event = GameEvent::new(5, "test-data");
        let encoded = event.encode();
        let decoded = GameEvent::decode(&encoded).unwrap();
        
        assert_eq!(decoded.event_type, 5);
        assert_eq!(decoded.data, "test-data");
    }

    #[test]
    fn test_network_stats_rtt_update() {
        let mut stats = NetworkStats::default();
        stats.update_rtt(10.0);
        stats.update_rtt(20.0);
        
        assert!(stats.avg_rtt > 0.0);
        assert!(stats.jitter >= 0.0);
    }

    #[test]
    fn test_config_default() {
        let config = DcfConfig::default();
        assert_eq!(config.udp_port, DEFAULT_UDP_PORT);
        assert_eq!(config.grpc_port, DEFAULT_GRPC_PORT);
        assert_eq!(config.transport, "UDP");
    }
}
