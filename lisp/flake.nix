{
  description = "Nix flake for Punctim (D-LISP) SDK – Emacs + SLY focused development";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    emacs-overlay.url = "github:nix-community/emacs-overlay";
  };

  outputs = { self, nixpkgs, flake-utils, emacs-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ emacs-overlay.overlays.default ];
        };

        # Required Quicklisp systems — kept in sync with the actual
        # (ql:quickload ...) / defpackage list in src/punctim.lisp. cl-protobufs
        # and jsonschema were dropped from the source (and jsonschema is not in
        # nixpkgs lispPackages), so they are not listed here.
        qlSystems = [
          "cffi" "uuid" "usocket" "bordeaux-threads"
          "log4cl" "trivial-backtrace" "flexi-streams" "fiveam"
          "ieee-floats" "cl-json"
        ];

        # SBCL with all required dependencies pre-loaded via Nixpkgs lispPackages
        sbclWithDeps = pkgs.sbcl.withPackages (ps: with ps; [
          cffi uuid usocket bordeaux-threads
          log4cl trivial-backtrace flexi-streams fiveam
          ieee-floats cl-json
        ]);

        # Emacs with native compilation and SLY
        emacsWithSly = pkgs.emacsNativeComp.pkgs.withPackages (epkgs: [
          epkgs.sly
        ]);

        # The Punctim executable – robust production build
        punctim = pkgs.stdenv.mkDerivation {
          pname = "punctim";
          version = "2.2.0";

          src = self;

          nativeBuildInputs = [ sbclWithDeps ];

          # Critical: prevent stripping which breaks SBCL executables
          dontStrip = true;

          buildPhase = ''
            ${sbclWithDeps}/bin/sbcl --no-userinit --non-interactive \
              --load src/punctim.lisp \
              --eval '(in-package :d-lisp)' \
              --eval '(dcf-deploy "punctim")' \
              --quit
          '';

          installPhase = ''
            mkdir -p $out/bin
            cp punctim $out/bin/punctim
          '';

          meta = with pkgs.lib; {
            description = "Punctim SDK executable";
            license = licenses.lgpl3Only;
            platforms = platforms.all;
          };
        };

      in {
        packages.default = punctim;

        devShells.default = pkgs.mkShell {
          buildInputs = [
            sbclWithDeps
            emacsWithSly
            pkgs.grpc
            pkgs.protobuf
            pkgs.openssl
            pkgs.zlib
            # Optional: for easier dependency management during dev
            pkgs.roswell
          ];

          shellHook = ''
            # Create minimal Emacs init with SLY configured for SBCL
            mkdir -p $HOME/.emacs.d
            cat > $HOME/.emacs.d/init.el <<'EOF'
            ;; Minimal SLY configuration for Punctim development
            (require 'sly)
            (setq inferior-lisp-program "${sbclWithDeps}/bin/sbcl")
            (add-hook 'lisp-mode-hook #'sly-mode)
            (add-hook 'slime-repl-mode-hook #'sly-mrepl-mode)
            ;; Optional: enable company completion
            (add-hook 'sly-mode-hook #'company-mode)
            EOF

            echo "══════════════════════════════════════════════════════════════"
            echo "Punctim development shell (Emacs + SLY) ready!"
            echo "• Start Emacs: emacs"
            echo "• In Emacs: M-x sly  → connects to SBCL with all deps loaded"
            echo "• Open punctim.lisp and evaluate forms with C-x C-e"
            echo "• Build executable: nix build .#"
            echo "• Run tests: (fiveam:run! 'punctim-suite) in REPL"
            echo "══════════════════════════════════════════════════════════════"
          '';
        };
      }
    );
}
