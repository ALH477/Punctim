### Workflow with This Punctim Flake

This Nix flake provides a **reproducible, production-grade environment** for developing, testing, and deploying the Punctim (D-LISP) SDK. It prioritizes an **Emacs + SLY-centric workflow** (no Quicklisp required), leverages Nixpkgs' built-in Common Lisp packages where possible, and ensures reliable builds of the standalone executable.

#### Key Features of the Flake
- **Dependencies**: Core Common Lisp libraries (e.g., `cffi`, `usocket`, `bordeaux-threads`, `log4cl`, `flexi-streams`, `cl-json`, `jsonschema`, etc.) are provided via `sbcl.withPackages` from Nixpkgs' `lispPackages` (or `sbclPackages` scope).
- **Emacs Integration**: Native-compiled Emacs with SLY pre-configured for seamless SBCL interaction.
- **Build Output**: A standalone `punctim` executable via `nix build`.
- **Development Shell**: Interactive REPL with all deps loaded.
- **System Requirements**: Runtime libs like `grpc`, `protobuf`, `openssl`, `zlib` for CFFI bindings; `libstreamdb.*` must be in your path separately (not packaged here).

#### 1. Setup and Enter the Development Environment
```bash
# Clone the repo (assuming flake.nix is in the root)
git clone https://github.com/your-repo/punctim/lisp.git
cd punctim

# Enter the dev shell (reproducible environment)
nix develop
```
- This drops you into a shell with:
  - SBCL pre-loaded with all required Lisp dependencies.
  - Emacs configured for SLY.
  - Helpful banner with instructions.
- A minimal `~/.emacs.d/init.el` is auto-generated on first run:
  ```elisp
  (require 'sly)
  (setq inferior-lisp-program "/path/to/sbcl-with-deps")
  (add-hook 'lisp-mode-hook #'sly-mode)
  ```
  This enables SLY in Lisp files and sets the correct SBCL binary.

#### 2. Daily Development Workflow (Emacs + SLY)
```bash
# From inside `nix develop`
emacs punctim.lisp  # Or any file in the project
```
Inside Emacs:
- `M-x sly` → Starts a SLY REPL connected to the pre-loaded SBCL (all deps like `cl-protobufs`, `usocket`, etc., are immediately available).
- Open `punctim.lisp` (or other files).
- Evaluate code interactively:
  - `C-x C-e`: Eval last expression.
  - `C-c C-r`: Eval region.
  - `C-c C-c`: Compile/defun at point.
- Use SLY features:
  - Autocompletion, inspector, debugger, stickers (live code instrumentation).
  - Multiple REPLs if needed.
- Test the code:
  - In REPL: `(fiveam:run! 'punctim-suite)`
- Hot-reload changes without restarting the REPL.

This is the **primary intended workflow** — highly interactive, Lisp-native development without manual dependency management.

#### 3. Building the Standalone Executable
```bash
# From the project root (no need for `nix develop`)
nix build .#

# Or specifically
nix build .#punctim
```
- Result: `./result/bin/punctim` (standalone binary).
- How it works:
  - Uses the same `sbclWithDeps` (SBCL + all Lisp packages).
  - Loads `punctim.lisp`.
  - Calls `(dcf-deploy "punctim")` → SBCL's `save-lisp-and-die` creates a core + executable.
  - `dontStrip = true` prevents binary stripping (critical for SBCL executables).
- Run it: `./result/bin/punctim help` (or other CLI commands from the code).

#### 4. Running Tests or CLI Outside Emacs
```bash
nix develop
sbcl  # Starts REPL with deps loaded
```
In SBCL REPL:
```lisp
(ql:quickload :punctim)  ; If needed, but deps are pre-loaded
(fiveam:run! 'punctim-suite)
```
Or run the built binary directly.

#### 5. Deployment / Distribution
- Share the flake: Others can `nix build` on any Nix-enabled system → identical executable.
- For servers/containers: Use the built binary (copy from `result/bin`).
- Runtime note: Ensure `libstreamdb.so` (or equivalent) is available in `LD_LIBRARY_PATH` for StreamDB features.

#### Limitations & Notes
- Not all Quicklisp systems are in Nixpkgs (e.g., some niche ones may be missing as of late 2025). If a dep fails, add it manually or fall back to ASDF/Quicklisp in dev.
- `libstreamdb` is a custom C lib → provide it separately (e.g., via system packages or another derivation).
- For advanced users: `roswell` is included in the shell for alternative Lisp management.

This workflow combines **Nix reproducibility** with **classic Lisp interactivity** via Emacs/SLY, making it ideal for both development and production deployment of Punctim.
