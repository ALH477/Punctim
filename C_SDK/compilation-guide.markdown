# Compilation Guide for DCF C SDK Across Platforms

This guide provides detailed instructions for compiling the DeMoD Communications Framework (DCF) C SDK from the monorepo at [github.com/ALH477/DeMoD-Communication-Framework](https://github.com/ALH477/DeMoD-Communication-Framework) on various platforms, including Windows, BSD, macOS, WebAssembly (Wasm), and mobile ARM64 (Android/iOS). The C SDK supports client, server, P2P, and AUTO modes with gRPC/Protobuf, RTT-based grouping, and plugins. Builds are optimized for low-latency (<1ms local RTT) and <5% CPU usage on Raspberry Pi-like devices, with Valgrind-compatible memory management.

All builds assume LGPL-3.0-only compliance and no built-in encryption for export regulations. Dependencies include CMake (>=3.10), Protobuf-C (>=1.4.1), libuuid, cJSON (>=1.7.18), gRPC (>=1.54), and ncurses (>=6.4). Use `git clone --recurse-submodules https://github.com/ALH477/DeMoD-Communication-Framework` to get the source, then `cd c_sdk`.

## General Prerequisites
- **Tools**: CMake, make (or nmake on Windows), C/C++ compiler (gcc/g++, clang, or MSVC), git, pkg-config (or equivalent).
- **Dependencies**: Install platform-specific packages for Protobuf-C, libuuid, cJSON, gRPC, ncurses.
- **Protobuf Regeneration**: If modifying `.proto` files, install `protoc` and run `protoc --c_out=. proto/messages.proto`.
- **Custom CMake Modules**: If `find_package` fails, add `cmake/FindProtobuf-c.cmake` and `cmake/FindUUID.cmake` (examples in repo docs), and set `list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")` in `CMakeLists.txt`.

## 1. Windows (MSVC or MinGW)
Windows builds use MSVC (Visual Studio) or MinGW for cross-platform compatibility.

### Using MSVC (Recommended for Native Windows)
1. **Install Dependencies**:
   - Visual Studio (2022+): Install with C++ desktop development workload.
   - vcpkg (package manager): Install via `git clone https://github.com/microsoft/vcpkg` and `./vcpkg/bootstrap-vcpkg.bat`.
   - Run `./vcpkg install protobuf-c libuuid cjson grpc ncurses --triplet x64-windows`.
   - Install CMake from https://cmake.org/download/.

2. **Set Environment**:
   - Set `VCPKG_ROOT` to vcpkg directory.
   - Open Developer Command Prompt for VS 2022.

3. **Build**:
   ```cmd
   git clone https://github.com/ALH477/DeMoD-Communication-Framework
   cd DeMoD-Communication-Framework/c_sdk
   mkdir build && cd build
   cmake .. -DCMAKE_TOOLCHAIN_FILE=%VCPKG_ROOT%\scripts\buildsystems\vcpkg.cmake
   cmake --build .
   ```

4. **Test**:
   ```cmd
   .\dcf.exe version --json
   ```

### Using MinGW (For Cross-Compilation or Lightweight Builds)
1. **Install Dependencies**:
   - MinGW-w64 via MSYS2: Install from https://www.msys2.org/, run `pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake mingw-w64-x86_64-make mingw-w64-x86_64-protobuf-c mingw-w64-x86_64-libuuid mingw-w64-x86_64-grpc mingw-w64-x86_64-ncurses mingw-w64-x86_64-pkg-config`.
   - Build cJSON from source: `git clone https://github.com/DaveGamble/cJSON`, `cd cJSON`, `mkdir build && cd build`, `cmake ..`, `make && make install`.

2. **Build**:
   ```bash
   mkdir build && cd build
   cmake .. -G "MinGW Makefiles"
   mingw32-make
   ```

3. **Test**:
   ```bash
   ./dcf.exe version --json
   ```

## 2. BSD (e.g., FreeBSD)
FreeBSD builds use `pkg` for dependencies.

1. **Install Dependencies**:
   ```bash
   sudo pkg update
   sudo pkg install cmake gmake protobuf-c libuuid cjson grpc ncurses pkgconf
   ```

2. **Build cJSON** (if not available via pkg):
   ```bash
   git clone https://github.com/DaveGamble/cJSON
   cd cJSON
   mkdir build && cd build
   cmake ..
   gmake && sudo gmake install
   ```

3. **Build**:
   ```bash
   git clone https://github.com/ALH477/DeMoD-Communication-Framework
   cd DeMoD-Communication-Framework/c_sdk
   mkdir build && cd build
   cmake ..
   gmake
   ```

4. **Test**:
   ```bash
   ./dcf version --json
   ```

## 3. macOS
macOS builds use Homebrew for dependencies.

1. **Install Dependencies**:
   ```bash
   brew install cmake protobuf-c libuuid cjson grpc ncurses pkg-config
   ```

2. **Build**:
   ```bash
   git clone https://github.com/ALH477/DeMoD-Communication-Framework
   cd DeMoD-Communication-Framework/c_sdk
   mkdir build && cd build
   cmake ..
   make
   ```

3. **Test**:
   ```bash
   ./dcf version --json
   ```

4. **Note**: If `libuuid` is not found, use `brew install ossp-uuid` as a fallback.

## 4. WebAssembly (Wasm)
Wasm builds use Emscripten for browser/edge deployment (e.g., AUTO mode in web apps).

1. **Install Dependencies**:
   - Install Emscripten: Download from https://emscripten.org/docs/getting_started/downloads.html and run `./emsdk install latest`, `./emsdk activate latest`.
   - Install system dependencies: On Ubuntu, `sudo apt install cmake make g++ protobuf-c-dev uuid-dev libgrpc++-dev libncurses-dev pkg-config`. Build cJSON from source as in manual build.

2. **Build**:
   ```bash
   git clone https://github.com/ALH477/DeMoD-Communication-Framework
   cd DeMoD-Communication-Framework/c_sdk
   mkdir build-wasm && cd build-wasm
   emcmake cmake ..
   emmake make
   ```

3. **Test**:
   - The output is a `.wasm` file (e.g., `dcf.wasm`). Load in a browser with JavaScript bindings (e.g., using Emscripten’s JS output).
   - Note: gRPC in Wasm requires WebAssembly-specific builds (e.g., `grpc-web`); AUTO mode may need WebSocket fallback for P2P.

4. **Limitations**: RTT grouping works, but full gRPC streaming may require additional configuration (e.g., gRPC-Web for browsers).

## 5. Mobile ARM64 (Android/iOS)
ARM64 builds target Android (NDK) or iOS (Xcode).

### Android (ARM64)
1. **Install Dependencies**:
   - Android Studio with NDK (r26b+).
   - Install dependencies via `vcpkg` or build from source for ARM64 (protobuf-c, libuuid, cjson, grpc, ncurses).

2. **Build**:
   - Use CMake with Android toolchain:
     ```bash
     mkdir build-android && cd build-android
     cmake .. -DCMAKE_TOOLCHAIN_FILE=$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28
     make
     ```
   - Deploy as a native library in an Android app (e.g., JNI bindings for AUTO mode).

3. **Test**:
   - Run on an ARM64 emulator/device via Android Studio.

### iOS (ARM64)
1. **Install Dependencies**:
   - Xcode (15+), CocoaPods or vcpkg for dependencies (protobuf-c, libuuid, cjson, grpc, ncurses).

2. **Build**:
   - Use CMake with iOS toolchain:
     ```bash
     mkdir build-ios && cd build-ios
     cmake .. -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_SYSROOT=iphoneos
     make
     ```
   - Integrate into an Xcode project as a framework (e.g., Swift bindings for AUTO mode).

3. **Test**:
   - Run on an iOS simulator/device via Xcode.

## Notes
- **Export Compliance**: All builds avoid encryption; add TLS via gRPC plugins if needed.
- **Valgrind**: Use on Linux/ARM for memory checks; alternatives like AddressSanitizer on macOS/Windows.
- **Cross-Compilation**: For Wasm/ARM64, use toolchains like Emscripten/NDK to target from x86 hosts.
- **Troubleshooting**: If `find_package` fails, add custom `Find*.cmake` modules (see repo docs). For Wasm/mobile, some features (e.g., full gRPC) may require adaptations (e.g., gRPC-Web).
- **R&D**: Install additional tools (e.g., `valgrind` on Linux, `Instruments` on macOS) for profiling AUTO mode.

This guide enables compiling the DCF C SDK across platforms, supporting the monorepo's goal of multi-language SDKs. For issues, open a GitHub issue.