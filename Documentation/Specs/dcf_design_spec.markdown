# DeMoD Communications Framework Design Specification (FOSS Shareware Version)

**Version 5.0.0 | August 20, 2025**  
**Developed by DeMoD LLC**  
**Contact:** info@demod.ltd
**License:** GNU Lesser General Public License v3.0 only (LGPL-3.0-only)

## Table of Contents
- [1. Introduction](#1-introduction)
- [2. Compliance with Export Regulations](#2-compliance-with-export-regulations)
- [3. Objectives](#3-objectives)
- [4. Glossary](#4-glossary)
- [5. Protocol Design](#5-protocol-design)
  - [5.1. Modular Components](#51-modular-components)
  - [5.2. Plugin System](#52-plugin-system)
  - [5.3. AUTO Mode and Master Node](#53-auto-mode-and-master-node)
  - [5.4. Abstract Protocol Flow](#54-abstract-protocol-flow)
  - [5.5. Self-Healing P2P Redundancy](#55-self-healing-p2p-redundancy)
  - [5.6. Compatibility Layer](#56-compatibility-layer)
  - [5.7. Protocol Buffers Definition](#57-protocol-buffers-definition)
  - [5.8. gRPC Service Definition](#58-grpc-service-definition)
  - [5.9. JSON Configuration Schema](#59-json-configuration-schema)
- [6. Security Considerations](#6-security-considerations)
- [7. Performance and Optimization](#7-performance-and-optimization)
- [8. Implementation Guidelines](#8-implementation-guidelines)
  - [8.1. Dependencies and Environment Setup](#81-dependencies-and-environment-setup)
  - [8.2. Perl Implementation](#82-perl-implementation)
  - [8.3. Python Implementation](#83-python-implementation)
  - [8.4. C Implementation](#84-c-implementation)
  - [8.5. C++ Implementation](#85-c-implementation)
  - [8.6. JavaScript (Node.js) Implementation](#86-javascript-nodejs-implementation)
  - [8.7. Go Implementation](#87-go-implementation)
  - [8.8. Rust Implementation](#88-rust-implementation)
  - [8.9. Lisp Implementation](#89-lisp-implementation)
  - [8.10. Mobile Bindings](#810-mobile-bindings)
    - [8.10.1. Android Bindings (Java/Kotlin)](#8101-android-bindings-javakotlin)
    - [8.10.2. iOS Bindings (Swift)](#8102-ios-bindings-swift)
- [9. SDK Development Guidelines](#9-sdk-development-guidelines)
- [10. Interoperability and Testing Guide](#10-interoperability-and-testing-guide)
  - [10.1. Cross-Language Interoperability](#101-cross-language-interoperability)
  - [10.2. Unit and Integration Testing](#102-unit-and-integration-testing)
  - [10.3. P2P and Redundancy Testing](#103-p2p-and-redundancy-testing)
  - [10.4. Hardware Validation](#104-hardware-validation)
  - [10.5. Mobile Testing](#105-mobile-testing)
  - [10.6. Plugin Testing](#106-plugin-testing)
  - [10.7. AUTO Mode Testing](#107-auto-mode-testing)
- [11. Repository Structure](#11-repository-structure)
- [12. Building, Deploying, and Collaboration](#12-building-deploying-and-collaboration)
  - [12.1. Building Instructions](#121-building-instructions)
  - [12.2. Deployment Guidelines](#122-deployment-guidelines)
  - [12.3. Contribution Guidelines](#123-contribution-guidelines)

## 1. Introduction
The DeMoD Communications Framework (DCF) is a free and open-source software (FOSS) framework designed for low-latency, modular, and interoperable data exchange across diverse applications, including IoT messaging, real-time gaming synchronization, distributed computing, and edge networking. Evolving from the DeMoD Secure Protocol, DCF employs a handshakeless design, efficient serialization via Protocol Buffers (Protobuf), and a unified compatibility layer supporting UDP, TCP, WebSocket, gRPC, and custom transports (e.g., LoRaWAN). This enables seamless peer-to-peer (P2P) networking with self-healing redundancy for dynamic environments.

DCF is hardware- and language-agnostic, supporting deployments from resource-constrained embedded devices (e.g., microcontrollers) to high-performance cloud servers and mobile platforms (Android, iOS) via dedicated bindings. Version 5.0.0 introduces a refined plugin system with separate registries for transport and hardware plugins, enhanced security plugins, expanded language support (C, C++, JavaScript, Go, Rust, Lisp), AUTO mode with master node control for dynamic role assignment, and SDK submodules for streamlined integration. Licensed under LGPL-3.0-only, DCF ensures transparency and community-driven development, offering command-line interface (CLI), text user interface (TUI), server/client logic, P2P, and AUTO modes for versatile use cases, including standalone tools, libraries, and networked services.

## 2. Compliance with Export Regulations
DCF adheres to U.S. Export Administration Regulations (EAR) and International Traffic in Arms Regulations (ITAR) by excluding cryptographic primitives, ensuring export-control-free distribution. Users implementing custom extensions (e.g., TLS via plugins) must ensure compliance with applicable regulations. DeMoD LLC disclaims liability for non-compliant modifications; consult legal experts for specific use cases.

## 3. Objectives
- Deliver a lightweight, low-latency framework for data exchange (<1ms local latency).
- Ensure cross-language (C, Perl, Python, Lisp, etc.) and cross-platform interoperability via Protobuf and gRPC.
- Support extensibility through a robust plugin system with separate transport and hardware registries.
- Enable self-healing P2P networking with RTT-based grouping and redundancy.
- Maintain LGPL-3.0-only compliance and encourage community contributions.

## 4. Glossary
- **DCFMessage**: Protobuf-defined message for data exchange, including fields like `sender`, `data`, `group_id`.
- **RTT**: Round-Trip Time, used for grouping peers into clusters (<50ms threshold).
- **Plugin Manager**: Component for dynamic loading and dispatching of plugins (e.g., `dcf_plugin_manager.c` in C SDK, `dcf-plugin-manager` class in Lisp SDK).
- **ITransport**: Interface for transport plugins, defining methods for setup (with JSON params and timeout), send (with retries), receive (with timeout), health check, and destroy.
- **IHardwarePlugin**: Interface for hardware plugins, defining methods for setup (with JSON params), execute (for device operations), and destroy.
- **AUTO Mode**: Dynamic role assignment mode controlled by a master node.

## 5. Protocol Design
### 5.1. Modular Components
DCF comprises independent modules (configuration, serialization, networking, redundancy, interface, core logic, plugin manager) with well-defined APIs, enabling extensibility and maintainability. The plugin manager coordinates dynamic loading and dispatching of transport and hardware plugins.

### 5.2. Plugin System
The plugin system enables extensibility through dynamic loading of transport (single instance) and hardware plugins (multiple, identified by unique IDs, e.g., "i2c_sensor"). Plugins implement standardized interfaces:
- **ITransport**: Defines `setup(params, timeout_ms)`, `send(data, target, retries)`, `receive(timeout_ms)`, `health_check(target, rtt_ms)`, and `destroy()`. Supports transports like UDP, TCP, gRPC, LoRaWAN.
- **IHardwarePlugin**: Defines `setup(params)`, `execute(command_data)`, and `destroy()`. Supports hardware like I2C, USB.
Key features:
- **Configuration**: Driven by a "plugins" JSON object in `config.json` (e.g., `{"transport": "libcustom_transport.so", "hardware": {"i2c_sensor": "libi2c_sensor.so", "usb_device": "libusb_device.so"}}`).
- **Version Checking**: Enforces minimum version (>=1.1) during loading to ensure compatibility.
- **Parameters**: Plugins receive JSON parameters via cJSON (C SDK) or cl-json (Lisp SDK) for flexible configuration.
- **Registries**: Separate registries for transport (singleton) and hardware (linked list in C, hash-table in Lisp, keyed by ID).
- **Error Handling**: Robust with specific error codes (e.g., `DCF_ERR_INVALID_VERSION` in C, `dcf-error` conditions in Lisp); non-fatal for individual hardware plugin failures.
- **Implementation**: C SDK uses `dlopen` (`dcf_plugin_manager.c`); Lisp SDK uses `load` and funcall (`dcf-plugin-manager` class). Examples include `c_sdk/plugins/custom_transport.c`, `lisp/plugins/i2c-transport.lisp`.
Plugins enable custom transports (e.g., LoRaWAN for IoT) and hardware interactions (e.g., remote I2C sensor access).

### 5.3. AUTO Mode and Master Node
Master nodes leverage plugin health checks (e.g., `ITransport.health_check`) to collect metrics (availability, RTT) for dynamic role assignment and network optimization. AUTO mode nodes adapt roles (client, server, P2P) based on master directives.

### 5.4. Abstract Protocol Flow
1. Nodes load plugins via the plugin manager (from `config.json`).
2. `DCFMessage` (Protobuf) encapsulates data, routed via transport plugins (e.g., gRPC, UDP).
3. Hardware plugins execute commands (e.g., I2C read) on remote nodes, with results returned via transports.
4. Redundancy module ensures delivery through health checks and rerouting.

### 5.5. Self-Healing P2P Redundancy
The redundancy module uses plugin health checks to detect failures and reroute messages within 10 seconds. RTT-based grouping clusters peers (<50ms) using Dijkstra’s algorithm with RTT weights.

### 5.6. Compatibility Layer
The compatibility layer abstracts transport protocols, with the plugin manager selecting the active transport based on configuration. Supports UDP, TCP, WebSocket, gRPC, and custom transports.

### 5.7. Protocol Buffers Definition
Defined in `messages.proto`, including `DCFMessage` (`sender`, `recipient`, `data`, `group_id`, etc.). Extended for plugin interactions with `PluginRequest` (`plugin_id`, `command`, `params`) and `PluginResponse` (`data`, `error`).

### 5.8. gRPC Service Definition
Defined in `services.proto`, including `SendMessage`, `HealthCheck`, and `BidirectionalChat` for streaming. Plugins can extend services for custom RPCs.

### 5.9. JSON Configuration Schema
The `config.schema.json` validates `config.json`, extended to include:
```json
{
  "type": "object",
  "properties": {
    "plugins": {
      "type": "object",
      "properties": {
        "transport": { "type": "string" },
        "hardware": {
          "type": "object",
          "additionalProperties": { "type": "string" }
        }
      },
      "required": ["transport"]
    },
    "transport-params": { "type": "object" },
    "hardware-params": {
      "type": "object",
      "additionalProperties": { "type": "object" }
    },
    "timeout-ms": { "type": "integer", "minimum": 1000 },
    "retries": { "type": "integer", "minimum": 0 }
  }
}
```

## 6. Security Considerations
- **No Built-in Encryption**: DCF avoids cryptographic primitives to comply with EAR/ITAR.
- **Plugin Safety**: The plugin manager validates versions and paths to prevent malicious loading; non-fatal hardware loads ensure system stability.
- **User Responsibility**: Users adding encryption (e.g., TLS via gRPC plugins) must ensure export compliance.
- **Basic Protections**: `DCFMessage` includes timestamps and sequence numbers to prevent replays; plugins validate inputs.

## 7. Performance and Optimization
- **Latency**: Local exchanges achieve <1ms latency; plugin loading adds <1ms overhead.
- **CPU Usage**: <5% on Raspberry Pi, verified via benchmarks.
- **Scalability**: Supports 100+ peers with bounded node degrees; RTT-based grouping optimizes routing.
- **Plugin Efficiency**: Minimal allocations; health checks ensure low-latency routing.
- **Memory Safety**: C SDK is Valgrind-clean; Lisp SDK uses `unwind-protect` for resource cleanup.

## 8. Implementation Guidelines
### 8.1. Dependencies and Environment Setup
- **C SDK**: Requires `libprotobuf-c`, `libgrpc`, `libdl` (for `dlopen`), `libcjson` (for JSON params). Install via `apt install libprotobuf-c-dev libgrpc-dev libjson-c-dev`.
- **Lisp SDK**: Requires `cl-protobufs`, `cl-grpc`, `cl-json`, `cffi` via Quicklisp.
- **Other SDKs**: Language-specific dependencies (e.g., `protobuf` for Python, `tonic` for Rust).

### 8.2. Perl Implementation
Implement plugin manager using hash for hardware plugins, `do` for loading, and JSON parsing for params.

### 8.3. Python Implementation
Use `importlib` for dynamic plugin loading, dictionary for hardware registries, and `json` for params.

### 8.4. C Implementation
The plugin manager (`c_sdk/src/dcf_sdk/dcf_plugin_manager.c`) uses `dlopen` for dynamic loading, linked list for hardware plugins, and cJSON for params. Supports version checking (>=1.1), error handling (`DCFError`), and separate registries.

### 8.5. C++ Implementation
Similar to C, using smart pointers for memory safety and `std::map` for hardware plugins.

### 8.6. JavaScript (Node.js) Implementation
Use `require` for dynamic loading, object for registries, and JSON parsing for params.

### 8.7. Go Implementation
Use reflection for plugin interfaces, `map` for hardware registries, and JSON parsing for params.

### 8.8. Rust Implementation
Implement plugin manager with traits for interfaces, `HashMap` for hardware registries, and `serde_json` for params.

### 8.9. Lisp Implementation
The plugin manager (`lisp/src/d-lisp.lisp`) uses a CLOS class (`dcf-plugin-manager`) with hash-table for hardware plugins, `load` for dynamic loading, and `cl-json` for params. Supports version checking and error conditions.

### 8.10. Mobile Bindings
#### 8.10.1. Android Bindings (Java/Kotlin)
Load plugins via `System.loadLibrary`; use JSON for params.

#### 8.10.2. iOS Bindings (Swift)
Load plugins dynamically; use JSON for configuration.

## 9. SDK Development Guidelines
Each SDK must implement:
- Plugin manager with separate transport (singleton) and hardware (multi, by ID) registries.
- Version checking (>=1.1) and error handling.
- Support for JSON params in plugin setup.
- Integration with Protobuf/gRPC, RTT grouping, and AUTO mode.

## 10. Interoperability and Testing Guide
### 10.1. Cross-Language Interoperability
Test plugin dispatch across SDKs (e.g., Lisp hardware plugin invoked via C transport).

### 10.2. Unit and Integration Testing
Use FiveAM (Lisp), Unity (C), pytest (Python). Cover plugin loading, version failures, setup with JSON params, and dispatch.

### 10.3. P2P and Redundancy Testing
Simulate node failures; verify rerouting within 10s using plugin health checks.

### 10.4. Hardware Validation
Test on Raspberry Pi, AWS EC2; measure latency (<1ms) and CPU (<5%) with hardware plugins (e.g., I2C, USB).

### 10.5. Mobile Testing
Verify plugin loading and performance on Android/iOS emulators/devices.

### 10.6. Plugin Testing
Test dynamic loading, multi-hardware dispatch by ID, failure cases (e.g., invalid version, missing functions).

### 10.7. AUTO Mode Testing
Test master node role assignment, config updates, and plugin metrics collection using Docker multi-node setups.

## 11. Repository Structure
```
demod-communications-framework/
├── README.md
├── LICENSE  // LGPL-3.0-only
├── messages.proto
├── services.proto  // For gRPC
├── config.schema.json  // For validation
├── config.json.example
├── perl/  // Perl SDK
│   ├── lib/DCF/  // Modules
│   └── dcf.pl  // Entry
├── python/  // Python SDK
│   ├── dcf/  // Modules
│   └── dcf.py  // Entry
├── c_sdk/  // C SDK submodule
│   ├── include/dcf_sdk/  // Headers: dcf_client.h, dcf_error.h, dcf_plugin_manager.h
│   ├── src/dcf_sdk/  // Sources: dcf_client.c, dcf_redundancy.c, dcf_plugin_manager.c
│   ├── proto/  // messages.proto, services.proto
│   ├── examples/  // p2p.c, master.c
│   ├── plugins/  // custom_transport.c, i2c_transport.c, usb_transport.c
│   ├── tests/  // test_redundancy.c, test_plugin.c
│   ├── CMakeLists.txt  // Build
│   └── C-SDKreadme.md  // Guide
├── lisp/  // Lisp SDK
│   ├── src/  // d-lisp.lisp
│   ├── plugins/  // i2c-transport.lisp, usb-transport.lisp
│   ├── tests/  // plugin-manager-test.lisp
│   └── README.md
├── cpp/  // C++ SDK
├── nodejs/  // Node.js SDK
├── go/  // Go SDK
├── rust/  // Rust SDK
├── android/  // Android bindings
├── ios/  // iOS bindings
├── plugins/  // Shared example plugins
├── tests/  // Shared tests
├── docs/
│   ├── CONTRIBUTING.md
│   ├── CODE_OF_CONDUCT.md
│   └── dcf_design_spec.md  // This document
├── .github/workflows/  // CI/CD
└── Dockerfile  // Container testing
```

## 12. Building, Deploying, and Collaboration
### 12.1. Building Instructions
1. Clone repository: `git clone --recurse-submodules https://github.com/ALH477/DeMoD-Communication-Framework.git`
2. Generate Protobuf/gRPC code:
   - Perl/Python: `protoc --perl_out=perl/lib --python_out=python/dcf --grpc_out=python/dcf --plugin=protoc-gen-grpc_python=python -m grpc_tools.protoc messages.proto services.proto`
   - C SDK: `protoc --c_out=c_sdk/src messages.proto`
   - C++: `protoc --cpp_out=cpp/src --grpc_out=cpp/src --plugin=protoc-gen-grpc=grpc_cpp_plugin messages.proto services.proto`
   - Node.js: `protoc --js_out=import_style=commonjs:nodejs/src --grpc_out=nodejs/src --plugin=protoc-gen-grpc=grpc_node_plugin messages.proto services.proto`
   - Go: `protoc --go_out=go/src --go-grpc_out=go/src messages.proto services.proto`
   - Rust: Use `tonic-build` in `build.rs`
   - Lisp: Use `cl-protobufs` for Protobuf parsing
   - Android: `protoc --java_out=android/app/src/main --grpc_out=android/app/src/main --plugin=protoc-gen-grpc-java=grpc-java-plugin messages.proto services.proto`
   - iOS: `protoc --swift_out=ios/Sources --grpc-swift_out=ios/Sources messages.proto services.proto`
3. Build SDKs:
   - C SDK: `cd c_sdk && mkdir build && cd build && cmake .. && make`
   - Lisp: `(ql:quickload :d-lisp)`
   - Perl: `cpanm --installdeps .`
   - Python: `pip install -r python/requirements.txt`
   - Others: Follow language-specific build tools.
4. Plugins: Place in `plugins/` or SDK-specific directories (e.g., `c_sdk/plugins/`, `lisp/plugins/`).

### 12.2. Deployment Guidelines
- Deploy as daemons (e.g., via systemd) or containerize with Docker.
- Expose gRPC ports securely; deploy master nodes on reliable hosts.
- Mobile: Package as libraries or apps.
- SDKs: Deploy as submodules; C SDK produces `libdcf_sdk.a`.
- Configure plugins via `config.json` for transport and hardware.

### 12.3. Contribution Guidelines
- Fork the repository, create a feature branch, add tests, and submit a pull request.
- Follow code style: `perltidy` (Perl), `black` (Python), `clang-format` (C/C++), `lispindent` (Lisp).
- New SDKs (e.g., Python, Perl) must implement plugin manager, RTT grouping, AUTO mode, and LGPL-3.0-only compliance.
- Discuss contributions via GitHub Issues.
