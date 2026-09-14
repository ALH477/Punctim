# Punctim (formerly DeMoD-LISP / D-LISP) SDK for DeMoD Communication Framework (DCF)

**Version 2.2.0** | **Updated: June 28, 2026**  
**Developed by DeMoD LLC**
**License**: GNU Lesser General Public License v3.0 (LGPL-3.0)  
**Repository**: [https://github.com/ALH477/DeMoD-Communication-Framework](https://github.com/ALH477/DeMoD-Communication-Framework)  
**Demo (soon)**: [YouTube Video](https://youtu.be/IXbIB8jCvDE)

## Overview

Punctim (formerly DeMoD-LISP / D-LISP) is a high-performance, production-ready Common Lisp implementation of the DeMoD Communication Framework (DCF), a free and open-source (FOSS) framework designed for low-latency, modular, and interoperable data exchange. Tailored for applications in IoT, gaming, distributed computing, and edge networking, Punctim provides a robust Domain-Specific Language (DSL) for seamless integration with DCF's modular architecture. It supports multiple transport protocols, self-healing P2P redundancy, AI-driven network optimization, and integrates **StreamDB** for persistent storage, making it ideal for developers building scalable, fault-tolerant communication systems.

This SDK, developed by **DeMoD LLC** with significant contributions from **Grok 4 Heavy** (xAI's advanced AI model), emphasizes reliability, extensibility, and compliance with U.S. export regulations (no encryption by default). It is part of the DCF mono repository and interoperates with other language SDKs (e.g., C, Python) for cross-platform compatibility. The rebranding to Punctim reflects a focus on resilient, multi-headed network topologies, while retaining full compatibility with DCF's command set (e.g., `dcf-send`, `dcf-db-insert`) for seamless migration.

### Efficiency Highlight: ~899 Lines of Code
Punctim achieves its extensive functionality in approximately **899 non-comment, non-blank lines of code** (verified across the core implementation and plugins). This remarkable efficiency is made possible by Common Lisp's expressive features, such as macros (e.g., `def-dcf-plugin` for concise plugin definitions) and CLOS for type-safe abstractions. The enhanced integration of StreamDB adds powerful persistence with minimal overhead, maintaining a lean codebase while delivering a full SDK/DSL. This compactness ensures maintainability, reduces deployment overhead, and highlights Lisp's power for building complex systems with minimal verbosity.

- **Core Breakdown**:
  - `d-lisp.lisp` (main SDK/DSL): ~899 lines
  - Plugins (e.g., LoRaWAN, Serial, USB, CAN, SCTP, Zigbee): ~2500 lines total (~21-1000 lines each)

## Key Features

- **Modular Transport Support**: Includes gRPC (default), Native Lisp (TCP), WebSocket, UDP, QUIC, Bluetooth, Serial, CAN, SCTP, Zigbee, and LoRaWAN via plugins.
- **Operating Modes**: Supports Client, Server, P2P, AUTO (dynamic role switching), and Master modes for flexible network configurations.
- **Self-Healing P2P**: Implements Dijkstra-based routing with RTT-based peer grouping (<50ms threshold) for automatic failover and redundancy.
- **Plugin System**: Extensible via `def-dcf-plugin` macro, allowing custom transports (e.g., LoRaWAN for long-range IoT).
- **Middleware Framework**: Customizable message processing with a chainable middleware system.
- **Type Safety**: Leverages CLOS with strict type declarations for messages and configurations.
- **AI-Driven Optimization**: Uses MGL neural networks for dynamic network topology optimization in Master mode.
- **Monitoring & Debugging**: Comprehensive metrics collection, Graphviz topology visualization, and an interactive TUI (ncurses-based).
- **StreamDB Integration**: Persistent storage for configurations, peer groups, metrics, and message logs using StreamDB, a lightweight, Rust-based key-value store with reverse trie indexing and thread-safe operations. Enhanced in v2.1.0 with end-to-end ACID transactions across gRPC RPCs, schema versioning validation, granular error parsing from Protobuf, transaction ID caching, and extended retry logic.
- **Interoperability**: Supports gRPC/Protobuf for cross-language communication and StreamDB’s file-based storage for shared data access with other DCF SDKs.
- **Testing**: Integrated FiveAM test suite for robust validation, including StreamDB operations, transaction RPCs, and extended benchmarks comparing StreamDB performance to in-memory storage.
- **Configuration**: JSON-based configuration with schema validation for reliability.
- **Production Enhancements (New in v2.1.0)**: End-to-end ACID support with new RPCs (`dcf-begin-transaction`, etc.), schema versioning for compatibility, Protobuf error code parsing, transaction ID caching for batch ops, and improved resource cleanup.

## Getting Started

### Prerequisites

- **Common Lisp Environment**: SBCL (recommended) or another compatible Lisp implementation.
- **Quicklisp**: For dependency management ([install Quicklisp](https://www.quicklisp.org/)).
- **Dependencies**: Install via Quicklisp: `:cl-protobufs`, `:cl-grpc`, `:cffi`, `:uuid`, `:cl-json`, `:jsonschema`, `:cl-ppcre`, `:cl-csv`, `:usocket`, `:bordeaux-threads`, `:curses`, `:log4cl`, `:trivial-backtrace`, `:cl-store`, `:mgl`, `:hunchensocket`, `:fiveam`, `:cl-dot`, `:cl-lsquic`, `:cl-serial`, `:cl-can`, `:cl-sctp`, `:cl-zigbee`, `:cl-lorawan`.
- **StreamDB Library**: Ensure `libstreamdb.so` (or `.wasm` for WASM targets) is available and loadable via CFFI.

### Config
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "transport": {
      "type": "string",
      "enum": ["gRPC", "native-lisp", "WebSocket"],
      "description": "Communication transport protocol (e.g., 'gRPC' for default interop)."
    },
    "host": {
      "type": "string",
      "description": "Host address (e.g., 'localhost' for local testing)."
    },
    "port": {
      "type": "integer",
      "minimum": 0,
      "maximum": 65535,
      "description": "Port number (e.g., 50051 for gRPC)."
    },
    "mode": {
      "type": "string",
      "enum": ["client", "server", "p2p", "auto", "master"],
      "description": "Node operating mode (e.g., 'p2p' for self-healing redundancy)."
    },
    "node-id": {
      "type": "string",
      "description": "Unique node identifier (e.g., UUID for distributed systems)."
    },
    "peers": {
      "type": "array",
      "items": { "type": "string" },
      "description": "List of peer addresses for P2P (e.g., ['peer1:50051', 'peer2:50052'])."
    },
    "group-rtt-threshold": {
      "type": "integer",
      "minimum": 0,
      "maximum": 1000,
      "description": "RTT threshold in ms for peer grouping (default 50 for <50ms clusters)."
    },
    "plugins": {
      "type": "object",
      "additionalProperties": true,
      "description": "Plugin configurations (e.g., {'udp': true} for custom transports)."
    },
    "storage": {
      "type": "string",
      "enum": ["streamdb", "in-memory"],
      "description": "Persistence backend (e.g., 'streamdb' for StreamDB integration)."
    },
    "streamdb-path": {
      "type": "string",
      "description": "Path to StreamDB file (required if storage='streamdb', e.g., 'dcf.streamdb')."
    },
    "optimization-level": {
      "type": "integer",
      "minimum": 0,
      "maximum": 3,
      "description": "Optimization level (e.g., 2+ enables StreamDB quick mode for ~100MB/s reads)."
    },
    "retry-max": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10,
      "default": 3,
      "description": "Max retries for transient errors (e.g., in StreamDB ops or gRPC calls)."
    }
  },
  "required": ["transport", "host", "port", "mode"],
  "additionalProperties": true,
  "dependencies": {
    "storage": {
      "oneOf": [
        { "properties": { "storage": { "const": "streamdb" } }, "required": ["streamdb-path"] },
        { "properties": { "storage": { "const": "in-memory" } } }
      ]
    }
  }
}
```

### Installation

1. Clone the repository:
   ```
   git clone https://github.com/ALH477/DeMoD-Communication-Framework --recurse-submodules
   cd DeMoD-Communication-Framework/lisp
   ```

2. Load the SDK in SBCL:
   ```
   sbcl --load src/d-lisp.lisp
   ```

3. (Optional) Build a standalone executable for deployment:
   ```lisp
   (dcf-deploy "hydra-mesh")
   ```

### Basic Usage

```lisp
;; Quick Start Client
(dcf-quick-start-client "config.json")

;; Send a Message
(dcf-quick-send "Hello, Punctim!" "localhost:50052")

;; Persist Data to StreamDB
(dcf-db-insert "/test/key" "{\"data\": \"value\", \"schema_version\": \"2.1.0\"}")

;; Query from StreamDB (with caching and validation)
(dcf-db-query "/test/key")

;; Async Query (non-blocking)
(dcf-db-get-async "/metrics/sends" (lambda (data len err) (if err (log:error err) (process-metrics data))))

;; Transactional Batch
(dcf-begin-transaction "tx1")
(dcf-send "batch data" "peer" "tx1")
(dcf-commit-transaction "tx1")

;; Benchmark Performance
(run-benchmarks)
```

For WASM targets (e.g., browser-based nodes), ensure `libstreamdb.wasm` is loaded, and use async ops for UI-responsive queries.

## Advanced Usage

### StreamDB Integration (Enhanced in v2.1.0)

StreamDB serves as Punctim's persistence layer, storing states, metrics, and logs with low-latency queries. Key enhancements:

- **Async Operations**: Non-blocking CRUD with callbacks (e.g., `dcf-db-get-async`) for real-time apps, integrated with Punctim's event loop.
- **Transactions**: ACID batch ops via `dcf-db-begin-transaction-async`, `dcf-db-commit-transaction-async`, etc., now extended to gRPC RPCs (`dcf-begin-transaction`, etc.) for end-to-end atomicity.
- **Schema Validation**: JSON data validated against schemas (e.g., `*streamdb-metrics-schema*`) on insert/query for type safety, with Protobuf `schema_version` checks for compatibility.
- **WASM Compatibility**: No-mmap fallback for embedded/browser targets, with examples for UI config fetches.
- **Error Handling**: Granular mapping of StreamDB and Protobuf errors to `dcf-error` conditions, with backtraces and retries for transients.
- **Caching**: Thread-safe LRU cache for queries and transaction IDs, reducing I/O in RTT-sensitive scenarios.
- **Benchmarks**: Compare StreamDB vs. in-memory performance, ensuring <1.5x overhead for 1000 operations, including transaction RPCs.

Use Case: In IoT, persist sensor data atomically during batch transactions with rollback on failure, querying async without blocking LoRaWAN receives.

### Plugins

Extend Punctim with plugins for custom transports (DCF commands remain compatible):

```lisp
(def-dcf-plugin my-transport
  ;; Plugin logic here
  )
(dcf-load-plugin "plugins/my-transport.lisp")
```

### Middleware

Customize message processing:

```lisp
(add-middleware (lambda (msg dir)
                  (log:debug "Processing ~A" msg)
                  msg))
```

### AI Optimization

In Master mode, optimize topology:

```lisp
(dcf-master-optimize-network "tx1")  ;; Transactional for atomic updates
```

Trains MGL nets on StreamDB-stored RTTs for efficient grouping.

### Testing

Run the FiveAM suite:

```lisp
(run-tests)
```

Includes StreamDB integration, transaction RPCs, and type checks.

### CLI/TUI

Use CLI for commands (e.g., `db-insert [path] [data]`, `begin-transaction [tx-id]`). Launch TUI for interactive monitoring:

```lisp
(dcf-tui)
```

### Deployment

Build executables:

```lisp
(dcf-deploy "hydra-mesh-app")
```

For WASM, compile with CL-WASM tools for browser deployment.

## Key Design Principles:

### 1. **Minimal Core (punctim.core)**
- Only essential abstractions: node, transport protocol, codec registry
- ~50 lines of code
- Everything else is optional

### 2. **Independent DSLs**
Each DSL is a separate package that can be loaded individually:

- **game-net**: Multiplayer gaming (`defgame`, `defplayer-message`)
- **audio-stream**: Real-time audio (`defaudio-stream`, `stream-audio`)
- **sensor-net**: IoT sensors (`defsensor`, `on-threshold`)
- **message-proto**: Protocol buffer-style messages (`defmessage`)
- **net-topology**: Network configuration (`defnetwork`, `peer`, `group`)
- **reliability**: Error handling patterns (`with-retry`, `with-circuit-breaker`)
- **metrics**: Observability (`defcounter`, `with-timing`)

### 3. **Declarative Over Imperative**

Instead of:
```lisp
(dcf-send-position "player1" 100.0 50.0 25.0)
```

You write:
```lisp
(defgame fps-match
  (position-update
   :fields ((x :float) (y :float) (z :float))
   :reliable nil))
```

And get `send-position-update` generated automatically!

### 4. **Composability**

Mix and match DSLs:
```lisp
(defpackage :my-game
  (:use :punctim.game-net    ; Gaming
        :punctim.audio-stream ; Voice chat
        :punctim.metrics))    ; Monitoring
```

### 5. **Zero Boilerplate**

The DSLs handle:
- ✅ Message encoding/decoding generation
- ✅ Sender function creation
- ✅ Handler registration
- ✅ Type safety
- ✅ Metrics tracking

### 6. **Domain-Specific Syntax**

Each DSL speaks its domain's language:
- **Game**: `broadcast`, `tick`, `on-receive`
- **Audio**: `stream-audio`, `set-codec`, `add-filter`
- **IoT**: `defsensor`, `report`, `aggregate`


## Why Punctim?

- **Performance**: Sub-ms messaging with StreamDB's low-latency persistence.
- **Modularity**: Plugins and middleware for easy customization.
- **Reliability**: Self-healing P2P, ACID transactions across RPCs, and robust error handling.
- **Interoperability**: Cross-language via gRPC and StreamDB.
- **FOSS Ethos**: LGPL-3.0 license promotes reuse in libraries and apps, with DCF command compatibility for easy migration.

## Acknowledgments

- **DeMoD LLC**: Core development and project leadership.
- **Grok 4 Heavy (xAI)**: AI-driven optimizations and code contributions.
- **Claude Sonnet (Anthropic)**: To correct Grok.
- **Open-Source Community**: For libraries like `cl-grpc`, `cffi`, `mgl`, and `streamdb`.

## License

This project is licensed under the GNU Lesser General Public License v3.0 (LGPL-3.0). See `LICENSE` for details.

---

**DeMoD LLC** | Empowering Scalable, Fault-Tolerant Communication  
**Built with ❤️ and Lisp**
