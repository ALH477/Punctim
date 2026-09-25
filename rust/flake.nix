{
  description = "DCF Remote Node - Static Production Build";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    
    # Use rust-overlay to pin exact versions (matches your Dockerfile's 1.83)
    rust-overlay.url = "github:oxalica/rust-overlay";
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        # 1. Overlay specific Rust version
        overlays = [ (import rust-overlay) ];
        pkgs = import nixpkgs { inherit system overlays; };

        # 2. Define the exact toolchain we want (1.83.0)
        # We explicitly add the 'musl' target for static linking
        rustToolchain = pkgs.rust-bin.stable."1.83.0".default.override {
          targets = [ "x86_64-unknown-linux-musl" ]; 
        };

        # 3. Create a static platform using the pinned toolchain
        # This forces the entire build to be static (Musl instead of Glibc)
        staticPlatform = pkgs.makeRustPlatform {
          cargo = rustToolchain;
          rustc = rustToolchain;
        };

        # 4. Source Filtering (Clean builds)
        src = pkgs.lib.cleanSourceWith {
          src = ./.;
          filter = path: type:
            let base = baseNameOf path; in
            !(type == "directory" && base == "target") &&
            !(type == "directory" && base == ".git") &&
            base != "flake.lock";
        };

        cargoToml = builtins.fromTOML (builtins.readFile ./Cargo.toml);
        version = cargoToml.package.version;
        pname = "dcf-node";

        # ---------------------------------------------------------------------
        # BUILD DEFINITION (Static)
        # ---------------------------------------------------------------------
        dcf-node-static = staticPlatform.buildRustPackage {
          inherit pname version src;
          cargoLock.lockFile = ./Cargo.lock;

          # Dependencies for Static Build
          nativeBuildInputs = with pkgs; [ 
            pkg-config 
            protobuf
            pkgsBuildHost.rustPlatform.bindgenHook # Sometimes needed for static sys-crates
          ];
          
          # We use pkgs.pkgsStatic to get static versions of C libraries (openssl, etc)
          buildInputs = with pkgs.pkgsStatic; [ 
            openssl 
          ];

          # Force static linking env vars
          RUSTFLAGS = "-C target-feature=+crt-static";

          # PATCH: Fix the build.rs read-only FS issue
          preConfigure = ''
            sed -i '/.out_dir("src\/generated")/d' build.rs
          '';

          # Skip tests in static builds (often require networking/runners)
          doCheck = false;

          # Licence of the built artefact = licence of the source it is built from
          # (rust/Cargo.toml: `license = "LGPL-3.0-only"`), matching every other
          # package in the tree. Note the binary is statically linked, so a
          # redistributor relying on LGPLv3 s4 must also ship the means to relink.
          meta = {
            description = "DCF Rust SDK node (dcf), statically linked";
            license = pkgs.lib.licenses.lgpl3Only;
            mainProgram = "dcf";
          };
        };

        # ---------------------------------------------------------------------
        # CONTAINER DEFINITION (Distroless Static)
        # ---------------------------------------------------------------------
        dockerImage = pkgs.dockerTools.streamLayeredImage {
          name = "alh477/${pname}";
          tag = version;
          
          # Since the binary is static, we need ZERO runtime libraries.
          # We only need SSL certs and Timezone data.
          contents = [ 
            pkgs.cacert 
            pkgs.tzdata 
          ];
          
          config = {
            User = "1000:1000";
            ExposedPorts = {
              "7777/udp" = {};
              "50051/tcp" = {};
            };
            Env = [
              "DCF_CONFIG=/etc/dcf/config.toml"
              "RUST_LOG=info"
            ];
            # ENTRYPOINT is the executable
            Entrypoint = [ "${dcf-node-static}/bin/dcf" ];
            # CMD is the default argument (matches your Dockerfile)
            Cmd = [ "start" ];
          };

          # The image ships dcf-node-static, so it carries that licence.
          meta = {
            description = "DCF Rust SDK node container (distroless, static)";
            license = pkgs.lib.licenses.lgpl3Only;
          };
        };

        # ---------------------------------------------------------------------
        # SCRIPTS
        # ---------------------------------------------------------------------
        mkConfigScript = pkgs.writeShellScriptBin "dcf-init-config" ''
          if [ -f "./config.toml" ]; then
             echo "ℹ️  config.toml exists."
          else
             echo "Creating default config.toml..."
             cat > config.toml <<EOF
[node]
id = "node-$(hostname)-01"
hub_url = "https://dcf.demod.ltd"
[network]
bind_addr = "0.0.0.0"
bind_port = 7777
EOF
          fi
          ''${EDITOR:-vim} config.toml
        '';

      in
      {
        # Default package is the STATIC binary
        packages.default = dcf-node-static;
        
        # Container target
        packages.container = dockerImage;

        # Development Shell
        devShells.default = pkgs.mkShell {
          buildInputs = [
            # Dev tools
            rustToolchain
            pkgs.pkg-config
            pkgs.openssl
            pkgs.protobuf
            pkgs.docker-compose
            
            # Helper scripts
            mkConfigScript
          ];

          # Set up env so 'cargo run' works locally
          shellHook = ''
            export PROTOC="${pkgs.protobuf}/bin/protoc"
            echo "🛡️  DCF-Node DevShell (Rust 1.83)"
            echo "   dcf-init-config  -> Setup Config"
          '';
        };
      }
    );
}
