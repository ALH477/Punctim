<!-- SPDX-License-Identifier: LGPL-3.0-only -->
<!-- Copyright (c) 2026 DeMoD LLC. -->
<!--
  Punctim comms — redesigned shell. The thesis: the CHANNEL is the connection.
  The left spine makes "who can hear me right now" the always-true ambient fact —
  identity, the tuner, and a channel-grouped roster. The five views (Messages /
  Jam / Arena / Radio / Wire) ride the same handshakeless rendezvous. Visual
  language + tokens are ported from the DeMoD Punctim design system.
-->
<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { api, on, type UiMessage, type FrameJson, type PeerDetail, type RecordingResult,
  type UiGameSnapshot, type UiGameEvent, type UiAudioLevel } from '@ipc'

// Transport capabilities — the desktop build has everything; the web/WASM build
// turns off host-only features (multitrack recording, HLS radio, Opus). The
// shared shell hides those bits rather than offering dead controls.
const caps = api.capabilities

// ── navigation ───────────────────────────────────────────────────────────
type ViewId = 'messages' | 'jam' | 'arena' | 'radio' | 'wire'
const NAV: { id: ViewId; label: string; glyph: string }[] = [
  { id: 'messages', label: 'Messages', glyph: '✉' },
  { id: 'jam', label: 'Jam', glyph: '◉' },
  { id: 'arena', label: 'Arena', glyph: '⊹' },
  { id: 'radio', label: 'Radio', glyph: '❂' },
  { id: 'wire', label: 'Wire', glyph: '⌗' },
]
// Hide nav entries the transport can't serve (Radio is host-only).
const navItems = computed(() => NAV.filter((it) => it.id !== 'radio' || caps.radio))
const view = ref<ViewId>('messages')

// ── connection / identity ────────────────────────────────────────────────
const conn = reactive({ node_id: 'me', host: '0.0.0.0', port: 50551, connected: false, selfId: '' })
const peerForm = reactive({ id: '', host: '127.0.0.1', port: 50552 })
const addingPeer = ref(false)
const peers = ref<{ id: string; host: string; port: number }[]>([])
const peerDetails = ref<PeerDetail[]>([])
const err = ref('')
function fail(e: unknown) { err.value = String(e) }

// ── rendezvous channel ───────────────────────────────────────────────────
const chan = reactive({ freq: 1420, passphrase: '', active: 0xffff })
const tuner = reactive({ mode: chan.passphrase ? 'passphrase' : 'freq', freq: String(chan.freq), phrase: chan.passphrase })
const tunerFocus = ref(false)

// The certified CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) — identical to the
// codec. Derives a numeric rendezvous channel from a shared word (live preview).
function channelFromPassphrase(s: string): number {
  let crc = 0xffff
  for (let i = 0; i < (s || '').length; i++) {
    crc = (crc ^ ((s.charCodeAt(i) & 0xff) << 8)) & 0xffff
    for (let k = 0; k < 8; k++) crc = crc & 0x8000 ? ((crc << 1) ^  0x1021) & 0xffff : (crc << 1) & 0xffff
  }
  return crc & 0xffff
}
const tunerPreview = computed(() =>
  tuner.mode === 'passphrase' && tuner.phrase ? channelFromPassphrase(tuner.phrase) : parseInt(tuner.freq) || 0)
const tunerDirty = computed(() => tunerPreview.value !== chan.active)

// ── roster (channel == connection) ───────────────────────────────────────
// We have configured peers (+host/port) and live per-peer stats from the node.
// A peer with live stats while we're connected is "on your channel" (audible,
// with RTT); a configured peer we haven't heard from is "elsewhere on the mesh".
const detailMap = computed<Record<string, PeerDetail>>(() =>
  Object.fromEntries(peerDetails.value.map((d) => [d.id, d])))
const roster = computed(() =>
  peers.value.map((p) => {
    const d = detailMap.value[p.id]
    return {
      ...p,
      rtt: d?.stats.avg_rtt, jitter: d?.stats.jitter, rx: d?.stats.packets_received ?? 0,
      here: conn.connected && !!d,
    }
  }))
const rosterHere = computed(() => roster.value.filter((p) => p.here))
const rosterElsewhere = computed(() => roster.value.filter((p) => !p.here))
const tunedInCount = computed(() => rosterHere.value.length)

// ── messages ─────────────────────────────────────────────────────────────
interface ChatMsg extends UiMessage { ts: number; state: 'sent' | 'nobody' | null }
const chatText = ref('')
const messages = ref<ChatMsg[]>([])
const logRef = ref<HTMLElement | null>(null)
function fmtTime(ts: number) { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }

// ── jam (audio) ──────────────────────────────────────────────────────────
const jam = reactive({ codec: 'opus', running: false })
const CODECS = [{ id: 'opus', label: 'Opus' }, { id: 'pcm', label: 'PCM-diag' }, { id: 'pm', label: 'Faust-PM' }]
// In a transport without host codecs (web/WASM), only PCM-diag is available.
const codecs = computed(() => CODECS.filter((c) => c.id === 'pcm' || caps.opus))
// The node emits one audio-level tick per arriving packet (per source). We map
// each tick to a meter that decays — real signal where there is real traffic.
const levels = reactive({ self: 0, peers: {} as Record<number, number> })
const jamPeers = computed(() => Object.entries(levels.peers).map(([src, level]) => ({ src: Number(src), level })))
const rec = reactive({ on: false, dir: 'punctim-rec', result: null as RecordingResult | null })
const recState = computed<'idle' | 'recording'>(() => (rec.on ? 'recording' : 'idle'))

// ── arena (dot game) ─────────────────────────────────────────────────────
const ARENA_W = 10, ARENA_H = 7.5 // metres mapped onto the arena box
const game = reactive({
  running: false, playerId: 0,
  me: { x: ARENA_W / 2, y: ARENA_H / 2 },
  others: {} as Record<number, { x: number; y: number; t: number }>,
  events: [] as { src: number; text: string }[],
})
let lastPosSent = 0

// ── radio ────────────────────────────────────────────────────────────────
const radio = reactive({ on: false, bind: '0.0.0.0:7110', http: '127.0.0.1:8000', archive: 'punctim-radio', status: 'off' })

// ── wire inspector (local certified decode for byte-strip + validity) ─────
const W_SYNC = 0xd3, W_VER = 1
const W_TYPES: Record<number, string> = { 0: 'FData', 1: 'FAck', 2: 'FBeacon', 3: 'FCtrl' }
const FIELD_SPANS = [
  { from: 0, to: 0, color: 'var(--violet)', label: 'SYNC' },
  { from: 1, to: 1, color: 'var(--violet)', label: 'VER|TYPE' },
  { from: 2, to: 3, color: 'var(--turquoise)', label: 'SEQ' },
  { from: 4, to: 5, color: 'var(--green-valid)', label: 'SRC' },
  { from: 6, to: 7, color: 'var(--yellow-lamp)', label: 'DST' },
  { from: 8, to: 11, color: 'var(--white-soft)', label: 'PAYLOAD' },
  { from: 12, to: 14, color: 'var(--violet)', label: 'TS_US' },
  { from: 15, to: 16, color: 'var(--turquoise)', label: 'CRC16' },
]
function spanColor(i: number) { for (const s of FIELD_SPANS) if (i >= s.from && i <= s.to) return s.color; return 'var(--grid-empty)' }
function wcrc(b: number[], n: number) {
  let c = 0xffff
  for (let i = 0; i < n; i++) {
    c = (c ^ ((b[i] & 0xff) << 8)) & 0xffff
    for (let k = 0; k < 8; k++) c = c & 0x8000 ? ((c << 1) ^ 0x1021) & 0xffff : (c << 1) & 0xffff
  }
  return c
}
function wFromHex(s: string): number[] | null {
  s = (s || '').replace(/[\s:,]/g, '').replace(/0x/gi, '')
  if (s.length !== 34 || /[^0-9a-fA-F]/.test(s)) return null
  const w: number[] = []
  for (let i = 0; i < 17; i++) w.push(parseInt(s.substr(i * 2, 2), 16))
  return w
}
type WireFields = { err?: string; ftype?: number; seq?: number; src?: number; dst?: number; payload?: number[]; ts?: number; crc?: number }
function wDecode(w: number[]): WireFields {
  if (w.length !== 17) return { err: `length ${w.length} != 17` }
  if (w[0] !== W_SYNC) return { err: 'bad sync byte' }
  if (w[1] >> 4 !== W_VER) return { err: 'bad version nibble' }
  if (wcrc(w, 15) !== ((w[15] << 8) | w[16])) return { err: 'CRC mismatch' }
  return { ftype: w[1] & 0xf, seq: (w[2] << 8) | w[3], src: (w[4] << 8) | w[5], dst: (w[6] << 8) | w[7],
    payload: [w[8], w[9], w[10], w[11]], ts: (w[12] << 16) | (w[13] << 8) | w[14], crc: (w[15] << 8) | w[16] }
}
const hex2 = (n: number) => n.toString(16).toUpperCase().padStart(2, '0')
const hex4 = (n: number) => n.toString(16).toUpperCase().padStart(4, '0')

const wireHex = ref('D31302000001FFFF06020100010203CAD7')
const wireFrame = ref<number[] | null>(null)
const wireFields = ref<WireFields | null>(null)
const wireLog = ref<{ prompt?: string; text: string; role?: string }[]>([
  { text: 'wire inspector ready — paste 17 bytes of hex', role: 'dim' }, { prompt: '>', text: '' },
])
const wireValid = computed<boolean | null>(() => (wireFields.value ? !wireFields.value.err : null))
const wireBytes = computed<(number | null)[]>(() => {
  if (wireFrame.value) return wireFrame.value
  const hex = (wireHex.value || '').replace(/[^0-9a-fA-F]/g, '')
  return Array.from({ length: 17 }, (_, i) => { const p = hex.substr(i * 2, 2); return p.length === 2 ? parseInt(p, 16) : null })
})
const wireRows = computed(() => {
  const f = wireFields.value
  if (!f || f.err) return [] as [string, string][]
  return [
    ['type', W_TYPES[f.ftype!]],
    ['seq', '0x' + hex4(f.seq!)],
    ['src → dst (channel)', `0x${hex4(f.src!)} → 0x${hex4(f.dst!)}`],
    ['payload', f.payload!.map(hex2).join(' ')],
    ['ts_us', '0x' + f.ts!.toString(16).toUpperCase()],
    ['crc16', '0x' + hex4(f.crc!)],
  ] as [string, string][]
})
function decodeWire(h?: string) {
  const src = h !== undefined ? h : wireHex.value
  const w = wFromHex(src)
  if (!w) {
    wireLog.value = [...wireLog.value.slice(0, -1), { prompt: '>', text: 'decode → need exactly 17 bytes of hex', role: 'err' }, { prompt: '>', text: '' }]
    return
  }
  const f = wDecode(w)
  wireFrame.value = w; wireFields.value = f
  const msg = f.err ? 'REJECTED: ' + f.err : `VALID ${W_TYPES[f.ftype!]} ${hex4(f.src!)}→${hex4(f.dst!)}`
  wireLog.value = [...wireLog.value.slice(0, -1), { prompt: '>', text: 'decode → ' + msg, role: f.err ? 'err' : 'ok' }, { prompt: '>', text: '' }]
}
function wireExample() { const ex = 'D31012340001FFFFDEADBEEFAB12CDA963'; wireHex.value = ex; decodeWire(ex) }

// ── actions ──────────────────────────────────────────────────────────────
async function connect() {
  err.value = ''
  try {
    conn.selfId = await api.connect(conn.node_id, conn.host, conn.port, peers.value)
    conn.connected = true
    await applyChannel()
  } catch (e) { fail(e) }
}
async function disconnect() { try { await api.disconnect(); conn.connected = false } catch (e) { fail(e) } }
async function addPeer() {
  if (!peerForm.id) return
  try {
    await api.addPeer(peerForm.id, peerForm.host, peerForm.port)
    peers.value.push({ id: peerForm.id, host: peerForm.host, port: peerForm.port })
    peerForm.id = ''; peerForm.host = '127.0.0.1'; peerForm.port = 50552; addingPeer.value = false
  } catch (e) { fail(e) }
}
async function applyChannel() {
  try { chan.active = await api.setChannel(chan.passphrase ? null : chan.freq, chan.passphrase || null) } catch (e) { fail(e) }
}
function tune() {
  chan.passphrase = tuner.mode === 'passphrase' ? tuner.phrase : ''
  chan.freq = parseInt(tuner.freq) || 0
  applyChannel()
}
async function send() {
  const t = chatText.value.trim()
  if (!t) return
  try {
    await api.sendMessage(t)
    // No ack channel on the wire — mark "sent" if someone is tuned in, else flag
    // that nobody is listening (the channel is empty), rather than faking an ack.
    messages.value.push({ from: 'me', text: t, channel: chan.active, ts: Date.now(), state: tunedInCount.value ? 'sent' : 'nobody' })
    chatText.value = ''
    scrollLog()
  } catch (e) { fail(e) }
}
async function toggleJam() {
  try {
    if (jam.running) { await api.stopJam(); jam.running = false }
    else { await api.startJam(jam.codec); jam.running = true }
  } catch (e) { fail(e) }
}
async function toggleRec() {
  try {
    if (rec.on) { rec.result = await api.stopRecording(); rec.on = false }
    else { rec.result = null; await api.startRecording(rec.dir); rec.on = true }
  } catch (e) { fail(e) }
}
function onArenaMove(e: MouseEvent) {
  if (!game.running) return
  const box = (e.currentTarget as HTMLElement).getBoundingClientRect()
  game.me.x = ((e.clientX - box.left) / box.width) * ARENA_W
  game.me.y = ((e.clientY - box.top) / box.height) * ARENA_H
  const now = performance.now()
  if (now - lastPosSent < 50) return // throttle SNAPSHOTs to ~20 Hz
  lastPosSent = now
  api.sendGamePosition(game.me.x, game.me.y).catch(fail)
}
async function pingArena() { try { await api.sendGameAction(`ping from ${game.playerId}`) } catch (e) { fail(e) } }
async function startRadio() {
  try { radio.status = await api.startLocalRadio(radio.bind, radio.http, radio.archive); radio.on = true } catch (e) { fail(e) }
}
async function stopRadio() { try { await api.stopLocalRadio(); radio.on = false; radio.status = 'off' } catch (e) { fail(e) } }
function openRadio() { api.openUrl(`http://${radio.http.replace(/^0\.0\.0\.0/, '127.0.0.1')}/`).catch(fail) }
async function refreshPeers() { if (conn.connected) { try { peerDetails.value = await api.peers() } catch (e) { fail(e) } } }
function scrollLog() { requestAnimationFrame(() => { if (logRef.value) logRef.value.scrollTop = logRef.value.scrollHeight }) }

// Arena auto-joins the DCF-Game session while the view is open and we're online.
watch([() => view.value, () => conn.connected], async ([v, c]) => {
  if (v === 'arena' && c && !game.running) { try { game.playerId = await api.startGame(); game.running = true } catch (e) { fail(e) } }
  else if ((v !== 'arena' || !c) && game.running) { try { await api.stopGame() } catch { /* ignore */ } game.running = false }
})

onMounted(async () => {
  if (!caps.opus && jam.codec !== 'pcm') jam.codec = 'pcm' // web build: PCM-diag only
  await on<UiMessage>('message', (m) => { messages.value.push({ ...m, ts: Date.now(), state: null }); scrollLog() })
  await on<UiAudioLevel>('audio-level', (l) => { if (l.dir === 'tx') levels.self = 1; else levels.peers[l.src] = 1 })
  await on<UiGameSnapshot>('game-snapshot', (s) => { game.others[s.src] = { x: s.x, y: s.y, t: Date.now() } })
  await on<UiGameEvent>('game-event', (e) => { game.events.unshift({ src: e.src, text: e.text }); game.events.splice(8) })
  await on<string>('status', () => {})
  decodeWire()
  setInterval(refreshPeers, 2000)
  // Decay audio meters toward zero between packets.
  setInterval(() => {
    levels.self *= 0.6; if (levels.self < 0.02) levels.self = 0
    for (const k of Object.keys(levels.peers)) {
      const n = +k; levels.peers[n] *= 0.6
      if (levels.peers[n] < 0.02) delete levels.peers[n]
    }
  }, 130)
  // Drop opponents that have gone silent (>2 s without a snapshot).
  setInterval(() => {
    const cut = Date.now() - 2000
    for (const k of Object.keys(game.others)) if (game.others[+k].t < cut) delete game.others[+k]
  }, 1000)
})
</script>

<template>
  <div class="void-bg">
    <div class="chrome">
      <!-- title bar -->
      <div class="chrome-bar">
        <div class="tl"><span class="d coral" /><span class="d yellow" /><span class="d green" /></div>
        <div class="chrome-title">HYDRA<span class="tq">MESH</span> <span class="dim2">// comms</span></div>
        <span class="chrome-right">CH {{ chan.active }} · {{ tunedInCount }} reachable</span>
      </div>

      <div class="chrome-body">
        <!-- ───────────────── left spine ───────────────── -->
        <aside class="spine">
          <!-- identity -->
          <div class="spine-block id-block">
            <div class="id-row">
              <span class="lamp" :class="conn.connected ? 'online' : 'error'" />
              <div class="id-name">
                <div class="id-node">node {{ conn.connected ? conn.selfId || conn.node_id : conn.node_id }}</div>
                <div class="id-addr">{{ conn.host }}:{{ conn.port }} · {{ conn.connected ? 'online' : 'offline' }}</div>
              </div>
              <button class="btn btn-sm" :class="conn.connected ? 'btn-ghost' : 'btn-primary'" @click="conn.connected ? disconnect() : connect()">
                {{ conn.connected ? 'Leave' : 'Connect' }}
              </button>
            </div>
            <div v-if="!conn.connected" class="id-edit">
              <span class="ifield"><input class="iinput mono" v-model="conn.node_id" /></span>
              <span class="ifield w-port"><input class="iinput mono" v-model.number="conn.port" /></span>
            </div>
          </div>

          <!-- channel tuner -->
          <div class="spine-block">
            <div class="tuner">
              <div class="tuner-head">
                <span class="lbl">Rendezvous channel</span>
                <span class="tuned-pill" :class="{ live: conn.connected && tunedInCount }">
                  <span class="tp-dot" />{{ conn.connected ? `${tunedInCount} tuned in` : 'offline' }}
                </span>
              </div>
              <div class="tuner-readout">
                <span class="tuner-num">{{ chan.active }}</span>
                <span class="tuner-hex">0x{{ hex4(chan.active) }}</span>
              </div>
              <p class="tuner-note">No handshake — anyone on this number hears you. Same frequency = connected.</p>
              <div class="seg">
                <button v-for="m in [['freq','Frequency'],['passphrase','Passphrase']]" :key="m[0]"
                  class="seg-btn" :class="{ on: tuner.mode === m[0] }" @click="tuner.mode = m[0]">{{ m[1] }}</button>
              </div>
              <input class="tuner-input" :class="{ focus: tunerFocus }"
                :value="tuner.mode === 'freq' ? tuner.freq : tuner.phrase"
                @input="(e) => tuner.mode === 'freq' ? tuner.freq = (e.target as HTMLInputElement).value : tuner.phrase = (e.target as HTMLInputElement).value"
                @keyup.enter="tune" @focus="tunerFocus = true" @blur="tunerFocus = false"
                :placeholder="tuner.mode === 'freq' ? '1420' : 'shared word'" />
              <button class="tuner-go" :class="{ dirty: tunerDirty }" @click="tune">
                {{ tunerDirty ? `Tune → ${tunerPreview}` : 'Tuned' }}
              </button>
            </div>
          </div>

          <!-- roster -->
          <div class="roster">
            <div class="roster-head">
              <span class="lbl">Roster</span>
              <button class="link-btn" @click="addingPeer = !addingPeer">{{ addingPeer ? '× cancel' : '+ peer' }}</button>
            </div>
            <div v-if="addingPeer" class="peer-add">
              <input class="iinput mono" placeholder="peer id" v-model="peerForm.id" />
              <div class="row">
                <span class="ifield"><input class="iinput mono" placeholder="host" v-model="peerForm.host" /></span>
                <span class="ifield w-port"><input class="iinput mono" placeholder="port" v-model.number="peerForm.port" /></span>
              </div>
              <button class="btn btn-sm btn-secondary btn-block" @click="addPeer">Add to mesh</button>
            </div>

            <div v-if="conn.connected && rosterHere.length" class="roster-group here">◈ On your channel</div>
            <div v-for="p in rosterHere" :key="p.id" class="peer-row tuned">
              <span class="pdot green" />
              <div class="pmeta">
                <div class="pid">{{ p.id }}</div>
                <div class="psub">CH {{ chan.active }} · {{ (p.rtt ?? 0).toFixed(0) }}ms<template v-if="p.jitter != null"> · ±{{ p.jitter.toFixed(1) }}</template></div>
              </div>
              <span class="ptag">RX</span>
            </div>

            <div v-if="rosterElsewhere.length" class="roster-group">Elsewhere on the mesh</div>
            <div v-for="p in rosterElsewhere" :key="p.id" class="peer-row">
              <span class="pdot dim" />
              <div class="pmeta">
                <div class="pid">{{ p.id }}</div>
                <div class="psub">{{ conn.connected ? 'out of earshot' : `${p.host}:${p.port}` }}</div>
              </div>
            </div>
            <div v-if="!peers.length" class="roster-empty">no peers yet — add one and they appear with live RTT.</div>
          </div>

          <!-- nav -->
          <nav class="nav">
            <button v-for="it in navItems" :key="it.id" class="nav-item" :class="{ on: view === it.id }" @click="view = it.id">
              <span class="nav-glyph">{{ it.glyph }}</span><span>{{ it.label }}</span>
            </button>
          </nav>
        </aside>

        <!-- ───────────────── main stage ───────────────── -->
        <main class="stage">
          <p v-if="err" class="err">{{ err }}</p>

          <!-- MESSAGES -->
          <section v-if="view === 'messages'" class="vfill">
            <div class="panel-head">
              <div><h2>Messages</h2><p class="sub">Reliable DCF text · gated to channel {{ chan.active }}</p></div>
              <span class="badge" :class="tunedInCount ? 'success' : 'warning'"><span class="bdot" :class="{ glow: tunedInCount }" />{{ tunedInCount }} tuned in</span>
            </div>
            <div v-if="conn.connected && !tunedInCount" class="warnbar">
              <span class="wb-dot" />No peer is on CH {{ chan.active }}. You can transmit, but nobody will hear you until someone tunes in.
            </div>
            <div ref="logRef" class="chat-log">
              <div v-if="!messages.length" class="chat-empty">
                <span class="tq">// channel quiet</span><br />
                Tune to a frequency, and anyone on the same one appears here. No handshake, no invite — the channel <em>is</em> the room.
              </div>
              <div v-for="(m, i) in messages" :key="i" class="chat" :class="{ mine: m.from === 'me' }">
                <div class="chat-meta"><span class="chat-from">{{ m.from === 'me' ? 'you' : m.from }}</span><span class="chat-time">{{ fmtTime(m.ts) }}</span></div>
                <div class="chat-bubble">{{ m.text }}</div>
                <span v-if="m.from === 'me' && m.state" class="chat-ack" :class="m.state">{{ m.state === 'nobody' ? '! no listeners' : '· sent' }}</span>
              </div>
            </div>
            <div class="composer">
              <span class="ifield grow"><input class="iinput" :placeholder="conn.connected ? 'message…' : 'connect to transmit'" :disabled="!conn.connected" v-model="chatText" @keyup.enter="send" /></span>
              <button class="btn btn-primary" :disabled="!conn.connected || !chatText.trim()" @click="send">Send</button>
            </div>
          </section>

          <!-- JAM -->
          <section v-else-if="view === 'jam'">
            <div class="panel-head">
              <div><h2>Jam</h2><p class="sub">Real-time audio over CH {{ chan.active }} · {{ jam.codec.toUpperCase() }}</p></div>
              <span class="badge" :class="jam.running ? 'success' : 'neutral'"><span class="bdot" :class="{ glow: jam.running }" />{{ jam.running ? 'live' : 'muted' }}</span>
            </div>
            <div class="grid2">
              <div class="card">
                <div class="lbl">// Transport</div>
                <div class="codec-row">
                  <button v-for="c in codecs" :key="c.id" class="codec-btn" :class="{ on: jam.codec === c.id }" @click="jam.codec = c.id">{{ c.label }}</button>
                </div>
                <button class="btn btn-block" :class="jam.running ? 'btn-destructive' : 'btn-primary'" :disabled="!conn.connected" @click="toggleJam">{{ jam.running ? 'Stop jam' : 'Start jam' }}</button>
                <p v-if="!conn.connected" class="hint">Connect first to open the mic.</p>
              </div>
              <div class="card">
                <div class="lbl">// Live levels</div>
                <div class="levels">
                  <div class="meter-row"><span class="meter-lbl">you</span><div class="meter"><div class="meter-fill tq" :style="{ width: (jam.running ? levels.self : 0) * 100 + '%' }" /></div><span class="meter-tag">tx</span></div>
                  <div v-for="p in jamPeers" :key="p.src" class="meter-row"><span class="meter-lbl">#{{ p.src }}</span><div class="meter"><div class="meter-fill vt" :style="{ width: p.level * 100 + '%' }" /></div><span class="meter-tag">rx</span></div>
                  <p v-if="!jamPeers.length" class="hint">No audio on CH {{ chan.active }} yet.</p>
                </div>
              </div>
            </div>
            <div v-if="caps.recording" class="card mt">
              <div class="card-head">
                <div class="lbl">// Multitrack record · sample-synced · ffmpeg on stop</div>
                <span v-if="recState === 'recording'" class="rec-tick">● REC</span>
              </div>
              <div class="rec-row">
                <button class="btn" :class="rec.on ? 'btn-destructive' : 'btn-secondary'" :disabled="!conn.connected" @click="toggleRec">{{ rec.on ? '■ Stop & mux' : '● Record' }}</button>
                <span class="ifield grow"><input class="iinput mono" v-model="rec.dir" /></span>
                <span class="dim2">{{ rec.on ? `arming ${tunedInCount + 1} bit-exact Opus tracks` : 'idle' }}</span>
              </div>
              <div v-if="rec.result" class="rec-result">
                <div><span class="dim2">master&nbsp;&nbsp;</span><span class="ok">{{ rec.result.master || '(ffmpeg n/a — raw .opus tracks)' }}</span></div>
                <div><span class="dim2">mixdown</span> {{ rec.result.mixdown || '—' }}</div>
                <div><span class="dim2">tracks&nbsp;&nbsp;</span>{{ rec.result.tracks.length }} <span class="dim2">({{ rec.result.tracks.join(', ') }})</span></div>
              </div>
            </div>
          </section>

          <!-- ARENA -->
          <section v-else-if="view === 'arena'">
            <div class="panel-head">
              <div><h2>Arena</h2><p class="sub">Cursor = unreliable snapshot · ping = reliable event · CH {{ chan.active }}</p></div>
              <button class="btn btn-sm btn-secondary" :disabled="!conn.connected" @click="pingArena">Ping ⚡</button>
            </div>
            <div class="arena" :class="{ live: conn.connected }" @mousemove="onArenaMove">
              <template v-if="conn.connected">
                <div class="dot me" :style="{ left: (game.me.x / ARENA_W * 100) + '%', top: (game.me.y / ARENA_H * 100) + '%' }"><span class="dtag">you</span></div>
                <div v-for="(o, src) in game.others" :key="src" class="dot other" :style="{ left: (o.x / ARENA_W * 100) + '%', top: (o.y / ARENA_H * 100) + '%' }"><span class="dtag">#{{ src }}</span></div>
              </template>
              <div v-else class="arena-off">connect to join the arena</div>
            </div>
            <div class="arena-foot">
              <p class="sub flex1">Your position streams as unreliable, latest-wins <span class="tq">snapshots</span>. Ping sends a reliable, ordered <span class="vt">event</span> — both ride DCF-Game on CH {{ chan.active }}.</p>
              <div class="evlog">
                <span v-if="!game.events.length" class="dimmer">no events yet</span>
                <div v-for="(e, i) in game.events" :key="i"><span class="vt">#{{ e.src }}</span> {{ e.text }}</div>
              </div>
            </div>
          </section>

          <!-- RADIO -->
          <section v-else-if="view === 'radio' && caps.radio">
            <div class="panel-head">
              <div><h2>Radio</h2><p class="sub">Broadcast this mesh as per-channel HLS stations · live + DVR</p></div>
              <span class="badge" :class="radio.on ? 'success' : 'neutral'"><span class="bdot" :class="{ glow: radio.on }" />{{ radio.on ? 'on air' : 'off' }}</span>
            </div>
            <div class="grid2">
              <div class="card">
                <div class="lbl">// Broadcast</div>
                <div class="rfields">
                  <label class="rfield"><span class="lbl">Tap (UDP)</span><input class="iinput mono" v-model="radio.bind" /></label>
                  <label class="rfield"><span class="lbl">Serve (HTTP)</span><input class="iinput mono" v-model="radio.http" /></label>
                  <label class="rfield"><span class="lbl">Archive dir</span><input class="iinput mono" v-model="radio.archive" /></label>
                  <div class="row">
                    <button class="btn btn-block" :class="radio.on ? 'btn-destructive' : 'btn-primary'" :disabled="!conn.connected" @click="radio.on ? stopRadio() : startRadio()">{{ radio.on ? '■ Stop broadcast' : '● Broadcast' }}</button>
                    <button class="btn btn-ghost" @click="openRadio">Open ↗</button>
                  </div>
                  <p class="hint">{{ radio.status }}</p>
                </div>
              </div>
              <div class="card">
                <div class="lbl">// Stations (one per active channel)</div>
                <div class="stations">
                  <div v-if="tunedInCount" class="station active">
                    <div><div class="st-ch">CH {{ chan.active }}</div><div class="st-sub">{{ tunedInCount }} talker{{ tunedInCount > 1 ? 's' : '' }} · {{ rosterHere.map(p => p.id).join(', ') }}</div></div>
                    <button class="btn btn-sm btn-ghost" :disabled="!radio.on" @click="openRadio">Tune in ↗</button>
                  </div>
                  <p v-else class="hint">No talkers yet — stations appear as peers speak on each channel.</p>
                </div>
                <p class="hint mt2">The player (station list, scrub-back DVR) opens in the browser — best HLS support.</p>
              </div>
            </div>
          </section>

          <!-- WIRE -->
          <section v-else-if="view === 'wire'">
            <div class="panel-head">
              <div><h2>Wire</h2><p class="sub">DeModFrame inspector — the 17-byte wire quantum</p></div>
              <span class="badge" :class="wireValid === null ? 'neutral' : wireValid ? 'success' : 'error'"><span class="bdot" :class="{ glow: wireValid === true }" />{{ wireValid === null ? '—' : wireValid ? 'valid' : 'rejected' }}</span>
            </div>
            <div class="card">
              <div class="lbl">// Frame hex (34 chars)</div>
              <div class="wire-input">
                <span class="ifield grow"><input class="iinput mono" v-model="wireHex" @keyup.enter="decodeWire()" placeholder="D313…CAD7" /></span>
                <button class="btn btn-primary" @click="decodeWire()">Decode</button>
                <button class="btn btn-ghost" @click="wireExample">Example</button>
              </div>
              <!-- byte strip -->
              <div class="bytestrip">
                <div v-for="(b, i) in wireBytes" :key="i" class="bcell">
                  <div class="bval" :style="{ color: b === null ? 'var(--gray-dim)' : (i >= 15 && wireValid !== null ? (wireValid ? 'var(--green-valid)' : 'var(--coral-soft)') : spanColor(i)) }">{{ b === null ? '--' : hex2(b) }}</div>
                  <div class="bbar" :style="{ background: i >= 15 && wireValid !== null ? (wireValid ? 'var(--green-valid)' : 'var(--coral-soft)') : spanColor(i) }" />
                  <div class="bidx">{{ String(i).padStart(2, '0') }}</div>
                </div>
              </div>
              <div class="legend">
                <span v-for="s in FIELD_SPANS" :key="s.label" class="leg"><span class="leg-sw" :style="{ background: s.color }" />{{ s.label }}</span>
              </div>
              <!-- field rows -->
              <div class="wire-rows">
                <div v-for="[k, v] in wireRows" :key="k" class="wrow"><span class="dim2">{{ k }}</span><span class="wval">{{ v }}</span></div>
                <div v-if="!wireRows.length && wireFields && wireFields.err" class="wire-err">{{ wireFields.err }}</div>
              </div>
              <!-- terminal -->
              <div class="term">
                <div class="term-bar"><span class="d coral" /><span class="d yellow" /><span class="d green" /><span class="term-title">wire.log</span></div>
                <div class="term-body">
                  <div v-for="(ln, i) in wireLog" :key="i" class="term-line" :class="ln.role"><span v-if="ln.prompt" class="tq">{{ ln.prompt }} </span>{{ ln.text }}<span v-if="i === wireLog.length - 1" class="caret" /></div>
                </div>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  </div>
</template>

<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700;800&display=swap');

:root {
  color-scheme: dark;
  /* backgrounds */
  --void: #080810; --deep-space: #0C0C14; --surface: #101018; --elevated: #161620;
  --overlay: #1C1C28; --panel-blue: #1A1A2E; --grid-empty: #2A2A3E;
  --surface-card: var(--surface);
  /* accents */
  --turquoise: #00F5D4; --turquoise-dim: #00E5C7; --turquoise-muted: #00B89F;
  --violet: #8B5CF6; --violet-soft: #A78BFA;
  /* text */
  --white: #FFFFFF; --white-soft: #E0E0E0; --gray: #808080; --gray-dim: #404050; --ink: #080810;
  /* signal */
  --green: #39FF14; --green-valid: #4CFF82; --yellow: #FFE814; --yellow-lamp: #FFD94C;
  --coral: #FF3B5C; --coral-soft: #FF4C6A; --amber: #FF9500;
  /* borders */
  --border: #252530;
  /* radius */
  --radius-sm: 4px; --radius-md: 8px; --radius-lg: 12px; --radius-xl: 16px;
  /* effects */
  --shadow-md: 0 4px 16px rgba(0,0,0,0.4); --shadow-lg: 0 8px 32px rgba(0,0,0,0.5);
  --glow-turquoise: 0 0 20px rgba(0,245,212,0.30); --glow-coral: 0 0 16px rgba(255,59,92,0.35);
  --ring-focus: 0 0 0 3px rgba(0,245,212,0.15);
  --grad-signature: linear-gradient(135deg, var(--turquoise) 0%, var(--violet) 100%);
  --grad-signature-hover: linear-gradient(135deg, var(--turquoise-dim) 0%, var(--violet-soft) 100%);
  --font-sans: "Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: "JetBrains Mono", "Fira Code", "SF Mono", ui-monospace, monospace;
}
@keyframes dm-pulse-glow { 0%,100% { box-shadow: 0 0 8px currentColor } 50% { box-shadow: 0 0 20px currentColor } }
@keyframes dm-blink { 0%,49% { opacity: 1 } 50%,100% { opacity: 0 } }
@media (prefers-reduced-motion: reduce) { * { animation-duration: .01ms !important; animation-iteration-count: 1 !important } }

* { margin: 0; padding: 0; box-sizing: border-box }
body { background: var(--void); color: var(--white-soft); font-family: var(--font-sans); -webkit-font-smoothing: antialiased }
::selection { background: var(--turquoise); color: var(--ink) }
input { font-family: inherit }
button { font-family: inherit }

.void-bg { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; background: linear-gradient(160deg, var(--deep-space) 0%, var(--void) 100%) }

/* ── window chrome ── */
.chrome { width: 1160px; max-width: 100%; height: 800px; max-height: calc(100vh - 48px); border: 1px solid var(--border); border-radius: var(--radius-xl); overflow: hidden; background: var(--surface); box-shadow: var(--shadow-lg), var(--glow-turquoise); display: flex; flex-direction: column }
.chrome-bar { display: flex; align-items: center; gap: 14px; padding: 13px 18px; background: var(--void); border-bottom: 1px solid var(--border) }
.tl, .term-bar { display: flex; gap: 8px; align-items: center }
.d { width: 12px; height: 12px; border-radius: 50% } .d.coral { background: var(--coral) } .d.yellow { background: var(--yellow-lamp) } .d.green { background: var(--green-valid) }
.chrome-title { flex: 1; min-width: 0; font-family: var(--font-mono); font-weight: 800; letter-spacing: .06em; color: var(--white); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.chrome-right { font-family: var(--font-mono); font-size: 11px; color: var(--gray) }
.chrome-body { flex: 1; min-height: 0; display: flex }
.tq { color: var(--turquoise) } .vt { color: var(--violet-soft) } .ok { color: var(--green-valid) }
.dim2 { color: var(--gray); font-weight: 500 }

/* ── spine ── */
.spine { width: 288px; flex-shrink: 0; background: var(--deep-space); border-right: 1px solid var(--border); display: flex; flex-direction: column; height: 100%; overflow: hidden }
.spine-block { padding: 16px; border-bottom: 1px solid var(--border) }
.id-row { display: flex; align-items: center; gap: 10px }
.id-name { flex: 1; min-width: 0 }
.id-node { font-family: var(--font-mono); font-size: 13px; font-weight: 800; color: var(--white); letter-spacing: .04em }
.id-addr { font-family: var(--font-mono); font-size: 10px; color: var(--gray); white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.id-edit { display: flex; gap: 8px; margin-top: 12px }
.lamp { width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0 }
.lamp.online { background: var(--green-valid); box-shadow: 0 0 10px rgba(76,255,130,.9) }
.lamp.error { background: var(--coral); box-shadow: 0 0 10px rgba(255,59,92,.9) }

/* inputs */
.ifield { display: flex; align-items: center; background: var(--deep-space); border: 2px solid var(--border); border-radius: var(--radius-md); padding: 0 12px; transition: border-color .15s, box-shadow .15s }
.ifield:focus-within { border-color: var(--turquoise); box-shadow: var(--ring-focus) }
.ifield.grow { flex: 1 } .ifield.w-port { width: 84px }
.iinput { width: 100%; min-width: 0; background: transparent; border: none; outline: none; color: var(--white); font-size: 14px; padding: 10px 0 }
.iinput.mono { font-family: var(--font-mono); font-size: 13px; letter-spacing: .04em }
.iinput:disabled { color: var(--gray) }
.id-edit .ifield, .peer-add .ifield { padding: 0 10px } .id-edit .iinput, .peer-add .iinput { padding: 8px 0; font-size: 12px }

/* tuner */
.tuner { background: var(--panel-blue); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px }
.tuner-head { display: flex; align-items: center; justify-content: space-between }
.lbl { font-family: var(--font-mono); font-size: 10px; font-weight: 700; letter-spacing: .2em; text-transform: uppercase; color: var(--gray) }
.tuned-pill { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; padding: 5px 10px; border-radius: var(--radius-sm); color: var(--white-soft); background: rgba(128,128,128,.15); border: 1px solid rgba(128,128,128,.3) }
.tuned-pill.live { color: var(--green); background: rgba(57,255,20,.15); border-color: rgba(57,255,20,.3) }
.tp-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor }
.tuned-pill.live .tp-dot { box-shadow: 0 0 6px currentColor }
.tuner-readout { display: flex; align-items: baseline; gap: 8px; margin: 12px 0 4px }
.tuner-num { font-family: var(--font-mono); font-size: 34px; font-weight: 800; color: var(--turquoise); letter-spacing: .02em; line-height: 1; text-shadow: 0 0 18px rgba(0,245,212,.4) }
.tuner-hex { font-family: var(--font-mono); font-size: 11px; color: var(--gray) }
.tuner-note { font-family: var(--font-mono); font-size: 10px; color: var(--gray); margin: 0 0 14px; line-height: 1.5 }
.seg { display: flex; gap: 4px; margin-bottom: 10px; background: var(--deep-space); padding: 3px; border-radius: var(--radius-sm) }
.seg-btn { flex: 1; padding: 6px 0; cursor: pointer; font-family: var(--font-mono); font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; border: none; border-radius: 3px; background: transparent; color: var(--gray) }
.seg-btn.on { background: var(--elevated); color: var(--turquoise) }
.tuner-input { width: 100%; background: var(--deep-space); border: 2px solid var(--border); border-radius: var(--radius-md); padding: 10px 12px; color: var(--white); font-family: var(--font-mono); font-size: 13px; outline: none; transition: border-color .15s, box-shadow .15s }
.tuner-input.focus { border-color: var(--turquoise); box-shadow: var(--ring-focus) }
.tuner-go { width: 100%; margin-top: 10px; padding: 9px 0; cursor: pointer; font-family: var(--font-mono); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; font-size: 12px; border-radius: var(--radius-md); border: 2px solid var(--turquoise); background: transparent; color: var(--turquoise) }
.tuner-go.dirty { border-color: transparent; background: var(--grad-signature); color: var(--ink); box-shadow: 0 4px 12px rgba(0,245,212,.3) }

/* roster */
.roster { flex: 1; overflow: auto; padding: 14px 12px; min-height: 0 }
.roster-head { display: flex; align-items: center; justify-content: space-between; padding: 0 4px 10px }
.link-btn { font-family: var(--font-mono); font-size: 11px; color: var(--turquoise); background: none; border: none; cursor: pointer; letter-spacing: .06em }
.peer-add { display: flex; flex-direction: column; gap: 8px; padding: 10px; margin-bottom: 10px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md) }
.peer-add .row { display: flex; gap: 6px }
.roster-group { font-family: var(--font-mono); font-size: 9px; font-weight: 700; letter-spacing: .2em; text-transform: uppercase; color: var(--gray); padding: 12px 4px 6px }
.roster-group.here { color: var(--green-valid); padding-top: 0 }
.peer-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: var(--radius-md); border: 1px solid transparent }
.peer-row.tuned { background: rgba(57,255,20,.05); border-color: rgba(57,255,20,.16) }
.pdot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0 }
.pdot.green { background: var(--green-valid); box-shadow: 0 0 8px var(--green-valid) } .pdot.dim { background: var(--gray-dim) }
.pmeta { flex: 1; min-width: 0 }
.pid { font-family: var(--font-mono); font-size: 12px; color: var(--white); font-weight: 700; letter-spacing: .03em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.psub { font-family: var(--font-mono); font-size: 10px; color: var(--gray) }
.ptag { font-family: var(--font-mono); font-size: 9px; font-weight: 700; letter-spacing: .1em; color: var(--green-valid) }
.roster-empty { font-family: var(--font-mono); font-size: 11px; color: var(--gray); padding: 4px }

/* nav */
.nav { padding: 12px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 2px }
.nav-item { display: flex; align-items: center; gap: 11px; width: 100%; text-align: left; cursor: pointer; padding: 10px 12px; border-radius: var(--radius-md); border: none; border-left: 2px solid transparent; background: transparent; color: var(--white-soft); font-family: var(--font-mono); font-size: 13px; font-weight: 600; letter-spacing: .04em; transition: background .15s, color .15s }
.nav-item:hover { background: rgba(255,255,255,.04); color: var(--white) }
.nav-item.on { background: rgba(0,245,212,.10); color: var(--turquoise); border-left-color: var(--turquoise) }
.nav-glyph { font-size: 14px; width: 16px; text-align: center; opacity: .7 } .nav-item.on .nav-glyph { opacity: 1 }

/* ── stage ── */
.stage { flex: 1; padding: 28px; overflow: auto; background: var(--surface); min-width: 0 }
.stage > section { display: block } .vfill { display: flex; flex-direction: column; height: 100% }
.err { color: var(--coral); font-family: var(--font-mono); font-size: 12px; margin-bottom: 14px }
.panel-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 22px }
.panel-head h2 { font-size: 22px; font-weight: 800; color: var(--white); letter-spacing: -.01em }
.sub { margin-top: 6px; font-family: var(--font-mono); font-size: 12px; color: var(--gray); line-height: 1.6 }

/* buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; font-family: var(--font-mono); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; line-height: 1; border: 2px solid transparent; border-radius: var(--radius-lg); cursor: pointer; padding: 12px 24px; font-size: 14px; transition: all .15s; white-space: nowrap }
.btn:disabled { opacity: .45; cursor: not-allowed }
.btn-sm { padding: 8px 14px; font-size: 12px } .btn-block { width: 100%; display: flex }
.btn-primary { background: var(--grad-signature); color: var(--ink); box-shadow: 0 4px 12px rgba(0,245,212,.3) }
.btn-primary:not(:disabled):hover { background: var(--grad-signature-hover); box-shadow: 0 6px 20px rgba(0,245,212,.4); transform: translateY(-1px) }
.btn-secondary { background: transparent; color: var(--turquoise); border-color: var(--turquoise) }
.btn-secondary:not(:disabled):hover { background: rgba(0,245,212,.10); box-shadow: var(--glow-turquoise) }
.btn-ghost { background: transparent; color: var(--white-soft) } .btn-ghost:not(:disabled):hover { background: rgba(255,255,255,.05); color: var(--white) }
.btn-destructive { background: var(--coral); color: var(--white) } .btn-destructive:not(:disabled):hover { background: #E62E4D; box-shadow: var(--glow-coral) }

/* badges */
.badge { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; line-height: 1; padding: 5px 10px; border-radius: var(--radius-sm) }
.bdot { width: 6px; height: 6px; border-radius: 50%; background: currentColor } .bdot.glow { animation: dm-pulse-glow 1.4s infinite }
.badge.success { color: var(--green); background: rgba(57,255,20,.15); border: 1px solid rgba(57,255,20,.3) }
.badge.warning { color: var(--yellow); background: rgba(255,232,20,.15); border: 1px solid rgba(255,232,20,.3) }
.badge.error { color: var(--coral); background: rgba(255,59,92,.15); border: 1px solid rgba(255,59,92,.3) }
.badge.neutral { color: var(--white-soft); background: rgba(128,128,128,.15); border: 1px solid rgba(128,128,128,.3) }

/* cards / grid */
.card { background: var(--surface-card); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 18px } .card.mt { margin-top: 14px }
.card-head { display: flex; align-items: center; justify-content: space-between }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px }
.hint { font-family: var(--font-mono); font-size: 11px; color: var(--gray); margin-top: 12px; line-height: 1.6 } .hint.mt2 { margin-top: 14px }
.row { display: flex; gap: 8px; align-items: center } .mt { margin-top: 14px }

/* messages */
.warnbar { display: flex; gap: 10px; align-items: center; padding: 10px 14px; margin-bottom: 14px; border-radius: var(--radius-md); background: rgba(255,217,76,.08); border: 1px solid rgba(255,217,76,.3); font-family: var(--font-mono); font-size: 12px; color: var(--yellow-lamp); line-height: 1.5 }
.wb-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--yellow-lamp); box-shadow: 0 0 8px var(--yellow-lamp) }
.chat-log { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 10px; padding: 4px 2px; min-height: 0 }
.chat-empty { margin: auto; text-align: center; max-width: 360px; font-family: var(--font-mono); font-size: 13px; color: var(--gray); line-height: 1.7 }
.chat { display: flex; flex-direction: column; align-items: flex-start; gap: 3px } .chat.mine { align-items: flex-end }
.chat-meta { display: flex; align-items: baseline; gap: 8px }
.chat-from { font-family: var(--font-mono); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--violet-soft) } .chat.mine .chat-from { color: var(--turquoise) }
.chat-time { font-family: var(--font-mono); font-size: 10px; color: var(--gray-dim) }
.chat-bubble { max-width: 78%; padding: 9px 13px; border-radius: 12px; border-bottom-left-radius: 3px; font-size: 14px; line-height: 1.45; color: var(--white-soft); background: var(--elevated); border: 1px solid var(--border) }
.chat.mine .chat-bubble { background: rgba(0,245,212,.10); border-color: rgba(0,245,212,.28); border-bottom-left-radius: 12px; border-bottom-right-radius: 3px }
.chat-ack { font-family: var(--font-mono); font-size: 10px; letter-spacing: .05em; color: var(--gray) } .chat-ack.nobody { color: var(--yellow-lamp) }
.composer { display: flex; gap: 10px; margin-top: 16px; align-items: stretch }

/* jam */
.codec-row { display: flex; gap: 6px; margin: 14px 0 18px }
.codec-btn { flex: 1; padding: 9px 0; cursor: pointer; font-family: var(--font-mono); font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; border-radius: var(--radius-sm); border: 2px solid var(--border); background: var(--deep-space); color: var(--gray) }
.codec-btn.on { border-color: var(--turquoise); background: rgba(0,245,212,.12); color: var(--turquoise) }
.levels { display: flex; flex-direction: column; gap: 11px; margin-top: 16px }
.meter-row { display: flex; align-items: center; gap: 12px }
.meter-lbl { font-family: var(--font-mono); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--white-soft); width: 70px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.meter { flex: 1; height: 14px; background: var(--deep-space); border: 1px solid var(--border); border-radius: 7px; overflow: hidden; position: relative }
.meter-fill { position: absolute; inset: 0; transition: width .16s linear } .meter-fill.tq { background: var(--turquoise) } .meter-fill.vt { background: var(--violet) }
.meter-tag { font-family: var(--font-mono); font-size: 10px; color: var(--gray); width: 22px; text-align: right }
.rec-tick { font-family: var(--font-mono); font-size: 12px; color: var(--coral-soft) }
.rec-row { display: flex; gap: 10px; align-items: center; margin-top: 16px }
.rec-result { margin-top: 16px; padding: 14px; background: var(--deep-space); border: 1px solid var(--border); border-radius: var(--radius-md); font-family: var(--font-mono); font-size: 12px; color: var(--white-soft); line-height: 1.8 }

/* arena */
.arena { position: relative; height: 360px; background: var(--deep-space); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; cursor: not-allowed; background-image: linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px); background-size: 10% 13.3% }
.arena.live { cursor: crosshair }
.arena-off { position: absolute; inset: 0; display: grid; place-items: center; font-family: var(--font-mono); font-size: 13px; color: var(--gray) }
.dot { position: absolute; width: 14px; height: 14px; border-radius: 50%; transform: translate(-50%,-50%); transition: left .3s linear, top .3s linear }
.dot.me { background: var(--turquoise); box-shadow: 0 0 10px var(--turquoise) } .dot.other { background: var(--violet); box-shadow: 0 0 10px var(--violet) }
.dtag { position: absolute; left: 18px; top: -2px; font-family: var(--font-mono); font-size: 11px; color: var(--white-soft); white-space: nowrap }
.arena-foot { display: flex; gap: 12px; margin-top: 14px } .flex1 { flex: 1 }
.evlog { width: 220px; height: 96px; overflow: auto; background: var(--deep-space); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 10px; font-family: var(--font-mono); font-size: 11px; line-height: 1.6; color: var(--white-soft) }
.dimmer { color: var(--gray-dim) }

/* radio */
.rfields { display: flex; flex-direction: column; gap: 14px; margin-top: 16px }
.rfield { display: flex; flex-direction: column; gap: 6px }
.stations { display: flex; flex-direction: column; gap: 8px; margin-top: 16px }
.station { display: flex; align-items: center; justify-content: space-between; padding: 11px 13px; background: var(--deep-space); border: 1px solid var(--border); border-radius: var(--radius-md) } .station.active { border-color: var(--turquoise) }
.st-ch { font-family: var(--font-mono); font-size: 13px; color: var(--white); font-weight: 700 }
.st-sub { font-family: var(--font-mono); font-size: 11px; color: var(--gray) }

/* wire */
.wire-input { display: flex; gap: 10px; margin: 12px 0 20px }
.bytestrip { display: flex; flex-wrap: wrap; gap: 4px }
.bcell { width: 34px; text-align: center; font-family: var(--font-mono) }
.bval { background: var(--panel-blue); border-radius: 2px 2px 0 0; padding: 7px 0; font-size: 12px; font-weight: 700 }
.bbar { height: 3px; border-radius: 0 0 2px 2px } .bidx { font-size: 9px; color: var(--gray-dim); margin-top: 3px }
.legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 14px }
.leg { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 10px; letter-spacing: .1em; color: var(--white-soft) }
.leg-sw { width: 10px; height: 10px; border-radius: 2px }
.wire-rows { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 28px; margin-top: 20px; font-family: var(--font-mono); font-size: 12px }
.wrow { display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px } .wval { color: var(--white-soft); text-align: right }
.wire-err { font-family: var(--font-mono); font-size: 12px; color: var(--coral-soft) }
.term { margin-top: 18px; background: var(--void); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-md); font-family: var(--font-mono) }
.term-bar { padding: 10px 14px; background: var(--deep-space); border-bottom: 1px solid var(--border) }
.term-bar .d { width: 11px; height: 11px } .term-title { margin-left: 8px; font-size: 11px; letter-spacing: .08em; color: var(--gray) }
.term-body { padding: 16px 18px; font-size: 13px; line-height: 1.7 }
.term-line { white-space: pre-wrap; color: var(--white-soft) }
.term-line.dim { color: var(--gray-dim) } .term-line.ok { color: var(--green-valid) } .term-line.err { color: var(--coral-soft) }
.caret { display: inline-block; width: 8px; height: 15px; margin-left: 2px; vertical-align: text-bottom; background: var(--turquoise); animation: dm-blink 1.1s steps(1) infinite }
</style>
