{
  description = "DeMoD Communication Framework (DCF) Mono Repo SDKs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # Rust toolchain with the wasm32-unknown-unknown target (for the browser
    # WASM codec — plain nixpkgs rustc ships host std only). See the `.#wasm` shell.
    rust-overlay.url = "github:oxalica/rust-overlay";
    # Pinned Faust for a reproducible, binary-cached compiled-Faust build of
    # HydraModem (nixos-24.05 ships Faust 2.72.14). The adapters are version-
    # tolerant across Faust 2.72–2.85, so this pin is for determinism, not
    # necessity; see hydramodem/docs/FAUST_MODERNIZATION.md.
    nixpkgs-faust.url = "github:NixOS/nixpkgs/nixos-24.05";
    # GPL-3.0 JANUS (STANAG 4748) reference modem. Built as a SEPARATE package and
    # used by the DCF `janus:` transport as a subprocess only — never linked into
    # the LGPL library. See Documentation/DCF_JANUS_SPEC.md and LICENSING.md.
    janus-c-src = { url = "github:mission-systems-pty-ltd/janus-c"; flake = false; };
    # GPL-3.0-only (OR DeMoD Commercial) quanta codec. Built as a SEPARATE package and
    # used by the DCF-Snake record plane via the quanta-stream / quanta-stream-decode
    # subprocesses only — never linked into the LGPL library (mere aggregation, like
    # janus-c). quanta lives in the `quanta/` subdir of the DeMoD monorepo. See
    # Documentation/DCF_SNAKE_SPEC.md and LICENSING.md.
    demod-quanta-src = { url = "github:ALH477/DeMoD"; flake = false; };
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay, nixpkgs-faust, janus-c-src, demod-quanta-src }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; overlays = [ rust-overlay.overlays.default ]; };
        # Faust 2.72.14 (nixos-24.05) — a cached, reproducible Faust for the
        # compiled-Faust build. The adapters also build on 2.83/2.85 (the build
        # is version-tolerant); this pin just fixes a known-good cached toolchain.
        faust270 = (import nixpkgs-faust { inherit system; }).faust;
        # Toolchain with the wasm target bundled in, for codec-wasm + the web build.
        rustWasm = pkgs.rust-bin.stable.latest.default.override {
          targets = [ "wasm32-unknown-unknown" ];
        };
        protobuf = pkgs.protobuf;
        # Shared proto generation function
        generateProtos = lang: outDir: flags: pkgs.runCommand "dcf-protos-${lang}" {} ''
          mkdir -p $out/${outDir}
          ${protobuf}/bin/protoc -I${self} ${flags} ${self}/messages.proto ${self}/services.proto
          cp -r . $out/${outDir}
        '';
        # LangGraph agent system — Python env + CLI/TUI/serve/mcp wrappers.
        # Defined in the outer scope so both `packages` (docker images) and
        # `apps` (nix run) can reference them.
        agentPython = pkgs.python3.withPackages (ps: with ps; [
          langgraph langchain-core httpx pydantic rich textual pytest
          fastapi uvicorn mcp
        ]);
        agentCLI = pkgs.writeShellApplication {
          name = "dcf-agent";
          runtimeInputs = [ agentPython ];
          text = ''
            export PYTHONPATH="${self}/langgraph_agents''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${agentPython}/bin/python -m langgraph_agents.cli "$@"
          '';
        };
        agentTUI = pkgs.writeShellApplication {
          name = "dcf-agent-tui";
          runtimeInputs = [ agentPython ];
          text = ''
            export PYTHONPATH="${self}/langgraph_agents''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${agentPython}/bin/python -m langgraph_agents.tui "$@"
          '';
        };
        agentServe = pkgs.writeShellApplication {
          name = "dcf-agent-serve";
          runtimeInputs = [ agentPython ];
          text = ''
            export PYTHONPATH="${self}/langgraph_agents''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${agentPython}/bin/python -m langgraph_agents.cli serve "$@"
          '';
        };
        agentMCP = pkgs.writeShellApplication {
          name = "dcf-agent-mcp";
          runtimeInputs = [ agentPython ];
          text = ''
            export PYTHONPATH="${self}/langgraph_agents''${PYTHONPATH:+:$PYTHONPATH}"
            exec ${agentPython}/bin/python -m langgraph_agents.cli mcp "$@"
          '';
        };
      in
      {
        packages = rec {

          # C SDK — builds the dcfnode mesh node (ProtoMessage/UDP + Faust modem).
          # src is the whole repo because the node includes the header-only codec
          # at ../codec; the build only targets dcfnode (the 4-module spine is
          # configured but not linked here).
          dcf-c = pkgs.stdenv.mkDerivation {
            pname = "dcf-c";
            version = "0.3.0";
            src = self;
            nativeBuildInputs = [ pkgs.cmake ];
            dontUseCmakeConfigure = true;
            buildPhase = ''
              runHook preBuild
              cmake -S C_SDK -B build \
                -DDCF_BUILD_NODE=ON -DDCF_BUILD_TESTS=OFF -DDCF_BUILD_EXAMPLES=OFF
              cmake --build build --target dcfnode -j''${NIX_BUILD_CORES:-2}
              runHook postBuild
            '';
            installPhase = ''
              mkdir -p $out/bin
              cp build/dcfnode $out/bin/
            '';
            meta.description = "DCF C SDK node (dcfnode: ProtoMessage/UDP + Faust modem)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcfnode";
          };

          # C++ SDK — the supercharged gRPC node (dcfcpp). CMake's find_package
          # picks up gRPC/protobuf and runs the C++/grpc codegen on cpp/proto/dcf.proto;
          # the Nix derivation provides the full transitive closure (re2/absl/...).
          dcf-cpp = pkgs.stdenv.mkDerivation {
            pname = "dcf-cpp";
            version = "0.3.0";
            src = self + "/cpp";
            nativeBuildInputs = [ pkgs.cmake pkgs.pkg-config pkgs.protobuf pkgs.grpc ];
            buildInputs = [ pkgs.grpc pkgs.protobuf pkgs.openssl pkgs.zlib pkgs.c-ares pkgs.re2 pkgs.abseil-cpp ];
            cmakeFlags = [ "-DDCF_CPP_GRPC=ON" ];
            # Build only the node target (the certify tests need ../Documentation at run
            # time and are exercised separately).
            buildPhase = "cmake --build . --target dcfcpp -j$NIX_BUILD_CORES";
            installPhase = ''
              mkdir -p $out/bin
              cp dcfcpp $out/bin/
            '';
            meta.description = "DCF C++ gRPC node (dcfcpp)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcfcpp";
          };

          # C++ node with the Steam-compatible transport on the open
          # GameNetworkingSockets (BSD-3) — Valve's same ISteamNetworkingSockets API
          # the Steamworks SDK exposes, so this same dcfcpp recompiles against the
          # proprietary SDK (DCF_CPP_STEAM, developer-supplied) for SDR relay +
          # lobbies. `serve-gns` is the dedicated-server hub; `connect-gns` a client.
          # See Documentation/DCF_STEAM_SPEC.md. NOTE: nixpkgs GNS 1.4.1 ships
          # without WebRTC/ICE, so internet NAT-punch P2P needs the Steam SDR backend
          # or a WebRTC-enabled GNS; LAN/direct + hub forwarding work here.
          dcf-cpp-gns = pkgs.stdenv.mkDerivation {
            pname = "dcf-cpp-gns";
            version = "0.3.0";
            src = self + "/cpp";
            nativeBuildInputs = [ pkgs.cmake pkgs.pkg-config ];
            buildInputs = [ pkgs.gamenetworkingsockets pkgs.protobuf pkgs.openssl ];
            cmakeFlags = [
              "-DDCF_CPP_GRPC=OFF"
              "-DDCF_CPP_GNS=ON"
              "-DDCF_GNS_INCLUDE_DIR=${pkgs.gamenetworkingsockets}/include/GameNetworkingSockets"
              "-DDCF_GNS_LIB_DIR=${pkgs.gamenetworkingsockets}/lib"
            ];
            buildPhase = "cmake --build . --target dcfcpp -j$NIX_BUILD_CORES";
            installPhase = ''
              mkdir -p $out/bin
              cp dcfcpp $out/bin/dcfcpp
            '';
            meta.description = "DCF C++ Steam-compatible node (GameNetworkingSockets)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcfcpp";
          };

          # Go SDK — builds the dcfnode CLI (a real UDP DeModFrame node, the Go
          # analogue of the Rust `dcf` binary). The module is stdlib-only (no
          # external deps, no gRPC/proto), so vendorHash = null.
          dcf-go = pkgs.buildGoModule {
            pname = "dcf-go";
            version = "0.3.0";
            src = self + "/go";
            vendorHash = null; # stdlib-only: nothing to vendor
            subPackages = [ "cmd/dcfnode" ];
            meta.description = "DCF Go SDK node (dcfnode CLI)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcfnode";
          };

          # StreamDB — the small C embedded DB the Lisp SDK dlopens at startup
          # (cffi:use-foreign-library libstreamdb). lisp/streamdb is plain C (.c/.h),
          # not Rust, so this is an stdenv build producing the shared object on the
          # loader path + headers. Mirrors lisp/Dockerfile's gcc recipe.
          streamdb = pkgs.stdenv.mkDerivation {
            pname = "streamdb";
            version = "2.2.0";
            src = self + "/lisp/streamdb";
            buildInputs = [ pkgs.util-linux.dev pkgs.util-linux.lib ]; # libuuid
            buildPhase = ''
              runHook preBuild
              $CC -shared -fPIC -O2 streamdb.c libstreamdb_wrapper.c \
                -o libstreamdb.so -luuid
              runHook postBuild
            '';
            installPhase = ''
              runHook preInstall
              mkdir -p $out/lib $out/include
              cp libstreamdb.so $out/lib/
              cp streamdb.h libstreamdb_wrapper.h $out/include/
              runHook postInstall
            '';
            meta.description = "StreamDB embedded DB for the DCF Lisp SDK";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # SBCL with the SDK's Quicklisp systems pre-loaded via nixpkgs lispPackages
          # (exposed through ASDF; src/punctim.lisp loads them portably).
          punctim-sbcl = pkgs.sbcl.withPackages (ps: with ps; [
            cffi uuid usocket bordeaux-threads
            log4cl trivial-backtrace flexi-streams fiveam
            ieee-floats cl-json
          ]);

          # Hermetic Lisp SDK executable (the `punctim` CLI). Builds the saved
          # SBCL core via dcf-deploy, with libstreamdb.so on the loader path so the
          # foreign library resolves both at build and at runtime.
          punctim-lisp = pkgs.stdenv.mkDerivation {
            pname = "punctim-lisp";
            version = "2.2.0";
            src = self + "/lisp";
            nativeBuildInputs = [ punctim-sbcl pkgs.makeWrapper ];
            dontStrip = true; # stripping breaks SBCL executables
            buildPhase = ''
              runHook preBuild
              export LD_LIBRARY_PATH=${streamdb}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
              ${punctim-sbcl}/bin/sbcl --no-userinit --non-interactive \
                --load src/punctim.lisp \
                --eval '(in-package :d-lisp)' \
                --eval '(dcf-deploy "punctim")' \
                --quit
              runHook postBuild
            '';
            installPhase = ''
              runHook preInstall
              mkdir -p $out/bin $out/libexec
              cp punctim $out/libexec/punctim
              # Wrap so the saved core can dlopen libstreamdb.so at runtime; without
              # it the SDK still runs (DB disabled), but this enables the DB.
              makeWrapper $out/libexec/punctim $out/bin/punctim \
                --prefix LD_LIBRARY_PATH : ${streamdb}/lib
              runHook postInstall
            '';
            meta.description = "Punctim (D-LISP) SDK executable";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "punctim";
          };

          # Python SDK. `pyproject`/`build-system` are explicit: nixpkgs dropped the
          # implicit setuptools default, and without them the derivation fails to
          # evaluate ("does not configure a `format`").  numpy is the one runtime
          # dependency python/pyproject.toml declares (dcf.transport imports the
          # acoustic/IQ modems unconditionally).
          dcf-python = pkgs.python3Packages.buildPythonPackage {
            pname = "dcf-python";
            version = "0.3.0";
            src = self + "/python";
            pyproject = true;
            build-system = with pkgs.python3Packages; [ setuptools ];
            propagatedBuildInputs = with pkgs.python3Packages; [ numpy protobuf grpcio grpcio-tools ];
            preBuild = ''
              python -m grpc_tools.protoc -I${self} --python_out=dcf --grpc_python_out=dcf ${self}/messages.proto ${self}/services.proto
            '';
            # The wheel ships dcf, dcf.modem, dcf.MCP and the dcf.{pipe,qkd,sense,spa}
            # subpackages; prove every one of them imports from the installed layout.
            pythonImportsCheck = [
              "dcf" "dcf.MCP" "dcf.pipe" "dcf.qkd" "dcf.sense" "dcf.spa"
            ];
            meta.description = "Python SDK for DCF";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # Rust SDK — the `dcf` node binary. Built from the whole repo so the
          # crate's path dependency on ../codec resolves; build.rs runs tonic-build
          # itself (it just needs protoc via PROTOC), so no manual codegen phase.
          dcf-rust = pkgs.rustPlatform.buildRustPackage {
            pname = "dcf-rust";
            version = "0.3.0";
            src = self;
            cargoRoot = "rust";
            buildAndTestSubdir = "rust";
            cargoLock.lockFile = self + "/rust/Cargo.lock"; # deterministic — local path dep, no hash
            nativeBuildInputs = [ protobuf ];
            PROTOC = "${protobuf}/bin/protoc";
            doCheck = false;
            meta.description = "DCF Rust SDK node (dcf binary)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcf";
          };

          # WS↔UDP bridge for the browser WASM client (web/bridge/). A stateless
          # relay — it shuttles opaque datagrams; the DCF codec runs in the browser.
          # Deploy behind WireGuard (the wire is plaintext). See DCF_WASM_SPEC.md.
          dcf-ws-bridge = pkgs.rustPlatform.buildRustPackage {
            pname = "dcf-ws-bridge";
            version = "0.1.0";
            src = self;
            cargoRoot = "web/bridge";
            buildAndTestSubdir = "web/bridge";
            cargoLock.lockFile = self + "/web/bridge/Cargo.lock";
            doCheck = false;
            meta.description = "DCF browser-client WebSocket↔UDP bridge";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcf-ws-bridge";
          };

          # DCF-SPA single-packet port authorizer. Authentication-only side
          # channel (EAR99; see Documentation/DCF_SPA_SPEC.md §3); runtime needs
          # `nft` on PATH to edit the allow-set (wrapped in via makeBinaryWrapper).
          dcf-spa-authorizer = pkgs.rustPlatform.buildRustPackage {
            pname = "dcf-spa-authorizer";
            version = "0.1.0";
            src = self;
            cargoRoot = "spa";
            buildAndTestSubdir = "spa";
            cargoLock.lockFile = self + "/spa/Cargo.lock";
            nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
            postInstall = ''
              wrapProgram $out/bin/dcf-spa-authorizer \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.nftables ]}
            '';
            meta.description = "DCF-SPA single-packet port authorizer (authentication-only)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
            meta.mainProgram = "dcf-spa-authorizer";
          };

          # FFmpeg with the native DCF-Audio demuxer built in. DCF-Audio is an
          # adapter over the 17-byte wire quantum; ff_dcf_demuxer reuses the
          # certified C reassembler (codec/demod_audio.h) so `ffmpeg -f dcf -i
          # capture.dcf out.flac` records audio off the wire into any file type.
          # See ffmpeg-dcf/README.md. The PM synth (codec_id 2) is compiled in
          # from codec/faust/dcf_pm_codec.gen.c; codec_id 0/1 ride built-in
          # opus / pcm_u8 decoders, so only a demuxer is registered.
          dcf-ffmpeg = pkgs.ffmpeg.overrideAttrs (old: {
            pname = "dcf-ffmpeg";
            postPatch = (old.postPatch or "") + ''
              cp ${self}/ffmpeg-dcf/dcfdec.c            libavformat/dcfdec.c
              cp ${self}/ffmpeg-dcf/dcf_pm_ff.c         libavformat/dcf_pm_ff.c
              cp ${self}/codec/demod_frame.h            libavformat/demod_frame.h
              cp ${self}/codec/demod_audio.h            libavformat/demod_audio.h
              cp ${self}/codec/faust/dcf_pm_codec.gen.c libavformat/dcf_pm_codec.gen.c
              cp ${self}/codec/faust/dcf_pm_faust.c     libavformat/dcf_pm_faust.c
              # Register the demuxer (configure's find_things_extern scans this file).
              sed -i '/extern const FFInputFormat  *ff_aa_demuxer;/a extern const FFInputFormat  ff_dcf_demuxer;' \
                  libavformat/allformats.c
              # Link the demuxer + PM synth (compiled via the missing-prototypes
              # build shim) when CONFIG_DCF_DEMUXER is on.
              printf '\nOBJS-$(CONFIG_DCF_DEMUXER) += dcfdec.o dcf_pm_ff.o\n' \
                  >> libavformat/Makefile
            '';
            configureFlags = (old.configureFlags or [ ]) ++ [ "--enable-demuxer=dcf" ];
            doCheck = false;
            meta = (old.meta or { }) // {
              description = "FFmpeg with the native DCF-Audio (dcf) demuxer";
            };
          });

          # CLI front door to the `dcf` demuxer: record / inspect / split / listen.
          # Python (reuses the certified python/MCP + python/dcf modules) and shells
          # ffmpeg/ffprobe from dcf-ffmpeg. See ffmpeg-dcf/dcf-rec + README.
          dcf-rec = pkgs.writeShellApplication {
            name = "dcf-rec";
            runtimeInputs = [ dcf-ffmpeg pkgs.python3 ];
            text = ''exec python3 ${self}/ffmpeg-dcf/dcf-rec "$@"'';
          };

          # DCF-Radio: digital-radio HLS stream + DVR (rewind) per channel, captured
          # off the wire. Convenience alias for `dcf-rec stream`. See DCF_RADIO.md.
          dcf-radio = pkgs.writeShellApplication {
            name = "dcf-radio";
            runtimeInputs = [ dcf-ffmpeg pkgs.python3 ];
            text = ''exec python3 ${self}/ffmpeg-dcf/dcf-rec stream "$@"'';
          };

          # DCF-SDR: carry DeModFrames over radio with Reed-Solomon FEC -> SoapySDR
          # or .cf32 IQ (GFSK/QPSK/QAM/OOK/AFSK-over-FM). See DCF_SDR_SPEC.md.
          # numpy covers .cf32 + loopback; `nix develop .#sdr` adds the SDR tools.
          dcf-sdr = pkgs.writeShellApplication {
            name = "dcf-sdr";
            runtimeInputs = [ (pkgs.python3.withPackages (ps: [ ps.numpy ])) ];
            text = ''exec python3 ${self}/python/modem/sdr.py "$@"'';
          };

          # ── OCI node images (hermetic; built with `nix build .#docker-*`) ──────
          # These run like real nodes, not cert harnesses: each image's entrypoint
          # is the node binary with `start` as the default command.

          # DCF-Radio service image: a long-running station that taps UDP audio and
          # serves HLS+DVR over HTTP. `nix build .#docker-dcf-radio`.
          docker-dcf-radio = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-radio";
            tag = "latest";
            contents = [ dcf-radio pkgs.cacert ];
            config = {
              Entrypoint = [ "${dcf-radio}/bin/dcf-radio" ];
              Cmd = [ "--bind" "0.0.0.0:7100" "--http" "0.0.0.0:8000"
                      "--archive" "/var/dcf-radio" "--dvr" "6h" ];
              ExposedPorts = { "7100/udp" = { }; "8000/tcp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          docker-dcf-go = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-go";
            tag = "latest";
            contents = [ dcf-go pkgs.cacert ];
            config = {
              Entrypoint = [ "${dcf-go}/bin/dcfnode" ];
              Cmd = [ "start" ];
              ExposedPorts = { "7777/udp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          docker-dcf-rust = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-rs";
            tag = "latest";
            contents = [ dcf-rust pkgs.cacert ];
            config = {
              Entrypoint = [ "${dcf-rust}/bin/dcf" ];
              Cmd = [ "start" ];
              ExposedPorts = { "7777/udp" = { }; "50051/tcp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # Python node: the stdlib mesh endpoint (matrix-bridge/a2a.py over
          # DcfTextNode — bare DeModFrame + SuperPack dialect). `recv --follow`
          # is a persistent listener; `send "msg" --peers h:p` unicasts.
          dcf-python-node = pkgs.writeShellApplication {
            name = "dcf-node";
            runtimeInputs = [ pkgs.python3 ];
            text = ''exec python3 ${self}/matrix-bridge/a2a.py "$@"'';
          };
          docker-dcf-python = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-python";
            tag = "latest";
            contents = [ dcf-python-node ];
            config = {
              Entrypoint = [ "${dcf-python-node}/bin/dcf-node" ];
              Cmd = [ "recv" "--follow" ];
              ExposedPorts = { "7801/udp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # C node image: dcfnode (ProtoMessage/UDP, meshes with Go/Rust; plus the
          # Faust modem send-modem/recv-modem over a file medium).
          docker-dcf-c = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-c";
            tag = "latest";
            contents = [ dcf-c pkgs.cacert ];
            config = {
              Entrypoint = [ "${dcf-c}/bin/dcfnode" ];
              Cmd = [ "start" ];
              ExposedPorts = { "7777/udp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # C++ node image: the supercharged gRPC node (dcfcpp).
          docker-dcf-cpp = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-cpp";
            tag = "latest";
            contents = [ dcf-cpp pkgs.cacert ];
            config = {
              Entrypoint = [ "${dcf-cpp}/bin/dcfcpp" ];
              Cmd = [ "serve" ];
              ExposedPorts = { "50051/tcp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # Steam-compatible dedicated-server image: the GameNetworkingSockets hub.
          # `nix build .#docker-dcf-gns`. Clients connect with `dcfcpp connect-gns
          # --peer host:27015`. Steam SDR/lobbies need the developer-supplied
          # Steamworks build (cpp/Dockerfile.steam), not this hermetic image.
          docker-dcf-gns = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-gns";
            tag = "latest";
            contents = [ dcf-cpp-gns pkgs.cacert ];
            config = {
              Entrypoint = [ "${dcf-cpp-gns}/bin/dcfcpp" ];
              Cmd = [ "serve-gns" "--port" "27015" ];
              ExposedPorts = { "27015/udp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # Node.js node: a stdlib `dgram` UDP node (JS/nodejs/src/node.js) in the
          # same bare-DeModFrame + SuperPack dialect as the Python node — they mesh.
          dcf-nodejs-node = pkgs.writeShellApplication {
            name = "dcf-node-js";
            runtimeInputs = [ pkgs.nodejs ];
            text = ''exec node ${self}/JS/nodejs/src/node.js "$@"'';
          };
          docker-dcf-nodejs = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-nodejs";
            tag = "latest";
            contents = [ dcf-nodejs-node ];
            config = {
              Entrypoint = [ "${dcf-nodejs-node}/bin/dcf-node-js" ];
              Cmd = [ "recv" "--follow" ];
              ExposedPorts = { "7801/udp" = { }; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # HydraModem toolbox image: the acoustic-modem CLI tools on PATH (a WAV/file
          # PHY, not a network daemon). Default cmd runs the DCF interop self-test;
          # override with any tool, e.g.
          #   docker run --rm -v "$PWD:/m" alh477/hydramodem frame_tx <34-hex> /m/out.wav
          #   docker run --rm -v "$PWD:/m" alh477/hydramodem frame_rx /m/out.wav
          docker-hydramodem = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/hydramodem";
            tag = "latest";
            contents = [ hydramodem-tools pkgs.bashInteractive pkgs.coreutils ];
            config = {
              Env = [ "PATH=${hydramodem-tools}/bin:${pkgs.coreutils}/bin:${pkgs.bashInteractive}/bin" ];
              Cmd = [ "dcf_loopback" ];
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # Hermetic Lisp SDK node image — the Nix replacement for the traditional
          # lisp/Dockerfile. The wrapped `punctim` binary already carries
          # libstreamdb.so on LD_LIBRARY_PATH (via makeWrapper), so the saved core
          # resolves the DB foreign library at startup.
          docker-punctim = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/punctim";
            tag = "latest";
            contents = [ punctim-lisp pkgs.bashInteractive pkgs.coreutils ];
            config = {
              Entrypoint = [ "${punctim-lisp}/bin/punctim" ];
              Cmd = [ "help" ];
              ExposedPorts = { "7777/udp" = {}; "50051/tcp" = {}; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # LangGraph agent system image: the HTTP API server + CLI + MCP server.
          # Entrypoint is `dcf-agent serve` (HTTP API on :8000). Override cmd for
          # other modes:
          #   docker run alh477/dcf-agent mcp               # MCP server (stdio)
          #   docker run alh477/dcf-agent backends          # list LLM backends
          #   docker run alh477/dcf-agent chat "hi"         # one-shot chat
          # Encryption-free for export control purposes.
          docker-dcf-agent = pkgs.dockerTools.buildLayeredImage {
            name = "alh477/dcf-agent";
            tag = "latest";
            contents = [ agentCLI agentPython pkgs.cacert ];
            config = {
              Entrypoint = [ "${agentCLI}/bin/dcf-agent" ];
              Cmd = [ "serve" "--host" "0.0.0.0" "--port" "8000" ];
              ExposedPorts = { "8000/tcp" = {}; };
              Labels = { "org.opencontainers.image.source" = "https://github.com/ALH477/Punctim"; };
            };
          };

          # Node.js SDK — the published package is the dependency-free wire codec
          # (frame/superpack/fec/text, pure Node stdlib; package.json has no deps).
          # So this is a plain install, not buildNpmPackage: there is no lockfile
          # and no npmDepsHash to pin, which is what made the old derivation
          # unbuildable (fakeHash). Certification runs in the certify-node CI job
          # (it needs Documentation/*_vectors.json, which live outside this src).
          # The UDP node entrypoint is the separate `dcf-nodejs-node` wrapper.
          dcf-nodejs = pkgs.stdenv.mkDerivation {
            pname = "dcf-nodejs";
            version = "0.3.0";
            src = self + "/JS/nodejs";
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p $out/lib/dcf-wire
              cp -r src package.json $out/lib/dcf-wire/
              runHook postInstall
            '';
            meta.description = "Node.js DCF wire codec (certified, dependency-free)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # Perl SDK (wrapper for modules)
          dcf-perl = pkgs.stdenv.mkDerivation {
            pname = "dcf-perl";
            version = "0.3.0";
            src = self + "/perl";
            nativeBuildInputs = [ protobuf pkgs.perl pkgs.perlPackages.GrpcXs ];
            buildInputs = with pkgs.perlPackages; [ JSON IOSocketINET GetoptLong CursesUI GoogleProtocolBuffersDynamic ModulePluggable ];
            preBuild = ''
              ${protobuf}/bin/protoc -I${self} --perl_out=lib ${self}/messages.proto ${self}/services.proto
            '';
            installPhase = "cp -r lib $out/lib";
            meta.description = "Perl SDK for DCF";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # HydraModem — acoustic M-FSK modem for the 17-byte DCF frame (LGPL-3.0,
          # relicensed from Apache-2.0 on integration). Plain Makefile, zero deps
          # (portable reference DSP); `make check` runs the full suite.
          hydramodem = pkgs.stdenv.mkDerivation {
            pname = "hydramodem";
            version = "1.0.0";
            src = self;
            buildPhase = ''
              runHook preBuild
              make -C hydramodem -j''${NIX_BUILD_CORES:-2}
              runHook postBuild
            '';
            doCheck = true;
            checkPhase = "make -C hydramodem check";
            installPhase = "make -C hydramodem PREFIX=$out install";
            meta.description = "HydraModem — acoustic M-FSK modem for the 17-byte Punctim/DCF frame";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # HydraModem built on the COMPILED FAUST DSP backend (not the C
          # reference). Uses pinned Faust 2.72.14 (2.70.x-era `-os` ABI). The
          # build runs `make faust-check`, so success means the real Faust DSP
          # passed the loopback. See hydramodem/docs/FAUST_MODERNIZATION.md.
          hydramodem-faust = pkgs.stdenv.mkDerivation {
            pname = "hydramodem-faust";
            version = "1.0.0";
            src = self;
            nativeBuildInputs = [ faust270 ];
            buildPhase = ''
              runHook preBuild
              make -C hydramodem faust \
                FAUST=${faust270}/bin/faust FAUST_INC=${faust270}/include \
                -j''${NIX_BUILD_CORES:-2}
              runHook postBuild
            '';
            doCheck = true;
            checkPhase = ''
              make -C hydramodem faust-check \
                FAUST=${faust270}/bin/faust FAUST_INC=${faust270}/include
            '';
            installPhase = ''
              mkdir -p $out/lib
              cp hydramodem/build/libhydramodem_faust.a $out/lib/
            '';
            meta.description = "HydraModem on the compiled Faust DSP backend (Faust 2.72.14)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # HydraModem CLI tools (the dcf-tools toolbox: frame_tx/frame_rx, tx/rx
          # campaigns, dcf_loopback interop, sense_node) — static-linked to
          # libhydramodem via dcf-tools/build.sh. This is what the container ships.
          hydramodem-tools = pkgs.stdenv.mkDerivation {
            pname = "hydramodem-tools";
            version = "1.0.0";
            src = self;
            nativeBuildInputs = [ pkgs.gcc pkgs.gnumake pkgs.bash ];
            buildPhase = ''
              runHook preBuild
              bash hydramodem/dcf-tools/build.sh
              runHook postBuild
            '';
            installPhase = ''
              mkdir -p $out/bin
              # Ship everything build.sh actually produces. sstv_send/sstv_recv and
              # snake_loopback were being compiled and then discarded, so the image
              # lacked the SSTV tools the docs advertise. snake_source/snake_mixer
              # need snake_ipc.h from the separate DeMoD audio-stack repo and are
              # skipped in a hermetic build — hence the existence check.
              for t in dcf_loopback tx_campaign rx_campaign frame_tx frame_rx sense_node \
                       sstv_send sstv_recv snake_loopback snake_source snake_mixer; do
                if [ -x hydramodem/dcf-tools/build/$t ]; then
                  install -m755 hydramodem/dcf-tools/build/$t $out/bin/
                fi
              done
            '';
            meta.description = "HydraModem CLI tools (frame_tx/frame_rx, campaigns, sense_node, SSTV, snake)";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # JANUS (NATO STANAG 4748) reference modem — GPL-3.0, CMRE-derived.
          # A STANDALONE package: the DCF `janus:` transport invokes janus-tx /
          # janus-rx as a separate process (never linked), so this GPL build is
          # NOT in any LGPL package's closure. Installs the binaries + the
          # parameter-set CSV the transport auto-discovers.
          janus-c = pkgs.stdenv.mkDerivation {
            pname = "janus-c";
            version = "unstable";
            src = janus-c-src;
            nativeBuildInputs = [ pkgs.cmake pkgs.pkg-config ];
            buildInputs = [ pkgs.fftw pkgs.fftwFloat pkgs.alsa-lib ];
            # The upstream CMakeLists predates CMake 4's minimum-policy removal.
            cmakeFlags = [ "-DCMAKE_POLICY_VERSION_MINIMUM=3.5" ];
            meta.description = "JANUS (STANAG 4748) underwater acoustic modem reference (janus-tx/janus-rx)";
            meta.license = pkgs.lib.licenses.gpl3Only;
          };

          # DeMoD quanta codec (Gabor-atom matching pursuit + QSS streaming) — GPL-3.0-only
          # OR DeMoD Commercial. A STANDALONE package: the DCF-Snake record plane invokes
          # quanta-stream / quanta-stream-decode as separate processes (never linked), so
          # this GPL build is NOT in any LGPL package's closure. Only libm; builds with the
          # quanta Makefile's own determinism CFLAGS. See Documentation/DCF_SNAKE_SPEC.md.
          quanta = pkgs.stdenv.mkDerivation {
            pname = "quanta";
            version = "unstable";
            src = "${demod-quanta-src}/quanta";
            buildPhase = ''
              runHook preBuild
              mkdir -p bin
              make bin/quanta-stream bin/quanta-stream-decode
              runHook postBuild
            '';
            installPhase = ''
              runHook preInstall
              mkdir -p $out/bin
              cp bin/quanta-stream bin/quanta-stream-decode $out/bin/
              runHook postInstall
            '';
            meta.description = "DeMoD quanta codec streaming encoder/decoder (quanta-stream/quanta-stream-decode)";
            meta.license = pkgs.lib.licenses.gpl3Only;
          };

          # Docs
          dcf-docs = pkgs.stdenv.mkDerivation {
            pname = "dcf-docs";
            version = "0.3.0";
            src = self + "/Documentation";
            nativeBuildInputs = pkgs.python3.withPackages (ps: with ps; [ sphinx myst-parser ]);
            buildPhase = "make html";
            installPhase = "cp -r _build/html $out";
            meta.description = "DCF Documentation";
            meta.license = pkgs.lib.licenses.lgpl3Only;
          };

          # Default package (e.g., all SDKs)
          default = dcf-c; # Or a collection
        };

        # Runnable entry points for the agent-to-agent mesh feature.
        #   nix run github:ALH477/Punctim#a2a         guided setup + run
        #   nix run github:ALH477/Punctim#a2a-demo    stdlib loopback smoke test
        #   nix run github:ALH477/Punctim#a2a-config  generate an MCP config for any agent client
        #   nix run github:ALH477/Punctim#a2a-send    send a message onto the mesh (no MCP)
        #   nix run github:ALH477/Punctim#a2a-recv    receive mesh text (no MCP)
        #   nix run github:ALH477/Punctim#mesh-agent  the DeModFrame MCP endpoint (stdio)
        #   nix run github:ALH477/Punctim#mesh-http   shared HTTP mesh service (mesh_mcp.py http)
        #   nix run github:ALH477/Punctim#mesh-viz    live web dashboard of mesh agents
        apps =
          let
            # The matrix-bridge/*.py scripts self-locate their imports (they add
            # matrix-bridge/ and python/MCP/ to sys.path), so they only need a
            # Python interpreter on PATH — no install/build step.
            mkA2A = name: tail: pkgs.writeShellApplication {
              inherit name;
              runtimeInputs = [ pkgs.python3 ];
              text = ''exec python3 ${self}/matrix-bridge/${tail} "$@"'';
            };
            interactive = mkA2A "dcf-a2a" "a2a_interactive.py";
            demo = mkA2A "dcf-a2a-demo" "a2a_runner.py --demo";
            viz = mkA2A "dcf-mesh-viz" "mesh_viz.py";   # stdlib web dashboard
            # mesh_mcp.py is the MCP server an agent connects to; it needs `mcp`.
            meshPython = pkgs.python3.withPackages (ps: [ ps.mcp ]);
            meshAgent = pkgs.writeShellApplication {
              name = "dcf-mesh-agent";
              runtimeInputs = [ meshPython ];
              text = ''exec ${meshPython}/bin/python ${self}/matrix-bridge/mesh_mcp.py "$@"'';
            };
            # Multi-agent client integration: the a2a.py umbrella (config/send/recv are
            # stdlib) and a shared HTTP mesh service (mesh_mcp.py http, needs `mcp`).
            a2aConfig = mkA2A "dcf-a2a-config" "a2a.py config";
            a2aSend = mkA2A "dcf-a2a-send" "a2a.py send";
            a2aRecv = mkA2A "dcf-a2a-recv" "a2a.py recv";
            meshHttp = pkgs.writeShellApplication {
              name = "dcf-mesh-http";
              runtimeInputs = [ meshPython ];
              text = ''exec ${meshPython}/bin/python ${self}/matrix-bridge/mesh_mcp.py http "$@"'';
            };
            # DCF-QKD: the ETSI GS QKD 014 end-to-end harness — no network and no
            # photonic hardware, running the flow over a loopback transport against a
            # mock KME pair.  dcf/qkd/ is itself stdlib-only, but it composes with
            # dcf.transport, which imports the acoustic/IQ modems and so needs numpy
            # (the one runtime dependency python/pyproject.toml declares).
            qkdPython = pkgs.python3.withPackages (ps: [ ps.numpy ]);
            qkdDemo = pkgs.writeShellApplication {
              name = "dcf-qkd-demo";
              runtimeInputs = [ qkdPython ];
              text = ''exec ${qkdPython}/bin/python ${self}/python/dcf/qkd/demo.py "$@"'';
            };
            mkApp = drv: bin: { type = "app"; program = "${drv}/bin/${bin}"; };
          in {
            a2a = mkApp interactive "dcf-a2a";
            a2a-demo = mkApp demo "dcf-a2a-demo";
            a2a-config = mkApp a2aConfig "dcf-a2a-config";
            a2a-send = mkApp a2aSend "dcf-a2a-send";
            a2a-recv = mkApp a2aRecv "dcf-a2a-recv";
            mesh-agent = mkApp meshAgent "dcf-mesh-agent";
            mesh-http = mkApp meshHttp "dcf-mesh-http";
            mesh-viz = mkApp viz "dcf-mesh-viz";
            agent = mkApp agentCLI "dcf-agent";
            agent-tui = mkApp agentTUI "dcf-agent-tui";
            agent-serve = mkApp agentServe "dcf-agent-serve";
            agent-mcp = mkApp agentMCP "dcf-agent-mcp";
            qkd-demo = mkApp qkdDemo "dcf-qkd-demo";
            default = mkApp interactive "dcf-a2a";
          };

        devShells = {
          default = pkgs.mkShell {
            packages = [
              protobuf pkgs.cmake pkgs.grpc pkgs.rustc pkgs.cargo pkgs.go
              pkgs.nodejs pkgs.python3 pkgs.perl pkgs.sbcl
              # DCF-Audio -> ffmpeg recording + radio: the `dcf` demuxer, dcf-rec,
              # and the dcf-radio (HLS + DVR) streamer.
              self.packages.${system}.dcf-ffmpeg self.packages.${system}.dcf-rec
              self.packages.${system}.dcf-radio
            ];
            shellHook = ''
              export PROTOC=${protobuf}/bin/protoc
            '';
            meta.description = "Dev shell for DCF mono repo";
          };

          # SDR / RF: Faust + SDR tooling + dcf-sdr. Pipe .cf32 to rtl_sdr /
          # hackrf_transfer / GNU Radio, or drive a SoapySDR device. See DCF_SDR_SPEC.md.
          sdr = pkgs.mkShell {
            packages = [
              pkgs.faust pkgs.rtl-sdr pkgs.hackrf pkgs.soapysdr
              (pkgs.python3.withPackages (ps: [ ps.numpy ]))
              self.packages.${system}.dcf-sdr
            ];
            shellHook = ''
              echo "◈ DCF-SDR shell — dcf-sdr tx/rx; .cf32 <-> rtl_sdr / hackrf_transfer"
            '';
            meta.description = "DCF SDR/RF dev shell (FEC modem + SoapySDR)";
          };

          # DCF-JANUS: the GPL janus-c reference (janus-tx/janus-rx) on PATH for
          # the `janus:` transport. GPL lives only in this shell/subprocess, never
          # linked into the LGPL library.
          janus = pkgs.mkShell {
            packages = [
              self.packages.${system}.janus-c
              (pkgs.python3.withPackages (ps: with ps; [ numpy ]))
            ];
            shellHook = ''
              echo "◈ DCF-JANUS shell — janus-tx/janus-rx (STANAG 4748, GPL-3.0) on PATH"
              echo "  cd python && python3 -m unittest tests.test_transport -k janus -v"
            '';
            meta.description = "DCF-JANUS dev shell (GPL janus-c reference + python)";
          };

          # HydraModem with the compiled-Faust DSP toolchain (pinned Faust
          # 2.72.14). FAUST_INC is exported so `make faust` / `make faust-check`
          # in hydramodem/ build the real Faust backend out of the box.
          hydramodem-faust = pkgs.mkShell {
            packages = [ faust270 pkgs.gcc pkgs.gnumake ];
            shellHook = ''
              export FAUST=${faust270}/bin/faust
              export FAUST_INC=${faust270}/include
              echo "◈ HydraModem Faust shell — $(${faust270}/bin/faust --version | head -1)"
              echo "  cd hydramodem && make faust-check    # build+run the compiled Faust DSP"
            '';
            meta.description = "HydraModem compiled-Faust DSP shell (Faust 2.72.14)";
          };

          lisp = pkgs.mkShell {
            packages = [ pkgs.sbcl ];
            inputsFrom = [ self.packages.${system}.streamdb ];
            shellHook = ''
              export LD_LIBRARY_PATH=${self.packages.${system}.streamdb}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
              echo "Lisp SDK dev shell. libstreamdb.so on LD_LIBRARY_PATH."
              echo "  Load the SDK:  sbcl --load ${self}/lisp/src/punctim.lisp"
              echo "  Run tests:     sbcl --load ${self}/lisp/src/punctim.lisp --eval '(d-lisp::run-tests)'"
            '';
            meta.description = "Lisp SDK dev shell";
          };

          # Punctim communications client (Tauri 2: client/). Build/run with:
          #   nix develop .#comms
          #   cd client && npm install && cargo tauri dev
          comms = pkgs.mkShell {
            nativeBuildInputs = [
              pkgs.pkg-config pkgs.rustc pkgs.cargo pkgs.nodejs pkgs.cargo-tauri
              # so the Radio tab's "Broadcast" can spawn dcf-radio off PATH
              self.packages.${system}.dcf-radio self.packages.${system}.dcf-ffmpeg
            ];
            buildInputs = [
              # Tauri webview (Linux)
              pkgs.glib pkgs.gtk3 pkgs.webkitgtk_4_1 pkgs.libsoup_3
              pkgs.gdk-pixbuf pkgs.cairo pkgs.pango pkgs.atk pkgs.openssl
              pkgs.dbus pkgs.librsvg
              # Real-time audio + Opus (cpal + the `audio` feature)
              pkgs.alsa-lib pkgs.libopus
            ];
            shellHook = ''
              echo "◈ Punctim comms client — cd client && npm install && cargo tauri dev"
            '';
            meta.description = "Punctim Tauri comms client dev shell";
          };

          # Browser WASM client (codec-wasm/ + web/) and the WS↔UDP bridge.
          #   nix develop .#wasm
          #   (cd web && npm install && npm run build)   # → web/dist/index.html (one file)
          #   node web/certify/certify_wasm.mjs           # byte-cert vs golden vectors
          #   cargo run --manifest-path web/bridge/Cargo.toml -- --listen 127.0.0.1:7000
          wasm = pkgs.mkShell {
            packages = [
              rustWasm pkgs.wasm-pack pkgs.wasm-bindgen-cli pkgs.binaryen
              pkgs.nodejs pkgs.pkg-config
            ];
            shellHook = ''
              echo "◈ Punctim WASM shell"
              echo "  cd web && npm install && npm run build   # single inlined index.html"
              echo "  npm --prefix web run certify             # cert WASM vs golden vectors"
              echo "  cargo run --manifest-path web/bridge/Cargo.toml -- --listen 127.0.0.1:7000"
            '';
            meta.description = "Punctim browser WASM client + WS↔UDP bridge dev shell";
          };

          # LangGraph agent system (langgraph_agents/). The CLI and TUI are
          # available as `nix run .#agent` / `nix run .#agent-tui`; this shell
          # is for development — run tests, edit code, iterate.
          #   nix develop .#agents
          #   dcf-agent backends                      # list LLM backends
          #   dcf-agent chat --backend echo "hi"      # one-shot
          #   dcf-agent agents --config langgraph_agents/agents.jsonc
          #   dcf-agent-tui                            # interactive TUI
          #   cd langgraph_agents && pytest -v         # run tests
          agents = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: with ps; [
                langgraph langchain-core httpx pydantic rich textual
                fastapi uvicorn mcp
                pytest pytest-asyncio
              ]))
            ];
            shellHook = ''
              export PYTHONPATH="${self}/langgraph_agents''${PYTHONPATH:+:$PYTHONPATH}"
              echo "◈ Punctim LangGraph agent shell"
              echo "  dcf-agent backends                     # list LLM backends"
              echo "  dcf-agent chat --backend echo 'hi'     # one-shot chat"
              echo "  dcf-agent agents --config langgraph_agents/agents.jsonc"
              echo "  dcf-agent tui                           # interactive TUI"
              echo "  dcf-agent serve --port 8000             # HTTP API server"
              echo "  dcf-agent mcp                            # MCP server (stdio)"
              echo "  cd langgraph_agents && pytest -v        # run tests"
            '';
            meta.description = "Punctim LangGraph agent CLI/TUI/API/MCP dev shell";
          };
        };
      }
    );
}
