# DCF / Punctim code review

**DeMoD LLC · C SDK v5.2.0 + Punctim v2.2.0 (incl. StreamDB C lib)**
Frank engineering review with root-cause diagnosis, plus shipped fixes. Items
marked *(verified)* were reproduced and checked in-container — SBCL for the Lisp,
a GF(2)/codec harness for the wire layer. Apply notes are in §Apply.

---

## Verdict

The **core C library is genuinely good**. The four modules that actually compile
and ship — `dcf_platform`, `dcf_error`, `dcf_ringbuf`, `dcf_connpool` — are
careful, lock-discipline-aware, atomics-correct in the SPSC ring, and backed by
a real `test_suite.c`. That is the spine and it is sound.

Everything *around* the spine has drifted, and the drift is the story:

1. **Three wire formats** coexist with no declared canonical one — the 17-byte
   `DeModFrame` (RF/Haskell/Lisp), a 17-byte proto-message UDP header, and the
   C SDK's larger `DCFMessageHeader`. Pick one quantum; make the rest adapters.
2. **The header→implementation gap.** Public headers declare `dcf_client`,
   `dcf_config`, `dcf_networking`, `dcf_serialization`, `dcf_redundancy`,
   `dcf_cancel` — but the build compiles none of them. The API surface promises
   roughly twice what the library delivers.
3. **Latent crashers in shipped code**: a connection-pool use-after-free, a
   StreamDB root-free NULL-deref, a serial transport that transmits uninitialized
   heap. These are in paths users hit, not in the quarantine pile.
4. **The Lisp validity predicate was vacuous.** `crc16-ccitt` returned `#xFFFF`
   for every input *(verified)* — the integrity check certified nothing and
   every cross-language frame was rejected.
5. **License was contradictory across the repo**, and CPack silently generated an
   MIT placeholder — a real distribution-licensing hazard. **(RESOLVED)** The repo
   is now unified to `LGPL-3.0-only` (top-level `LICENSE` is the LGPLv3 text, every
   manifest/flake and all source SPDX headers agree), CPack points at that real
   `LICENSE`, and GPL-3.0 stays scoped to the DOOM example only.
6. A peripheral pile of transports is written against APIs/ABIs that no longer
   exist and **cannot compile**; they should be quarantined, not shipped.

None of this is fatal and most of it is mechanical. The fixes ship alongside this
review. Below, severity-ordered.

---

## 2026-09-24 — DCF-Medium pass (dated findings)

Each entry below is dated 2026-09-24 and was verified against the tree on that date.
Open items are marked *Open*; items fixed in this pass say so.

**R1. Hosted CI had never run a job — root cause found and fixed 2026-09-25.** All 57
`.github/workflows/wire-certify.yml` runs on GitHub Actions (2026-06-10 → 2026-06-21) failed
at startup within seconds. The earlier "billing lock" framing was at best incomplete: the
true root cause was that **GitHub Actions was disabled at the repository level**
(`GET /repos/ALH477/Punctim/actions/permissions` returned `{"enabled": false}`), which is
why no run was even triggered after 2026-06-21 — and the repo is public, so standard
GitHub-hosted runners are free regardless. The workflow was also invalid YAML from commit
`70beec5` until this pass fixed it. Actions was re-enabled on 2026-09-25, and the workflow
**executed steps for the first time** that day (run `36082394985`, `workflow_dispatch`):
**25 of 26 jobs green**. The one failure, `certify-python`, was not in the certification
itself — all 246 vectors passed — but in a later step, `wirelab_mcp.py --selftest`, which
broke on an unpinned dependency: `pip install mcp` now resolves to mcp 2.x, which renamed
`FastMCP` to `MCPServer`. Fixed in `bd771db` (made the MCP SDK import optional so the
certification path no longer depends on that third-party API, plus pinned `mcp<2`) and
`d6764ec` (same pin for `langgraph_agents`, whose `Server.list_tools` also vanished in 2.x).
`ci.yml` is now 4/4 green (run `36083085188`) and `docs.yml` is green (run `36082816317`).
Hosted CI is running and green as of today — but this is its first day running, not a
track record; `make ci-local` and `.github/LOCAL_CI_RESULTS.md` remain the local
certification path. New jobs this pass: `certify-medium` and `io-matrix`. *Resolved
2026-09-25.*

**R2. Lua never checks the 246 golden vectors.** `GUI/wirelab.lua` self-certifies on load
against its own anchors only — `crc16("123456789") = 0x29B1` and one example frame ending
`…CDA963` — and names `golden_vectors.json` only in a comment; `lua/selftest.lua` checks
embedded audio example frames plus the same anchor. Nothing in `lua/` or `GUI/` reads the
JSON, so the README tier table's "each certifying all 246 vectors" overstated Lua
(corrected there). Fix: a Lua wire cert that parses the JSON, as `lisp/src/wire.lisp`
does for Lisp. *Open.*

**R3. The generated C headers for text / SSTV / snake omit the largest cases.** The generated
headers bound a case to `payload[128]` and `frames[33][17]`, so the C certs never see the
multi-frame rails: `codec/text_vectors.gen.h` carries **7 of 9** framing cases in
`text_vectors.json` (omits 257 B and the 4092 B max), `codec/sstv_vectors.gen.h` **6 of 9**
(omits 257, 5000 and the 8188 B max), `codec/snake_vectors.gen.h` **6 of 10** (omits 784,
1328, 2048 and the 8188 B max); reassembly is complete (4/4 each). The C tests admit it
("the 4092 B rail is certified by Python + Rust"), but "certified across C/Rust/Python"
holds for C only up to 124 B. Fix: emit the large cases as per-case arrays. *Open.*

**R4. SuperPack `unpack` type/version-nibble divergence — fixed in this pass (`43bebe0`).**
The wire gate is sync + version nibble 1 + CRC; the type nibble is not gated (the
246-vector encode basis carries types 4 and 8). Rust `codec/src/superpack.rs::unpack`
re-validated the rebuilt inner frames with `Frame::decode` → `FrameType::from_nibble`,
which rejects type nibbles 4–15 that Python and C accept (and never checked the inner
version nibble); it now applies the gate directly. C
`codec/demod_superpack.h::dcf_superpack_unpack` used `dcf_frame_valid`, which skips the
version nibble; it now re-checks each rebuilt core with `dcf_superpack_core`. Pinned by the
medium certificate (156 → 162 cases): `udp_bare` `lone_type8` / `pair_type4_type15`,
`l2eth` `three_frames_with_type8`, and reserved-type cases in stream / hex / udp_proto.

**R5. SuperPack `unpack` elsewhere (read-only audit; not fixed, no local toolchain).**
(a) Haskell `haskell/src/DCF/Transport/SuperPack.hs::unpackSuper` re-checks with
`decodeFrame`, whose `nibbleToFrameType` rejects types 4–15 and which never checks the
version nibble — so `packSuper` (type-agnostic `frameCore`) then `unpackSuper` fails for a
reserved-type frame. Fix: re-check with `frameCore`. *Open.* (b) Lua
`lua/dcf_superpack.lua::M.unpack` does not re-check the rebuilt frames at all, so an inner
version nibble ≠ 1 passes (the CRC is recomputed on rebuild; version edge case only).
*Open.* (c) Lisp (`unpack-super` → `decode-frame`, `lisp/src/wire.lisp`), Kotlin
(`SuperPack.unpack` → `Frame.decode`) and Swift (`unpackSuper` → `decode`) check sync +
version + CRC and ignore the type — matching Python.

**R6. SDK frame decoders diverge from the wire gate (read-only; not fixed).** Rust
`Frame::decode` (`codec/frame.rs`) rejects types 4–15 via `FrameType::from_nibble`, and
neither it nor `Frame::is_valid` (length + sync + CRC; any type) checks the version
nibble. C `dcf_frame_decode` / `dcf_frame_valid` (`codec/demod_frame.h`) check sync + CRC
only and skip the version nibble. Any SDK path that inspects received frames through them
drops reserved types (Rust) or accepts a wrong-version frame (Rust, C);
`codec-wasm/src/lib.rs::decode_frame` inspects through `Frame::decode`, so the browser Wire
inspector shows a valid type-4..15 frame as rejected. The desktop Tauri client has the same
defect, not just the browser: `client/src-tauri/src/lib.rs:192` decodes via
`dcf_wire_codec::Frame::decode`. Every Rust adapter reassembler inherits the same gap by
inspecting through `Frame::decode` (`codec/src/{audio,text,sstv,game,snake,monitor,qkd}.rs`).
A third divergent decoder, not listed above: Lisp `lisp/src/punctim.lisp:355-362` checks
length + sync + CRC and **skips the version nibble** — the same defect as C. The medium
codecs and every DCF-Medium `punctim` CLI apply the gate directly and are unaffected (the
Lisp SDK's own binary is also named `punctim`, so read that as the five DCF-Medium CLIs
specifically, not every binary of that name). *Open.*

**R7. `python/modem/main.py` — the only live audio path — is non-conforming.** It sends its
own frame: a 15-byte header (`>BIQH`: type u8, seq u32, ts u64, len u16) + N payload bytes
+ CRC-8, with no `0xD3`, no version nibble, no CRC-16, through the shared
`acoustic_frame.encode_bits` bit layer. Now labelled non-conforming in its docstrings; its
`crc8` was also mislabelled "CRC-8/MAXIM" (it is the non-reflected poly-0x31 CRC, check
`0xA2`; MAXIM is reflected, `0xA1`). The port to the 17-byte frame is deferred to v0.2 and
should target the certified `afsk_bits` codec in `python/MCP/mediumlab_core.py`. *Open
(port).*

**R8. Dead `DCF_MODEM_AUDIO` option — removed in this pass.** `option(DCF_MODEM_AUDIO …)` in
`C_SDK/CMakeLists.txt` was consumed by nothing (no `if()`, no `#ifdef` anywhere) and no
PortAudio/ALSA backend exists. `DCF_MODEM_SPEC.md`, the README and CLAUDE.md now say the
C modem has no live-audio path.

**R9. HydraModem `aux_cable` comment — corrected in this pass.** `hydramodem/src/hydra_profile.c` called
the aux profile "4x faster than default" and said it "matches python/modem/acoustic_frame.py
aux-cable". It is 1.2× the baud (1200 vs 1000 baud; 0.290 s vs 0.356 s per conv-coded
frame) and shares only baud and preamble length with the AFSK profile (tones, sync, CRC
and FEC differ; they do not interoperate — `DCF_MODEM_SPEC.md` already said so). *Resolved
2026-09-25.*

**R10. Smaller doc corrections in this pass.** `DCF_STEAM_SPEC.md` claimed the GNS backend
runs in CI — no job builds or tests it (`nix build .#dcf-cpp-gns` builds only; the
`gns_loopback` ctest runs locally). `lua/dcf_transport.lua` pointed at a nonexistent
DCF_TALK_SPEC.md — now `DCF_FIELD_USE.md` / `SUPERPACK_SPEC.md` / `DCF_FEC_SPEC.md`.
`MineCraft/` now states that the datapack is a demonstration emitting no `0xD3` sync /
CRC-16 — non-conforming and uncertified. `Documentation/index.md` now lists every spec,
though the legacy `Documentation/Specs/*.markdown` set (including `export_compliance.markdown`,
which CLAUDE.md cites as normative) is not in the index.

**Found by `punctim sim`** (2026-09-24):

**R11. Pipe `lan` profile exceeds a 1500-B MTU.** `PROFILES["lan"]` in
`python/dcf/pipe/protocol.py` is a 1400-B chunk, nparity 16, W 16. The DCF-FEC wrap makes
each chunk `feclab_core.encode_message(bytes(1400), 16)` = **1521 B**; + the 6-B data-lane
header = **1527 B**; + 28 B IPv4/UDP = **1555 B** — so every `lan` chunk IP-fragments on UDP
(max unfragmented payload 1472 B) and does not fit one raw `l2eth` frame at all. Suggested
fix (not applied): `chunk_size` 1300 (→ 1419 + 6 = 1425 B), or nparity sized per block so
the wrapped chunk ≤ 1472 B. *Open.*

**R12. One SSTV image overflows the default outbound queue.** An 8188-B DCF-SSTV image is
1 + 2047 = **2048** `DATA` frames; `OutboundQueue` in `python/dcf/transport.py` (and
`punctim io --queue`) defaults to **256**, so one un-paced send sheds frames on any medium
slower than the producer. Senders must pace; `punctim sim` reports it as an EXACT burst row.
*Open.*

**R13. Verdict item 5's "GPL-3.0 stays scoped to the DOOM example only" did not hold —
mostly fixed in this pass.** An exhaustive licence sweep (every flake `meta.license`, every
language manifest, all 504 SPDX identifiers across 455 tracked files) found the core claim
sound — 502 of 504 SPDX headers say `LGPL-3.0-only`, the top-level `LICENSE` is the
canonical FSF LGPLv3 text, and CPack points at it — but five tracked docs contradicted it.
**Fixed 2026-09-25:** `Documentation/Specs/briefing.markdown`,
`Documentation/Specs/dcf_design_spec.markdown`, `Documentation/install_deps_README.markdown`
and `C_SDK/compilation-guide.markdown` declared GPL-3.0 (header *and* body prose) and now
declare `LGPL-3.0-only` throughout; `node/README.md` carried an **MIT** badge linking
`./LICENSE`, a file that does not exist, and now declares `LGPL-3.0-only` against the real
`../LICENSE`. `LICENSING.md` documented the `janus-c` GPL boundary thoroughly but never
mentioned `quanta` (built `gpl3Only`, `flake.nix:711-716`); it now carries a matching
subsection — nothing was ever vendored or linked, so that was a documentation gap, not a
leak. **Still open:** no GPLv3 text ships in the tree even though LGPLv3 incorporates it by
reference (FSF practice is `COPYING` beside `COPYING.LESSER`); `rust/flake.nix`
`packages.default` (L131) and `packages.container` (L134) carry no `meta` block at all;
`hydra-llm-interface/flake.nix` has no `meta.license` and its file header grants LGPL
"or (at your option) any later version" = `LGPL-3.0-or-later`, not `-only`; and
`hydra-llm-interface/src/rust/cargo.toml` has no `license` field (and is lowercase-named,
so cargo ignores it). *Open (the four items above).*

**DCF-Medium (new module) — status.** Spec `Documentation/DCF_MEDIUM_SPEC.md` (normative)
and a **162-case certificate** over seven families (stream 11, hex 8, udp_proto 21,
udp_bare 7, l2eth 5, hydra_symbols 74, afsk_bits 36) in `Documentation/medium_vectors.json`
(+ the `python/MCP/` copy and `codec/medium_vectors.gen.h`). Five `punctim` CLIs with one
contract (Python, C, Rust, Go, Node) and three codec-only ports of the five digital
families (C++, Java, Perl); the HydraModem symbol stream is proven against the real
`hydra_frame_build`; `make io-matrix` = 140 pass / 0 fail (every writer × reader over
file, stdio, UDP proto/bare and HydraModem WAV, plus a resync leg). Solid: the codecs and
the determinism rule for finite inputs. **Deferred (v0.2):** live tcp / serial / ws media;
a conforming live-audio port (R7); the `sdr_bytes` family (SDR is loopback-tested and
Python-only); `libhydramodem` linked into the C `punctim` (`hydra:impl=cffi` is
Python-only); WAV byte-determinism across DSP backends (certification stops at the
symbol/bit stream); Kotlin/Swift/Haskell/Lua/Lisp medium ports.

---

## CRITICAL — compile breakers, memory safety, wire correctness

> **Status (v0.3.0): all CRITICAL items below are RESOLVED.** C1–C6 are applied in
> source (the C SDK builds and the wire cert passes); C7–C9 were folded from
> `lisp/punctim-hotfix.lisp` into `lisp/src/punctim.lisp` with a load-time
> self-cert and the `certify-lisp` CI job. Descriptions are retained for history;
> see `CHANGELOG.md` and `Documentation/DCF_BACKLOG.md`.

**C1. Duplicate `DCFCmd` enum (compile breaker).** `dcf_types.h` and
`dcf_interface.h` both define `DCFCmd`, with different numeric values — one
starts at `DCF_CMD_UNKNOWN=0`, the other at `DCF_CMD_INIT=0`. `dcf_interface.h`
includes `dcf_client.h` → `dcf_types.h`, so the redefinition is guaranteed.
*Fix:* delete the copy in `dcf_interface.h`, include `dcf_types.h`. (patch,
verified to apply)

**C2. Two conflicting `ITransport` definitions (ABI hazard).**
`dcf_plugin_manager.h` defines `ITransport` as a 4-function-pointer struct; the
legacy plugins initialize it positionally. `dcf_types.h` defines `ITransport` as
an 11-member struct starting `void* ctx`. Any TU including both fails to compile;
worse, where only one is seen, function pointers land in the wrong slots and the
plugin "works" until it calls through a misaligned member. *Fix:* rename the
legacy 4-fn struct to `DCFTransportV1` (the v1 plugin ABI), keep the rich
`ITransport` (v2) in `dcf_types.h`, and migrate plugins with a one-token change.
The good UDP plugin is migrated in the patch as the exemplar; the loader casts on
`dlsym`.

**C3. `messages.pb-c.h` literal typo (compile breaker). (RESOLVED)** A stray article
once preceded a `uint8_t` parameter type in the committed protobuf header; the
shipping header now reads `const uint8_t *data` correctly.

**C4. Connection-pool health thread use-after-free.** With `test_on_idle` (the
default) the health thread is created in `start`, but `stop` joins only
`eviction_thread`. `destroy` then frees the pool while the health thread is still
reading `pool->running` → read of freed memory. *Fix:* track a `health_started`
flag, set it on successful create, and join it in `stop`. **Verbatim corrected
functions** (use these if the patch hunks reject):

```c
/* in the pool struct, beside `running`: */
atomic_bool health_started;

/* create(): */
atomic_init(&pool->running, true);
atomic_init(&pool->health_started, false);

/* start(): only mark started if the thread really launched */
if (pool->config.test_on_idle) {
    if (dcf_thread_create(&pool->health_thread, health_check_loop, pool) == 0) {
        atomic_store(&pool->health_started, true);
    }
}

/* stop(): drain both workers before destroy() can free the pool */
atomic_store(&pool->running, false);
dcf_thread_join(pool->eviction_thread);
if (atomic_load(&pool->health_started)) {
    dcf_thread_join(pool->health_thread);
    atomic_store(&pool->health_started, false);
}
```

**C5. StreamDB root-free NULL-deref.** In `remove_helper`, deleting the *last*
key frees the trie root and leaves `db->root` dangling; the next `insert`
dereferences it → segfault. *Fix:* after delete, if `db->root == NULL`, recreate
an empty root so the invariant "root is never NULL" holds. (patch)

**C6. Serial transport sends uninitialized heap *(memory disclosure)*.**
`serial_send` `malloc`s a COBS buffer, never encodes into it, then `write`s it —
transmitting uninitialized heap contents (corruption + an info leak of whatever
was on the heap). *Fix:* drop the fake COBS and send the payload directly until
real framing exists; the patch does this and flags that framed receivers still
need a real delimiter. Do **not** ship the placeholder COBS.

**C7. Lisp `crc16-ccitt` returns `#xFFFF` for all input *(verified)*.** An inner
`let*` shadows the running `crc`; every iteration's work is discarded. The wire
integrity check was vacuous and every Haskell/Python/Rust frame was rejected.
*Fix:* `punctim-hotfix.lisp` F1. SBCL run: broken → `#xFFFF`; fixed → `0x29B1`
for "123456789" and `0xA963` for the exampleFrame body. The hotfix asserts both
anchors at load and refuses to load if the codec is wrong. See
`wire_quanta_category.md` §7 for why this is, precisely, a broken validity
equalizer (the Lisp encoder stops being a cone over the spec).

**C8. Lisp `dcf-stop` use-after-free.** It frees the StreamDB, then calls
`save-state` which writes to the freed handle. *Fix:* hotfix F4 reorders to
save → stop endpoint → flush+free → clear.

**C9. Lisp StreamDB result ABI mismatch *(crash)*.** `collect-streamdb-results`
walks the return of `streamdb_prefix_search` as a NUL-terminated pointer
**array**, but the C function returns a `Result*` **linked list** (`key`,
`key_len`, `value`, `value_size`, `next`). The Lisp reads garbage. *Fix:* hotfix
F8 adds the matching `defcstruct` and walks `next`.

---

## HIGH — wrong results, hangs, silent data loss

> **Status (v0.3.0, verified 2026-09-25): all HIGH items below are RESOLVED.** An
> out-of-tree build + `ctest` (21/21) confirmed H1–H2 are applied in source (the C
> SDK builds and the tests pass); the Lisp fixes (H3–H7) are folded into
> `lisp/src/punctim.lisp` and self-cert on load. Descriptions are retained for
> history.

**H1. UDP send silently truncates >512 B and returns success (data loss).**
`DOOM_udp_transport.c` `udp_send` caps at 512 and returns `true`, and sends from
an uninitialized `sockaddr_in`. *Fix:* reject oversize, `memset` the address,
require a full send. (patch)

**H2. Connection-pool `max_connections` race.** The create path checks
`total < max` under the lock, then unlocks to call the factory — two threads pass
the check and exceed the cap. *Fix:* reserve the count under the lock before
unlocking, roll back if the factory returns NULL:

```c
/* under the write lock, before unlocking to call the factory: */
if (pool->total_connections >= pool->config.max_connections) { /* fail/wait */ }
pool->total_connections++;
pool->active_connections++;
peer->active_count++;
/* unlock, call factory ... then on failure (conn == NULL), relock and undo: */
pool->total_connections--;
pool->active_connections--;
peer->active_count--;
```

**H3. Lisp UDP shutdown hang.** `stop-udp-endpoint` joins a receiver parked in
`socket-receive` (no timeout) *before* closing the socket → deadlock until the
next packet. *Fix:* hotfix F3 closes the socket first; the receiver's
`handler-case` absorbs the closed-socket error and exits on `running=nil`.

**H4. Lisp RTT is garbage across processes.** `send-udp-pong` stamps the
responder's clock instead of echoing the ping timestamp, and SBCL's
`get-internal-real-time` has a per-process epoch — so even localhost RTT is
meaningless. *Fix:* hotfix F2 echoes the ping timestamp; the sender subtracts on
its own clock.

**H5. Lisp config silently ignored.** `load-config` uses `getf` on the **alist**
`cl-json:decode-json` returns; `getf` never matches, so every key falls back to
its default and the config file does nothing but (sometimes) fail validation.
*Fix:* hotfix F5 adds a tolerant alist accessor `jref` and manual validation,
and drops the `cl-json-schema` dependency (not in Quicklisp — see B-class below).

**H6. Lisp state persistence is broken two ways.** `save-state` passes a Lisp
**list** to `dcf-db-insert`, which `aref`s it (error), and stores a bare array
against an object schema. *Fix:* hotfix F6/F7 persist peers as the JSON object
`{"peers":[...]}` and harden `dcf-db-insert` to accept string | octet-vector |
any object (JSON-encoded), closing the `aref` trap for all callers.

**H7. Lisp `dcf-benchmark` reports zeros.** `(when (network-stats-last-rtt …))`
— `0` is truthy in CL, so zeros are recorded before any pong arrives and
`min-rtt` is always 0. *Fix:* hotfix F9 guards with `plusp`.

---

## MEDIUM — portability, robustness

> **Status (v0.3.0, verified 2026-09-25): all MEDIUM items below are RESOLVED**
> (M1–M6, including M4 — see its corrected parenthetical below). An out-of-tree
> build + `ctest` (21/21) confirmed the fixes are applied in source. This does
> not extend to the quarantined transports (`plugins/experimental/`,
> `tests/legacy/`): those were **moved, not repaired** — the defects remain
> in-file, which was the prescribed remedy.

**M1. `DCF_INTERNAL` is GCC-only but the code supports MSVC.**
`#define DCF_INTERNAL __attribute__((visibility("hidden")))` is unconditional.
*Fix:* guard for `_MSC_VER` / `__GNUC__`. (patch)

**M2. `dcf_cond_timedwait` uses `CLOCK_REALTIME`.** A wall-clock step (NTP,
manual set) can stall the wait. *Fix:* build the deadline on `CLOCK_MONOTONIC`,
matching the condvar attr. (patch)

**M3. Dead unsigned error checks in `sctp`/`can`.** `*size` is `size_t`, so
`*size < 0` / `*size <= 0` can never catch a `recv` error. *Fix:* capture the
signed return in `ssize_t` first. (patch)

**M4. Crash handler is async-signal-unsafe.** `dcf_error.c`'s handler calls
`fopen`/`malloc`/`backtrace_symbols` inside a signal context. *Fix:* use
`write(2)` and `backtrace_symbols_fd` to a pre-opened fd; precompute the message.
(fixed: `src/dcf_error.c:597-634` now uses only `write()`, a pre-opened
`g_crash_log_fd`, and `backtrace_symbols_fd`)

**M5. Logging before `dcf_log_init` is UB.** `dcf_log_write_v` locks a possibly
uninitialized mutex. *Fix:* either require init, or use a statically-initialized
mutex and a `once` guard.

**M6. `strcasecmp`/`isatty` portability in `dcf_error.c`.** Needs `strings.h` on
POSIX; Windows has `_stricmp`/`_isatty`. *Fix:* a small platform shim.

---

## LOW / DX — the things that waste a new user's first hour

**D1. Documentation drift.** The Punctim README advertises a TUI, middleware,
and Dijkstra routing not present in v2.2.0; the C compilation guide cites a line
count and features the built library doesn't expose. Trim the docs to what ships,
or mark the rest "planned."

**D2. Dead test files.** `C_SDK/tests/test_plugin.c`, `tests/test_redundancy.c`,
and `lisp/tests/main.lisp` reference functions that do not exist
(`dcf_config_load`, `dcf_config_get_host`, `streamdb_close`, `dcf-group-peers`,
`dcf-simulate-failure`, `dcf-visualize-topology`, …). They aren't built, so they
rot silently. Either implement against the real API or move them to
`tests/legacy/` and exclude them.

**D3. Header/install mismatch.** The CMake header-install list ships ~5 headers
while the public API spans more (`dcf_client.h`, `dcf_config.h`, `dcf_cancel.h`).
Install what you expose, or stop exposing what you don't implement (see the
header-gap directive below).

**D4. Dockerfile foot-guns.** `COPY LICENSE*` errors on older Docker if nothing
matches; the runtime stage installs `libpthread-stubs0-dev` (a build-time dev
package, unnecessary on glibc); the coverage stage reconfigures CMake in a
build dir carried from the Release builder, so the Release cache can defeat the
Debug+coverage flags. Use a clean build dir for coverage and drop the dev package
from the runtime image.

**D5. `-march=native` in the latency test** is fine locally but not for
distributed builds; gate it behind a flag. `clang -Weverything` on C is mostly
noise; prefer `-Wall -Wextra -Wconversion`.

---

## Architecture directives

**Canonicalize one wire quantum.** Declare the 17-byte `DeModFrame` (version
nibble = 1) THE quantum. Express the proto-message UDP header and the C
`DCFMessageHeader` as explicit adapters over it, or retire them. The companion
`wire_quanta_category.md` proves this frame is a cemented retract with a unique
decoder and gives a 246-vector certificate; `golden_vectors.json` +
`wirelab_mcp.py certify` make conformance a CI check. Wire it into the build:
every SDK emits its 109 encode + 137 syndrome vectors, CI diffs them against the
golden file. That single test would have caught C7 the day it landed.

**Close the header→implementation gap.** For each of `dcf_cancel`,
`dcf_client`, `dcf_config`, `dcf_networking`, `dcf_serialization`,
`dcf_redundancy`: either implement it, or move the header to `include/experimental/`,
exclude it from the install target, and mark it clearly. Shipping headers whose
symbols don't exist is the fastest way to lose a developer's trust.

**Two-tier transport ABI, explicitly.** `DCFTransportV1` (4 fns, legacy) and the
rich `ITransport` (v2). Document which plugins target which; provide the one-line
migration. One exemplar plugin (UDP) is migrated in the patch.

**Quarantine the un-compilable transports.** `bluetooth_transport.c` (unbounded
`measure_rtt`↔`send` recursion, implicit decls, a nonexistent `dcf_parse_message`,
a hardcoded 10-peer array with no bounds check), `unified_dual_transport.c` (sends
a struct pointer as payload via a shadowed `data`, a `send` symbol clashing with
POSIX `send(2)`, references to an extinct `DCFConfig` shape), `quic_transport.c`
(misuses the MsQuic API — `MsQuicOpen2` returns an API table, not a connection),
`irc_transport.c` (base64 encoder mis-handles inputs not a multiple of 3, reads
past the buffer), `zigbee_transport.c` (fictional API). Move these to
`src/transports/experimental/`, exclude from the build, and track real ports as
issues. They are not close to compiling; patching them in place would be theater.

**Unify the license. (RESOLVED)** Previously the README said LGPL, the DOOM example
said GPL-3.0, the flake meta said MIT, and CPack auto-generated an MIT placeholder
when no LICENSE existed — so a `cpack` artifact could ship mislicensed. The repo is
now unified on `LGPL-3.0-only` (consistent with a linkable protocol library): a real
top-level `LICENSE` exists, `CPACK_RESOURCE_FILE_LICENSE` points at it
(`C_SDK/CMakeLists.txt`), and every flake `meta.license` (including `C_SDK/flake.nix`
and `lisp/flake.nix`) plus all language manifests now declare `LGPL-3.0-only`. The
GPL-3.0 notice stays scoped to the DOOM example only.

**Remove the slur in the Punctim acknowledgments. (RESOLVED)** A line in the
`lisp/README.md` acknowledgments section contained a slur with outsized
reputational stakes — it could not ship in a repo you license, demo, or hand to a
donee. The offending line has been removed.

---

## Apply

1. **Lisp — edit source first, then load the hotfix.** In `src/punctim.lisp`
   remove `:cl-json-schema` from both the `ql:quickload` list and the
   `defpackage :d-lisp (:use …)` form (it's not in the Quicklisp dist; the bare
   `quickload` aborts the load before anything could patch it). Then:
   ```lisp
   (load "src/punctim.lisp")
   (load "punctim-hotfix.lisp")   ; prints "wire codec :CERTIFIED" on success
   ```
   The hotfix late-binds every fix through the function cell, so already-compiled
   callers pick them up. Fold the bodies back into source for permanence.

2. **C — patch with recount + reject.**
   ```sh
   cd C_SDK
   git apply --recount --reject ../c_sdk_fixes.patch
   ```
   `--recount` makes the hunk line numbers advisory. The `dcf_interface.h` hunk is
   verified to apply; the rest are anchored on the exact buggy lines. Any `.rej`
   has its complete replacement quoted above (C4, H2 in full; the others are
   single-line). Then exclude the quarantine list from CMake and add the
   `STREAMDB_DEMO` guard's negative (don't define it for the library build).

3. **Verify the wire layer.**
   ```sh
   python3 verify_laws.py        # ALL LAWS HOLD; emits golden_vectors.json
   python3 wirelab_mcp.py --selftest
   ```

4. **MCP server (agent face for the wire codec).** Point your MCP client at:
   ```json
   {"mcpServers": {"dcf-wirelab":
     {"command": "python3", "args": ["/abs/path/wirelab_mcp.py"]}}}
   ```
   Tools: `crc16_ccitt`, `encode_frame`, `decode_frame`, `field_map`,
   `bitflip_audit`, `certify`, `golden_vectors`.

5. **GUI front panel.** `./demod-ui wirelab.lua` — encode/decode/audit-136 with a
   live field-colored byte grid and a Sierpinski seal that only lights turquoise
   when the codec self-certifies against the anchors.

---

## Shipped artifacts

| file | what it is |
|---|---|
| `DCF_CODE_REVIEW.md` | this document |
| `c_sdk_fixes.patch` | C SDK fixes (C1–C6, H1, M1–M3, C5; verified `dcf_interface.h` hunk) |
| `punctim-hotfix.lisp` | Lisp fixes F1–F9, self-certifying; SBCL-verified |
| `wire_quanta_category.md` | the cemented-wire-quantum formalization (Thms 1–4) |
| `wirelab_core.py` | reference codec |
| `verify_laws.py` | executable laws; regenerates the certificate |
| `golden_vectors.json` | 109+137-vector finite certificate |
| `wirelab_mcp.py` | MCP server over the codec + certificate |
| `wirelab.lua` | DeMoD UI front panel, self-certifying |

The single highest-leverage action: make `certify` a CI gate. It turns "every SDK
agrees" from a hope into a 246-vector test, and it would have caught the
`#xFFFF` Lisp bug — and will catch the next one — before it reached the wire.
