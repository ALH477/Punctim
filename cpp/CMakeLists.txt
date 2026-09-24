# SPDX-License-Identifier: LGPL-3.0-only
cmake_minimum_required(VERSION 3.13)
project(dcf_wire_cpp CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(DCF_CPP_GRPC "Build the dcfcpp gRPC node (needs gRPC + protobuf)" ON)
option(DCF_CPP_GNS  "Build the Steam-compatible transport on GameNetworkingSockets" OFF)
option(DCF_CPP_STEAM "Build against the proprietary Steamworks SDK (SDR relay + lobbies)" OFF)
# Developer-supplied Steamworks SDK root (the `sdk/` dir) — required for DCF_CPP_STEAM.
set(DCF_STEAMWORKS_SDK "" CACHE PATH "Path to the Steamworks SDK (sdk/) for DCF_CPP_STEAM")

if(DCF_CPP_GNS AND DCF_CPP_STEAM)
    message(FATAL_ERROR "DCF_CPP_GNS and DCF_CPP_STEAM are mutually exclusive backends")
endif()

# Header-only wire codec (frame.hpp + superpack.hpp + medium.hpp).
add_library(dcf_wire INTERFACE)
target_include_directories(dcf_wire INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

enable_testing()
add_executable(certify tests/certify.cpp)
target_link_libraries(certify PRIVATE dcf_wire)
add_test(NAME certify
         COMMAND certify ${CMAKE_CURRENT_SOURCE_DIR}/../Documentation/golden_vectors.json)

add_executable(certify_superpack tests/certify_superpack.cpp)
target_link_libraries(certify_superpack PRIVATE dcf_wire)
add_test(NAME certify_superpack COMMAND certify_superpack
         WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)

# DCF-Medium digital codecs (stream/hex/udp_proto/udp_bare/l2eth), header-only medium.hpp.
add_executable(certify_medium tests/certify_medium.cpp)
target_link_libraries(certify_medium PRIVATE dcf_wire)
add_test(NAME certify_medium COMMAND certify_medium
         WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)

# ── dcfcpp node: gRPC and/or Steam-compatible (GNS / Steamworks) transports ──
if(DCF_CPP_GRPC OR DCF_CPP_GNS OR DCF_CPP_STEAM)
    set(THREADS_PREFER_PTHREAD_FLAG ON)
    find_package(Threads REQUIRED)

    add_executable(dcfcpp src/dcf_node.cpp)
    target_include_directories(dcfcpp PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(dcfcpp PRIVATE dcf_wire Threads::Threads)

    # gRPC transport (legacy): unary SendFrame + bidi MeshStream.
    if(DCF_CPP_GRPC)
        find_package(Protobuf CONFIG REQUIRED)
        find_package(gRPC CONFIG REQUIRED)
        add_library(dcf_proto STATIC proto/dcf.proto)
        target_link_libraries(dcf_proto PUBLIC protobuf::libprotobuf gRPC::grpc++)
        target_include_directories(dcf_proto PUBLIC "${CMAKE_CURRENT_BINARY_DIR}")
        protobuf_generate(TARGET dcf_proto LANGUAGE cpp
            IMPORT_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/proto
            PROTOC_OUT_DIR ${CMAKE_CURRENT_BINARY_DIR})
        protobuf_generate(TARGET dcf_proto LANGUAGE grpc
            GENERATE_EXTENSIONS .grpc.pb.h .grpc.pb.cc
            PLUGIN "protoc-gen-grpc=$<TARGET_FILE:gRPC::grpc_cpp_plugin>"
            IMPORT_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/proto
            PROTOC_OUT_DIR ${CMAKE_CURRENT_BINARY_DIR})
        target_compile_definitions(dcfcpp PRIVATE DCF_CPP_GRPC)
        target_link_libraries(dcfcpp PRIVATE
            dcf_proto protobuf::libprotobuf gRPC::grpc++ gRPC::grpc++_reflection)
    endif()

    # Steam-compatible transport on the open GameNetworkingSockets (BSD-3).
    if(DCF_CPP_GNS)
        target_sources(dcfcpp PRIVATE src/net_steam.cpp)
        target_compile_definitions(dcfcpp PRIVATE DCF_CPP_GNS)
        # The nixpkgs GameNetworkingSockets CMake config has a doubled include path
        # and a dangling protobuf target in its link interface, so we link the lib
        # directly (its .so resolves protobuf/openssl via rpath). The flake passes
        # DCF_GNS_INCLUDE_DIR / DCF_GNS_LIB_DIR; otherwise fall back to find_package.
        if(DCF_GNS_INCLUDE_DIR)
            target_include_directories(dcfcpp PRIVATE ${DCF_GNS_INCLUDE_DIR})
        endif()
        find_library(DCF_GNS_LIB NAMES GameNetworkingSockets HINTS ${DCF_GNS_LIB_DIR})
        if(DCF_GNS_LIB)
            target_link_libraries(dcfcpp PRIVATE ${DCF_GNS_LIB})
        else()
            find_package(GameNetworkingSockets CONFIG REQUIRED)
            target_link_libraries(dcfcpp PRIVATE GameNetworkingSockets::GameNetworkingSockets)
        endif()
    endif()

    # Proprietary Steamworks backend (SDR relay + lobbies + server browser).
    # Developer-supplied SDK; not redistributable / not in CI. See DCF_STEAM_SPEC.md.
    if(DCF_CPP_STEAM)
        if(NOT DCF_STEAMWORKS_SDK)
            message(FATAL_ERROR "DCF_CPP_STEAM requires -DDCF_STEAMWORKS_SDK=/path/to/sdk")
        endif()
        message(WARNING
            "Building against the proprietary Steamworks SDK at ${DCF_STEAMWORKS_SDK}.\n"
            "  Steamworks SDK Access Agreement: the SDK is NOT redistributable. Do not\n"
            "  commit/vendor it. Only redistributable_bin/ (libsteam_api.so, steamclient.so)\n"
            "  may ship, in object form, with the built node. Do not ship steam_appid.txt in\n"
            "  production. See Documentation/DCF_STEAM_SPEC.md (Licensing).")
        target_sources(dcfcpp PRIVATE src/net_steam.cpp src/steam_gameserver.cpp src/steam_lobby.cpp)
        target_compile_definitions(dcfcpp PRIVATE DCF_CPP_STEAM)
        target_include_directories(dcfcpp PRIVATE ${DCF_STEAMWORKS_SDK}/public)
        find_library(STEAM_API_LIB NAMES steam_api libsteam_api
            PATHS ${DCF_STEAMWORKS_SDK}/redistributable_bin/linux64 REQUIRED)
        target_link_libraries(dcfcpp PRIVATE ${STEAM_API_LIB})
    endif()

    include(GNUInstallDirs)
    install(TARGETS dcfcpp RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

    # Hub <-> 2-client forwarding test (only the open GNS build runs in CI).
    if(DCF_CPP_GNS)
        add_test(NAME gns_loopback
            COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/gns_loopback.sh $<TARGET_FILE:dcfcpp>)
    endif()
endif()
