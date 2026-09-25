# Installation Script for DeMoD Communications Framework (DCF) Dependencies

## Overview
The `install_deps.sh` script automates the setup of dependencies for the DeMoD Communications Framework (DCF) mono repository, supporting SDKs in Perl, Python, C, C++, Node.js, Go, Rust, and potentially Common Lisp. It is designed to work on Debian-based (Debian/Ubuntu), Arch-based (Arch/Manjaro), and Fedora-based (Fedora/RHEL/CentOS) Linux distributions. The script installs system packages and language-specific dependencies, includes robust error handling, and supports Quicklisp for Common Lisp. It is tailored for the DCF repo at [https://github.com/ALH477/DeMoD-Communication-Framework](https://github.com/ALH477/DeMoD-Communication-Framework).

## Features
- **Distro Detection**: Identifies Debian, Arch, or Fedora-based systems using `/etc/os-release`.
- **System Packages**: Installs tools and libraries (e.g., `git`, `cmake`, `protobuf`, `sbcl`) using `apt`, `pacman`, or `dnf`.
- **Language-Specific Dependencies**:
  - Perl: Installs CPAN modules (e.g., `JSON`, `Grpc::XS`).
  - Python: Installs pip modules (e.g., `protobuf`, `grpcio`).
  - Node.js: Installs npm modules (e.g., `@grpc/grpc-js`).
  - Go: Installs Go modules (e.g., `protoc-gen-go-grpc`).
  - Rust: Installs Rust via `rustup` and crates (e.g., `tonic-build`).
  - Common Lisp: Installs Quicklisp for SBCL.
- **Robust Error Handling**: Logs errors without halting execution for non-critical failures, providing a summary at the end.
- **User Interaction**: Prompts for confirmation before installing system or language-specific dependencies.
- **Non-Interactive Support**: Uses flags like `-y`, `--noconfirm` for automation.

## Prerequisites
- A supported Linux distribution (Debian/Ubuntu, Arch/Manjaro, Fedora/RHEL/CentOS).
- Internet access for downloading packages and language-specific dependencies.
- `sudo` privileges for installing system packages.
- Bash shell (default on most Linux systems).

## Usage
1. **Save the Script**:
   Save the script as `install_deps.sh`.

2. **Make Executable**:
   ```bash
   chmod +x install_deps.sh
   ```

3. **Run the Script**:
   Run with `sudo` for system package installation:
   ```bash
   sudo ./install_deps.sh
   ```

4. **Follow Prompts**:
   - The script will prompt: `Install system packages? (y/n)`. Enter `y` or `Y` to proceed.
   - It will then prompt: `Install language-specific dependencies? (y/n)`. Enter `y` or `Y` to proceed.
   - If errors occur, they are logged and summarized at the end.

5. **Post-Installation**:
   - Clone the DCF repo:
     ```bash
     git clone --recurse-submodules https://github.com/ALH477/DeMoD-Communication-Framework.git
     ```
   - Follow the build instructions in the DCF repo’s `README.md` (e.g., generate Protobuf/gRPC stubs, build SDKs).

## Installed Dependencies
### System Packages
- Common: `git`, `cmake`, `make`, `protobuf-compiler`, `libprotobuf-dev`/`protobuf-devel`, `libgrpc++-dev`/`grpc-devel`, `libprotobuf-c-dev`/`protobuf-c-devel`, `uuid-dev`/`util-linux`/`libuuid-devel`, `libcjson-dev`/`cjson`/`json-c-devel`, `libncurses-dev`/`ncurses`/`ncurses-devel`, `clang-format`/`clang-tools-extra`, `valgrind`, `python3`, `python3-pip`, `perl`, `libapp-cpanminus-perl`/`perl-app-cpanminus`, `nodejs`, `npm`, `go`/`golang`, `openssl`/`openssl-devel`, `sbcl`.
- Debian-Specific: `libc6-dev` (for `libdl`).
- Arch-Specific: `glibc` (for `libdl`), `python-pip` (pip alias).
- Fedora-Specific: `glibc-devel` (for `libdl`), `clang-tools-extra` (for `clang-format`).

### Language-Specific Dependencies
- **Perl (CPAN)**: `JSON`, `IO::Socket::INET`, `Getopt::Long`, `Curses::UI`, `Google::ProtocolBuffers::Dynamic`, `Grpc::XS`, `Module::Pluggable`.
- **Python (pip)**: `protobuf`, `grpcio`, `grpcio-tools`.
- **Node.js (npm)**: `@grpc/grpc-js`, `@grpc/proto-loader`.
- **Go**: `google.golang.org/grpc/cmd/protoc-gen-go-grpc`, `google.golang.org/protobuf/cmd/protoc-gen-go`.
- **Rust**: `rustup` (stable toolchain), `tonic-build`, `prost-build`.
- **Common Lisp**: Quicklisp (installed via SBCL).

## Error Handling
- **Non-Critical Failures**: If a package or module fails to install (e.g., unavailable in repos), the script logs the error and continues, allowing partial success.
- **Critical Failures**: The script exits immediately if:
  - `/etc/os-release` is missing.
  - The distro is unsupported.
- **Error Summary**: At the end, the script reports all errors, listing each issue and suggesting manual installation for failed packages.

## Notes and Limitations
- **Android/iOS**: The script does not cover Android (Java/Kotlin) or iOS (Swift) dependencies, which require Android Studio or Xcode.
- **Root Privileges**: System packages require `sudo`. Language-specific installs use `--user` for Python (pip) and user-level paths for Rust/Go where possible.
- **Package Availability**: If a package is missing (e.g., `cjson` on older distros), the script logs the error. Users must install manually or update repositories.
- **Quicklisp**: Requires `sbcl`. If SBCL is unavailable, Quicklisp installation is skipped with a logged error.
- **Network Dependency**: Requires internet for downloading `rustup`, Quicklisp, and language-specific packages.
- **Verification**: After running, verify installations (e.g., `sbcl --version`, `protoc --version`, `cmake --version`) before building the DCF repo.

## Troubleshooting
- **Error: Package not found**: Ensure your package manager’s repositories are up-to-date. For Debian, add `contrib`/`non-free` if needed. For Fedora, enable EPEL for some packages.
- **Error: Permission denied**: Run with `sudo` for system packages. For language-specific installs, `--user` (pip) or user-level paths (Rust/Go) avoid this.
- **Quicklisp fails**: Verify `sbcl` is installed (`sbcl --version`). Manually download and install Quicklisp if needed: `curl -O https://beta.quicklisp.org/quicklisp.lisp`.
- **Rust issues**: Ensure `$HOME/.cargo/env` is sourced. Run `source $HOME/.cargo/env` manually if needed.
- **Check logs**: Review the error summary at the script’s end for specific failures.

## Contributing
If you encounter issues or want to add support for other distros, contribute to the DCF repo:
1. Fork the repo: [https://github.com/ALH477/DeMoD-Communication-Framework](https://github.com/ALH477/DeMoD-Communication-Framework).
2. Create a feature branch: `git checkout -b feature/install-script`.
3. Update `install_deps.sh` and this README.
4. Submit a pull request using the repo’s PR template.
5. Discuss issues via [GitHub Issues](https://github.com/ALH477/DeMoD-Communication-Framework/issues).

## License
This script is part of the DCF project and is licensed under the GNU Lesser General Public License v3.0 only (LGPL-3.0-only). See the DCF repo’s `LICENSE` file for details.
