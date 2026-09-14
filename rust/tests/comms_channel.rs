// SPDX-License-Identifier: LGPL-3.0-only
// Copyright (c) 2026 DeMoD LLC.
//
// Headless proof of the Punctim comms client's core path: two SDK nodes exchange a
// text message and DCF-Audio over a shared frequency CHANNEL, while a third node tuned
// to a different channel receives neither. Uses only the public dcf-rust-sdk API (no
// Tauri, no cpal, no libopus) — the same calls the `client/` Rust core makes.

use dcf_rust_sdk::{
    audio, reassemble_audio_payload, run_udp_receiver, DcfConfig, DcfNode, GameEvent,
    MessageHandler, Position,
};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

const CHAT: u8 = 42;
const CH_A: u16 = 0x1234; // B's rendezvous channel
const CH_OTHER: u16 = 0x5678; // C's (mismatched) channel

/// Records what a node received, applying the channel rendezvous filter the client uses.
struct Rec {
    channel: u16,
    chats: Mutex<Vec<String>>,
    audio: Mutex<Vec<Vec<u8>>>,
    reasm: Mutex<audio::AudioReassembler>,
}
impl Rec {
    fn new(channel: u16) -> Arc<Self> {
        Arc::new(Self {
            channel,
            chats: Mutex::new(Vec::new()),
            audio: Mutex::new(Vec::new()),
            reasm: Mutex::new(audio::AudioReassembler::new()),
        })
    }
}
impl MessageHandler for Rec {
    fn handle_position(&self, _p: Position, _from: SocketAddr) {}

    fn handle_audio(&self, data: &[u8], _from: SocketAddr) {
        if data.len() != 17 {
            return;
        }
        // Frequency rendezvous: accept only frames addressed to our channel (or broadcast).
        let dst = ((data[6] as u16) << 8) | data[7] as u16;
        if dst != self.channel && dst != 0xFFFF {
            return;
        }
        if let Some(pkt) = reassemble_audio_payload(&mut self.reasm.lock().unwrap(), data) {
            self.audio.lock().unwrap().push(pkt.payload);
        }
    }

    fn handle_game_event(&self, e: GameEvent, _from: SocketAddr) {
        if e.event_type != CHAT {
            return;
        }
        // Messages are channel-tagged "<channel>\u{1}<text>"; filter to our channel.
        if let Some((ch, text)) = e.data.split_once('\u{1}') {
            if ch.parse::<u16>().ok() == Some(self.channel) {
                self.chats.lock().unwrap().push(text.to_string());
            }
        }
    }
}

fn node(port: u16, id: &str) -> Arc<DcfNode> {
    let cfg = DcfConfig {
        udp_port: port,
        host: "127.0.0.1".to_string(),
        node_id: Some(id.to_string()),
        ..Default::default()
    };
    Arc::new(DcfNode::new(cfg).expect("node init"))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn message_and_audio_rendezvous() {
    let (pa, pb, pc) = (47811u16, 47812, 47813);
    let a = node(pa, "A");
    let b = node(pb, "B");
    let c = node(pc, "C");
    a.start().unwrap();
    b.start().unwrap();
    c.start().unwrap();

    // A broadcasts to both B and C; only the channel decides who actually "hears" it.
    a.add_peer("B", "127.0.0.1", pb).unwrap();
    a.add_peer("C", "127.0.0.1", pc).unwrap();

    let rb = Rec::new(CH_A); // tuned
    let rc = Rec::new(CH_OTHER); // mistuned
    tokio::spawn(run_udp_receiver(b.clone(), rb.clone() as Arc<dyn MessageHandler>));
    tokio::spawn(run_udp_receiver(c.clone(), rc.clone() as Arc<dyn MessageHandler>));
    tokio::time::sleep(Duration::from_millis(150)).await;

    // text message on CH_A
    a.send_game_event(CHAT, &format!("{}\u{1}hello jammers", CH_A)).unwrap();

    // DCF-Audio on CH_A — small opaque payload (3 frames/packet) so the test is robust;
    // the codec is irrelevant to the framing/channel path being proven.
    let payload = vec![0xDEu8, 0xAD, 0xBE, 0xEF, 0x12, 0x34];
    for pid in 0..3u16 {
        a.send_audio_dcf(audio::CODEC_PCM_DIAG, &payload, pid, pid as u32 * 20_000, CH_A)
            .unwrap();
    }
    tokio::time::sleep(Duration::from_millis(400)).await;

    // B (tuned to CH_A) hears the message and all three audio packets.
    assert_eq!(
        rb.chats.lock().unwrap().as_slice(),
        &["hello jammers".to_string()],
        "tuned node should receive the channel message"
    );
    {
        let got = rb.audio.lock().unwrap();
        assert_eq!(got.len(), 3, "tuned node should reassemble 3 audio packets");
        assert_eq!(got[0], payload, "audio payload must round-trip over the wire");
    }

    // C (different channel) hears nothing — the handshakeless rendezvous gates it.
    assert!(rc.chats.lock().unwrap().is_empty(), "mistuned node must not receive the message");
    assert!(rc.audio.lock().unwrap().is_empty(), "mistuned node must not receive the audio");

    a.stop().unwrap();
    b.stop().unwrap();
    c.stop().unwrap();
}
