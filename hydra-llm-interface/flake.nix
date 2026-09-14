# Copyright (C) 2025 DeMoD LLC
#
# This file is part of the Punctim LLM Interface System.
#
# The Punctim LLM Interface System is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# The Punctim LLM Interface System is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

{
  description = "Reproducible environment for LLM-Punctim interface";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
    rust-overlay.url = "github:oxalica/rust-overlay";
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        overlays = [ (import rust-overlay) ];
        pkgs = import nixpkgs { inherit system overlays; };

        rustToolchain = pkgs.rust-bin.stable.latest.default.override {
          extensions = [ "rust-src" "cargo" "rustc" ];
        };

        streamdb = pkgs.stdenv.mkDerivation {
          name = "libstreamdb";
          src = ./src/rust;
          nativeBuildInputs = [ rustToolchain pkgs.cargo ];
          buildPhase = ''
            cargo build --release
            cp target/release/libstreamdb.so $out/lib/
          '';
          installPhase = ''
            mkdir -p $out/lib
            cp target/release/libstreamdb.so $out/lib/
          '';
        };

        pythonEnv = pkgs.python312.withPackages (ps: with ps; [
          requests
          grpcio
          grpcio-tools
          protobuf
          aiohttp
          cryptography
        ]);

      in {
        packages.default = pkgs.stdenv.mkDerivation {
          name = "llm-hydra-interface";
          src = ./src/python;
          buildInputs = [ pythonEnv streamdb ];
          installPhase = ''
            mkdir -p $out/bin $out/lib
            cp llm_hydra_interface.py $out/bin/
            chmod +x $out/bin/llm_hydra_interface.py
            cp ${streamdb}/lib/libstreamdb.so $out/lib/
          '';
          shellHook = ''
            export LD_LIBRARY_PATH=$out/lib:$LD_LIBRARY_PATH
            export PYTHONPATH=${pythonEnv}/lib/python3.12/site-packages:$PYTHONPATH
          '';
          meta = {
            description = "LLM interface with Punctim";
            mainProgram = "llm_hydra_interface.py";
          };
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [ pythonEnv streamdb pkgs.grpc-tools ];
          shellHook = ''
            export LD_LIBRARY_PATH=${streamdb}/lib:$LD_LIBRARY_PATH
            export PYTHONPATH=${pythonEnv}/lib/python3.12/site-packages:$PYTHONPATH
            echo "Environment ready. Run: python llm_hydra_interface.py"
          '';
        };
      }
    );
}
