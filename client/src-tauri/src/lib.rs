// SPDX-License-Identifier: LGPL-3.0-only
// Copyright (c) 2026 DeMoD LLC.
//! Punctim communications client — Tauri core. Bridges the dcf-rust-sdk node
//! (mesh/transport/peers), the certified dcf-wire-codec (wire + DCF-Audio), and cpal
//! audio I/O to a web UI. Connect | Peers | Messages | Jam | Wire over the
//! frequency-channel rendezvous.

mod audio;
mod channel;
mod engine;
mod game;
mod sync;
#[cfg(feature = "audio")]
mod recorder;

use dcf_rust_sdk::{run_ping_scheduler, run_udp_receiver, DcfConfig, DcfNode, MessageHandler};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, State};

#[derive(Default)]
pub struct AppState {
    node: Mutex<Option<Arc<DcfNode>>>,
    handler: Mutex<Option<Arc<engine::UiHandler>>>,
    channel: Arc<AtomicU16>,
    jam: Mutex<Option<audio::Jam>>,
    game: Mutex<Option<Arc<game::GameSession>>>,
    radio: Mutex<Option<std::process::Child>>, // a locally-spawned `dcf-radio` broadcaster
}

#[derive(Deserialize)]
pub struct PeerArg {
    id: String,
    host: String,
    port: u16,
}

#[derive(Deserialize)]
pub struct ConnectArgs {
    node_id: String,
    host: String,
    port: u16,
    #[serde(default)]
    peers: Vec<PeerArg>,
}

#[tauri::command]
fn connect(app: AppHandle, state: State<AppState>, args: ConnectArgs) -> Result<String, String> {
    let cfg = DcfConfig {
        node_id: Some(args.node_id),
        host: args.host,
        udp_port: args.port,
        ..Default::default()
    };
    let node = Arc::new(DcfNode::new(cfg).map_err(|e| e.to_string())?);
    node.start().map_err(|e| e.to_string())?;
    for p in &args.peers {
        node.add_peer(&p.id, &p.host, p.port).map_err(|e| e.to_string())?;
    }
    let handler = engine::UiHandler::new(app.clone(), state.channel.clone());
    tauri::async_runtime::spawn(run_udp_receiver(
        node.clone(),
        handler.clone() as Arc<dyn MessageHandler>,
    ));
    tauri::async_runtime::spawn(run_ping_scheduler(node.clone(), Duration::from_secs(2)));
    let id = node.node_id().to_string();
    *state.node.lock() = Some(node);
    *state.handler.lock() = Some(handler);
    let _ = app.emit("status", "connected");
    Ok(id)
}

#[tauri::command]
fn disconnect(state: State<AppState>) {
    if let Some(j) = state.jam.lock().take() {
        audio::stop(j);
    }
    *state.game.lock() = None;
    if let Some(n) = state.node.lock().take() {
        let _ = n.stop();
    }
    *state.handler.lock() = None;
}

#[tauri::command]
fn add_peer(state: State<AppState>, id: String, host: String, port: u16) -> Result<(), String> {
    let node = state.node.lock().clone().ok_or("not connected")?;
    node.add_peer(&id, &host, port).map_err(|e| e.to_string())
}

#[derive(Deserialize)]
pub struct ChannelArgs {
    freq: Option<u16>,
    passphrase: Option<String>,
}

#[tauri::command]
fn set_channel(state: State<AppState>, args: ChannelArgs) -> u16 {
    let ch = match args.passphrase {
        Some(p) if !p.is_empty() => channel::channel_from_passphrase(&p),
        _ => args.freq.unwrap_or(channel::BROADCAST),
    };
    state.channel.store(ch, Ordering::Relaxed);
    ch
}

#[tauri::command]
fn send_message(state: State<AppState>, text: String) -> Result<(), String> {
    let ch = state.channel.load(Ordering::Relaxed);
    let node = state.node.lock().clone().ok_or("not connected")?;
    node.send_game_event(engine::CHAT_EVENT, &engine::tag_message(ch, &text))
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn start_jam(state: State<AppState>, codec: String) -> Result<(), String> {
    let node = state.node.lock().clone().ok_or("not connected")?;
    let codec_id = match codec.as_str() {
        "pm" => dcf_rust_sdk::audio::CODEC_FAUST_PM,
        "pcm" => dcf_rust_sdk::audio::CODEC_PCM_DIAG,
        _ => dcf_rust_sdk::audio::CODEC_OPUS,
    };
    let jam = audio::start(node, state.channel.clone(), codec_id)?;
    if let Some(h) = state.handler.lock().as_ref() {
        h.set_audio_sink(Some(jam.audio_sink()));
    }
    *state.jam.lock() = Some(jam);
    Ok(())
}

#[tauri::command]
fn stop_jam(state: State<AppState>) {
    if let Some(h) = state.handler.lock().as_ref() {
        h.set_audio_sink(None);
    }
    if let Some(j) = state.jam.lock().take() {
        audio::stop(j);
    }
}

#[tauri::command]
fn start_game(state: State<AppState>) -> Result<u16, String> {
    let node = state.node.lock().clone().ok_or("not connected")?;
    let node_id = node.node_id().to_string();
    let session = Arc::new(game::GameSession::new(node, state.channel.clone(), &node_id));
    let pid = session.player_id();
    *state.game.lock() = Some(session);
    Ok(pid)
}

#[tauri::command]
fn stop_game(state: State<AppState>) {
    *state.game.lock() = None;
}

#[tauri::command]
fn send_game_position(state: State<AppState>, x: f32, y: f32) -> Result<(), String> {
    let session = state.game.lock().clone().ok_or("start the game first")?;
    session.send_position(x, y)
}

#[tauri::command]
fn send_game_action(state: State<AppState>, text: String) -> Result<(), String> {
    let session = state.game.lock().clone().ok_or("start the game first")?;
    session.send_action(&text)
}

#[derive(Serialize)]
pub struct FrameJson {
    version: u8,
    frame_type: String,
    seq: u16,
    src: u16,
    dst: u16,
    payload: String,
    ts_us: u32,
}

#[tauri::command]
fn decode_frame(hex: String) -> Result<FrameJson, String> {
    let clean: String = hex.chars().filter(|c| c.is_ascii_hexdigit()).collect();
    if clean.len() != 34 {
        return Err("a DeModFrame is 17 bytes (34 hex chars)".into());
    }
    let mut buf = [0u8; 17];
    for i in 0..17 {
        buf[i] = u8::from_str_radix(&clean[i * 2..i * 2 + 2], 16).map_err(|e| e.to_string())?;
    }
    let f = dcf_wire_codec::Frame::decode(&buf).map_err(|e| format!("{:?}", e))?;
    Ok(FrameJson {
        version: f.version,
        frame_type: format!("{:?}", f.frame_type),
        seq: f.seq,
        src: f.src_id,
        dst: f.dst_id,
        payload: f.payload.iter().map(|b| format!("{:02X}", b)).collect(),
        ts_us: f.timestamp_us,
    })
}

#[tauri::command]
fn peers(state: State<AppState>) -> Result<serde_json::Value, String> {
    let node = state.node.lock().clone().ok_or("not connected")?;
    serde_json::to_value(node.list_peers_detailed()).map_err(|e| e.to_string())
}

#[tauri::command]
fn start_recording(state: State<AppState>, dir: String) -> Result<(), String> {
    #[cfg(feature = "audio")]
    {
        let jam = state.jam.lock();
        jam.as_ref()
            .ok_or("start a jam first")?
            .start_recording(std::path::PathBuf::from(dir))
    }
    #[cfg(not(feature = "audio"))]
    {
        let _ = (&state, &dir);
        Err("audio feature disabled in this build".into())
    }
}

#[tauri::command]
fn stop_recording(state: State<AppState>) -> Result<serde_json::Value, String> {
    #[cfg(feature = "audio")]
    {
        let jam = state.jam.lock();
        let res = jam
            .as_ref()
            .and_then(|j| j.stop_recording())
            .ok_or("not recording")?;
        Ok(serde_json::json!({
            "dir": res.dir, "master": res.master, "mixdown": res.mixdown, "tracks": res.tracks,
        }))
    }
    #[cfg(not(feature = "audio"))]
    {
        let _ = &state;
        Err("audio feature disabled in this build".into())
    }
}

#[tauri::command]
fn recording_status(state: State<AppState>) -> bool {
    #[cfg(feature = "audio")]
    {
        state.jam.lock().as_ref().map(|j| j.is_recording()).unwrap_or(false)
    }
    #[cfg(not(feature = "audio"))]
    {
        let _ = &state;
        false
    }
}

#[tauri::command]
fn metrics(state: State<AppState>) -> Result<serde_json::Value, String> {
    let node = state.node.lock().clone().ok_or("not connected")?;
    Ok(serde_json::json!({
        "node_id": node.node_id(),
        "running": node.is_running(),
        "channel": state.channel.load(Ordering::Relaxed),
    }))
}

#[derive(Deserialize)]
pub struct RadioArgs {
    bind: String,
    http: String,
    archive: String,
}

/// Broadcast this node's channel as a digital-radio station: spawn a local
/// `dcf-radio` (must be on PATH — it is in the `nix develop .#comms` shell) that
/// taps `bind`, serves HLS+DVR on `http`, and archives to `archive`.
#[tauri::command]
fn start_local_radio(state: State<AppState>, args: RadioArgs) -> Result<String, String> {
    let mut g = state.radio.lock();
    if g.as_mut().map(|c| c.try_wait().ok().flatten().is_none()).unwrap_or(false) {
        return Err("radio already running".into());
    }
    let child = std::process::Command::new("dcf-radio")
        .args(["--bind", &args.bind, "--http", &args.http, "--archive", &args.archive])
        .spawn()
        .map_err(|e| format!("could not start dcf-radio ({e}); is it on PATH?"))?;
    *g = Some(child);
    Ok(format!("broadcasting: tap {} -> http://{}/", args.bind, args.http))
}

#[tauri::command]
fn stop_local_radio(state: State<AppState>) {
    if let Some(mut c) = state.radio.lock().take() {
        let _ = c.kill();
        let _ = c.wait();
    }
}

#[tauri::command]
fn radio_status(state: State<AppState>) -> bool {
    let mut g = state.radio.lock();
    match g.as_mut() {
        Some(c) => c.try_wait().ok().flatten().is_none(), // still running?
        None => false,
    }
}

/// Open a URL (a station player / .m3u8) in the system browser, which has the best
/// HLS + DVR support — avoids bundling a player into the strict-CSP webview.
#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    let prog = "xdg-open";
    #[cfg(target_os = "macos")]
    let prog = "open";
    #[cfg(target_os = "windows")]
    let prog = "explorer";
    std::process::Command::new(prog)
        .arg(&url)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("open {url}: {e}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let _ = env_logger::try_init();
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            connect,
            disconnect,
            add_peer,
            set_channel,
            send_message,
            start_jam,
            stop_jam,
            start_game,
            stop_game,
            send_game_position,
            send_game_action,
            decode_frame,
            metrics,
            peers,
            start_recording,
            stop_recording,
            recording_status,
            start_local_radio,
            stop_local_radio,
            radio_status,
            open_url
        ])
        .run(tauri::generate_context!())
        .expect("error while running Punctim client");
}
