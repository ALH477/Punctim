cmake_minimum_required(VERSION 3.14)

project(DCF 
    VERSION 5.2.0
    DESCRIPTION "DeMoD Communications Framework"
    LANGUAGES C
)

# ============================================================================
# Options
# ============================================================================

option(DCF_BUILD_SHARED "Build shared library" ON)
option(DCF_BUILD_STATIC "Build static library" ON)
option(DCF_BUILD_TESTS "Build unit tests" ON)
option(DCF_BUILD_BENCHMARKS "Build benchmarks" OFF)
option(DCF_BUILD_EXAMPLES "Build examples" ON)
option(DCF_BUILD_NODE "Build the dcfnode UDP mesh node (ProtoMessage transport)" ON)
option(DCF_BUILD_PLUGINS "Compile the v1 transport plugins (bit-rot guard; nothing loads them yet)" OFF)
option(DCF_MODEM_AUDIO "Link a live-audio backend into the Faust modem transport" OFF)
option(DCF_ENABLE_SANITIZERS "Deprecated alias for -DDCF_SAN=address" OFF)
set(DCF_SAN "OFF" CACHE STRING
    "Sanitizer configuration: OFF, address (ASan+UBSan), thread (TSan), undefined (UBSan only)")
set_property(CACHE DCF_SAN PROPERTY STRINGS OFF address thread undefined)
option(DCF_ENABLE_ARCH_NATIVE "Enable -march=native for latency benchmark" OFF)
option(DCF_ENABLE_COVERAGE "Enable code coverage" OFF)
option(DCF_ENABLE_LTO "Enable link-time optimization" OFF)

# ============================================================================
# Compiler Configuration
# ============================================================================

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type" FORCE)
endif()

# Compiler warnings
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    set(DCF_WARNINGS
        -Wall
        -Wextra
        -Wpedantic
        -Werror=return-type
        -Werror=implicit-function-declaration
        -Wformat=2
        -Wformat-security
        -Wnull-dereference
        -Wstack-protector
        -Wstrict-overflow=2
        -Warray-bounds=2
        -Wshift-overflow=2
        -Wcast-qual
        -Wconversion
        -Wsign-conversion
        -Wdouble-promotion
        -Wfloat-equal
        -Wundef
        -Wshadow
        -Wpointer-arith
        -Wunused-macros
        -Wmissing-declarations
        -Wmissing-prototypes
        -Wstrict-prototypes
        -Wredundant-decls
        -Wswitch-default
        -Wswitch-enum
    )
    
    if(CMAKE_C_COMPILER_ID STREQUAL "GNU")
        list(APPEND DCF_WARNINGS
            -Wlogical-op
            -Wduplicated-cond
            -Wduplicated-branches
            -Wrestrict
            -Wnull-dereference
        )
    endif()
    
    if(CMAKE_C_COMPILER_ID STREQUAL "Clang")
        list(APPEND DCF_WARNINGS
            -Wall -Wextra -Wconversion
            -Wno-padded
            -Wno-covered-switch-default
            -Wno-declaration-after-statement
            -Wno-disabled-macro-expansion
            -Wno-used-but-marked-unused
        )
    endif()
elseif(MSVC)
    set(DCF_WARNINGS /W4 /WX /permissive-)
endif()

# Hardening flags
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    # Base hardening (for all targets)
    set(DCF_HARDENING_BASE
        -fstack-protector-strong
        -D_FORTIFY_SOURCE=2
    )
    if(NOT CMAKE_SYSTEM_NAME STREQUAL "Darwin")
        list(APPEND DCF_HARDENING_BASE -fstack-clash-protection)
    endif()
    
    # For executables and static libs: add -fPIE
    set(DCF_HARDENING ${DCF_HARDENING_BASE} -fPIE)
    
    # For shared libraries: use -fPIC (not -fPIE) and global-dynamic TLS
    set(DCF_HARDENING_SHARED ${DCF_HARDENING_BASE} -fPIC -ftls-model=global-dynamic)
endif()

# Sanitizers. ASan and TSan are mutually exclusive, so this is a single-choice
# knob rather than a boolean: address = ASan+UBSan, thread = TSan, undefined =
# UBSan alone. DCF_ENABLE_SANITIZERS is kept as a back-compat alias.
if(DCF_ENABLE_SANITIZERS AND DCF_SAN STREQUAL "OFF")
    set(DCF_SAN "address")
endif()
if(NOT DCF_SAN STREQUAL "OFF")
    if(DCF_SAN STREQUAL "address")
        set(DCF_SANITIZER_FLAGS -fsanitize=address,undefined -fno-omit-frame-pointer)
    elseif(DCF_SAN STREQUAL "thread")
        set(DCF_SANITIZER_FLAGS -fsanitize=thread -fno-omit-frame-pointer)
    elseif(DCF_SAN STREQUAL "undefined")
        set(DCF_SANITIZER_FLAGS -fsanitize=undefined -fno-omit-frame-pointer)
    else()
        message(FATAL_ERROR "DCF_SAN must be OFF, address, thread, or undefined (got '${DCF_SAN}')")
    endif()
endif()

# Coverage
if(DCF_ENABLE_COVERAGE)
    set(DCF_COVERAGE_FLAGS --coverage -fprofile-arcs -ftest-coverage)
endif()

# LTO - Note: We only apply LTO to static library, not shared.
# Shared libraries with TLS cannot use LTO (breaks TLS model).
if(DCF_ENABLE_LTO)
    include(CheckIPOSupported)
    check_ipo_supported(RESULT ipo_supported OUTPUT ipo_error)
    if(ipo_supported)
        set(DCF_LTO_SUPPORTED TRUE)
        message(STATUS "LTO supported - will be enabled for static library only")
    else()
        set(DCF_LTO_SUPPORTED FALSE)
        message(STATUS "LTO not supported: ${ipo_error}")
    endif()
else()
    set(DCF_LTO_SUPPORTED FALSE)
endif()

# ============================================================================
# Dependencies
# ============================================================================

find_package(Threads REQUIRED)

# Check for optional dependencies
include(CheckSymbolExists)
include(CheckIncludeFile)

check_include_file("execinfo.h" HAVE_EXECINFO_H)
check_include_file("sys/eventfd.h" HAVE_EVENTFD_H)
check_symbol_exists(getrandom "sys/random.h" HAVE_GETRANDOM)
check_symbol_exists(arc4random "stdlib.h" HAVE_ARC4RANDOM)

# ============================================================================
# Library Sources
# ============================================================================

set(DCF_HEADERS
    include/dcf_platform.h
    include/dcf_types.h
    include/dcf_error.h
    include/dcf_ringbuf.h
    include/dcf_connpool.h
    include/dcf_transport_v1.h
)

set(DCF_SOURCES
    src/dcf_platform.c
    src/dcf_error.c
    src/dcf_ringbuf.c
    src/dcf_connpool.c
)

# ============================================================================
# Library Targets
# ============================================================================

# Common compile definitions
set(DCF_COMPILE_DEFS
    $<$<BOOL:${HAVE_EXECINFO_H}>:HAVE_EXECINFO_H>
    $<$<BOOL:${HAVE_EVENTFD_H}>:HAVE_EVENTFD_H>
    $<$<BOOL:${HAVE_GETRANDOM}>:HAVE_GETRANDOM>
    $<$<BOOL:${HAVE_ARC4RANDOM}>:HAVE_ARC4RANDOM>
)

# Static library
if(DCF_BUILD_STATIC)
    add_library(dcf_static STATIC ${DCF_SOURCES})
    target_include_directories(dcf_static 
        PUBLIC 
            $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:include>
    )
    target_compile_options(dcf_static PRIVATE 
        ${DCF_WARNINGS} 
        ${DCF_HARDENING}
        ${DCF_SANITIZER_FLAGS}
        ${DCF_COVERAGE_FLAGS}
    )
    target_compile_definitions(dcf_static PRIVATE ${DCF_COMPILE_DEFS})
    target_link_libraries(dcf_static PUBLIC Threads::Threads)
    
    if(UNIX AND NOT APPLE)
        target_link_libraries(dcf_static PUBLIC dl)
    endif()
    
    set_target_properties(dcf_static PROPERTIES
        OUTPUT_NAME dcf
        POSITION_INDEPENDENT_CODE ON
        C_VISIBILITY_PRESET hidden
    )
    
    # Apply LTO only to static library (safe for static libs)
    if(DCF_LTO_SUPPORTED)
        set_target_properties(dcf_static PROPERTIES
            INTERPROCEDURAL_OPTIMIZATION ON
        )
    endif()
    
    if(DCF_SANITIZER_FLAGS)
        target_link_options(dcf_static PUBLIC ${DCF_SANITIZER_FLAGS})
    endif()
    if(DCF_ENABLE_COVERAGE)
        target_link_options(dcf_static PUBLIC ${DCF_COVERAGE_FLAGS})
    endif()
endif()

# Shared library
if(DCF_BUILD_SHARED)
    add_library(dcf_shared SHARED ${DCF_SOURCES})
    target_include_directories(dcf_shared 
        PUBLIC 
            $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:include>
    )
    
    # For shared libraries:
    # - Use global-dynamic TLS model (avoid R_X86_64_TPOFF32 relocation errors)
    # - Use -fPIC not -fPIE
    # - Disable LTO (LTO recompiles and loses TLS model setting)
    # DCF_HARDENING_SHARED already includes: -fPIC -ftls-model=global-dynamic
    set(DCF_SHARED_COMPILE_FLAGS ${DCF_WARNINGS} ${DCF_HARDENING_SHARED})
    if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
        # Belt and suspenders: explicitly disable LTO
        list(APPEND DCF_SHARED_COMPILE_FLAGS -fno-lto)
    endif()
    
    target_compile_options(dcf_shared PRIVATE 
        ${DCF_SHARED_COMPILE_FLAGS}
        ${DCF_SANITIZER_FLAGS}
        ${DCF_COVERAGE_FLAGS}
    )
    target_compile_definitions(dcf_shared 
        PRIVATE DCF_BUILD_SHARED ${DCF_COMPILE_DEFS}
    )
    target_link_libraries(dcf_shared PUBLIC Threads::Threads)
    
    if(UNIX AND NOT APPLE)
        target_link_libraries(dcf_shared PUBLIC dl)
    endif()
    
    # Explicitly disable LTO for shared library - LTO breaks TLS model
    set_target_properties(dcf_shared PROPERTIES
        OUTPUT_NAME dcf
        VERSION ${PROJECT_VERSION}
        SOVERSION ${PROJECT_VERSION_MAJOR}
        POSITION_INDEPENDENT_CODE ON
        C_VISIBILITY_PRESET hidden
        VISIBILITY_INLINES_HIDDEN ON
        INTERPROCEDURAL_OPTIMIZATION OFF
    )
    
    # Also disable LTO at link time and ensure PIC
    if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
        target_link_options(dcf_shared PRIVATE -fno-lto -fPIC)
    endif()
    
    if(DCF_SANITIZER_FLAGS)
        target_link_options(dcf_shared PUBLIC ${DCF_SANITIZER_FLAGS})
    endif()
    if(DCF_ENABLE_COVERAGE)
        target_link_options(dcf_shared PUBLIC ${DCF_COVERAGE_FLAGS})
    endif()
endif()

# Alias for convenience
if(DCF_BUILD_STATIC)
    add_library(dcf::static ALIAS dcf_static)
endif()
if(DCF_BUILD_SHARED)
    add_library(dcf::shared ALIAS dcf_shared)
endif()

# Default library target
if(DCF_BUILD_STATIC)
    add_library(dcf::dcf ALIAS dcf_static)
elseif(DCF_BUILD_SHARED)
    add_library(dcf::dcf ALIAS dcf_shared)
endif()

# ============================================================================
# Tests
# ============================================================================

if(DCF_BUILD_TESTS)
    enable_testing()
    
    add_executable(dcf_tests tests/unit/test_suite.c)
    target_link_libraries(dcf_tests PRIVATE dcf::dcf)
    target_compile_options(dcf_tests PRIVATE ${DCF_WARNINGS})
    
    if(DCF_SANITIZER_FLAGS)
        target_compile_options(dcf_tests PRIVATE ${DCF_SANITIZER_FLAGS})
        target_link_options(dcf_tests PRIVATE ${DCF_SANITIZER_FLAGS})
    endif()
    
    if(DCF_ENABLE_COVERAGE)
        target_compile_options(dcf_tests PRIVATE ${DCF_COVERAGE_FLAGS})
        target_link_options(dcf_tests PRIVATE ${DCF_COVERAGE_FLAGS})
    endif()
    
    add_test(NAME unit_tests COMMAND dcf_tests)
    set_tests_properties(unit_tests PROPERTIES
        TIMEOUT 60
        ENVIRONMENT "ASAN_OPTIONS=detect_leaks=1"
    )

    # Regression suite: pins the fixed crashers/races (C4, H2, C6-class).
    # Run it in every DCF_SAN config — the sanitizers are half the assertion.
    add_executable(test_regressions tests/unit/test_regressions.c)
    target_include_directories(test_regressions PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(test_regressions PRIVATE dcf::dcf)
    if(DCF_SANITIZER_FLAGS)
        target_compile_options(test_regressions PRIVATE ${DCF_SANITIZER_FLAGS})
        target_link_options(test_regressions PRIVATE ${DCF_SANITIZER_FLAGS})
    endif()
    add_test(NAME regressions COMMAND test_regressions)
    set_tests_properties(regressions PROPERTIES TIMEOUT 120)

    # StreamDB C5 regression — the first C-level test of lisp/streamdb.
    if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/../lisp/streamdb/streamdb.c)
        add_executable(test_streamdb_regress
            tests/test_streamdb_regress.c
            ${CMAKE_CURRENT_SOURCE_DIR}/../lisp/streamdb/streamdb.c)
        target_include_directories(test_streamdb_regress PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/../lisp/streamdb)
        # streamdb.c uses strdup/usleep/PTHREAD_MUTEX_RECURSIVE; expose the
        # POSIX surface explicitly since CMAKE_C_EXTENSIONS is OFF here.
        target_compile_definitions(test_streamdb_regress PRIVATE
            _DEFAULT_SOURCE _XOPEN_SOURCE=700)
        target_link_libraries(test_streamdb_regress PRIVATE Threads::Threads)
        if(DCF_SANITIZER_FLAGS)
            target_compile_options(test_streamdb_regress PRIVATE ${DCF_SANITIZER_FLAGS})
            target_link_options(test_streamdb_regress PRIVATE ${DCF_SANITIZER_FLAGS})
        endif()
        add_test(NAME streamdb_regress COMMAND test_streamdb_regress)
    endif()

    # Adapter certification binaries. These were previously compiled only by
    # raw gcc lines in wire-certify.yml; registering them here means one
    # `ctest` run covers the full C certification surface.
    foreach(cert wire superpack fec audio game text sstv snake monitor pipe medium)
        add_executable(test_${cert}_certify tests/test_${cert}_certify.c)
        target_include_directories(test_${cert}_certify PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
        target_link_libraries(test_${cert}_certify PRIVATE m)
        add_test(NAME ${cert}_certify COMMAND test_${cert}_certify
                 WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)
    endforeach()

    add_executable(test_snake_dsp tests/test_snake_dsp.c)
    target_include_directories(test_snake_dsp PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
    target_link_libraries(test_snake_dsp PRIVATE m)
    add_test(NAME snake_dsp COMMAND test_snake_dsp)

    add_executable(test_snake_l2 tests/test_snake_l2.c)
    target_include_directories(test_snake_l2 PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
    target_link_libraries(test_snake_l2 PRIVATE m)
    add_test(NAME snake_l2 COMMAND test_snake_l2)
    
    # Latency test for audio benchmarking (only if the source is present)
    if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/tests/latency/latency_test.c)
        add_executable(latency_test tests/latency/latency_test.c)
        target_link_libraries(latency_test PRIVATE dcf::dcf m)
        target_compile_options(latency_test PRIVATE
            ${DCF_WARNINGS}
            -O3 $<$<BOOL:${DCF_ENABLE_ARCH_NATIVE}>:-march=native>
        )
        if(DCF_SANITIZER_FLAGS)
            target_compile_options(latency_test PRIVATE ${DCF_SANITIZER_FLAGS})
            target_link_options(latency_test PRIVATE ${DCF_SANITIZER_FLAGS})
        endif()
    endif()
endif()

# ============================================================================
# dcfnode — the C mesh node (independent of the 4-module spine; header-only
# codec from ../codec + ProtoMessage/UDP). Built by default; toggle DCF_BUILD_NODE.
# ============================================================================

if(DCF_BUILD_NODE)
    add_executable(dcfnode node/dcfnode.c)
    target_include_directories(dcfnode PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/node
        ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
    target_compile_options(dcfnode PRIVATE ${DCF_WARNINGS})
    target_link_libraries(dcfnode PRIVATE m)

    # punctim — the uniform DCF medium CLI (Documentation/DCF_MEDIUM_SPEC.md): any medium in
    # -> any medium out, byte-deterministic across languages. Header-only codec
    # (../codec/demod_medium.h); `punctim certify` compiles in tests/test_medium_certify.c.
    add_executable(punctim node/punctim.c)
    target_include_directories(punctim PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/node
        ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
    target_compile_options(punctim PRIVATE ${DCF_WARNINGS})
    target_link_libraries(punctim PRIVATE m)

    if(DCF_BUILD_TESTS)
        add_executable(test_proto_certify tests/test_proto_certify.c)
        target_include_directories(test_proto_certify PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/node)
        add_test(NAME proto_certify COMMAND test_proto_certify)

        add_executable(test_modulation_certify tests/test_modulation_certify.c)
        target_include_directories(test_modulation_certify PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
        add_test(NAME modulation_certify COMMAND test_modulation_certify
                 WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)

        add_executable(test_modem_loopback tests/test_modem_loopback.c)
        target_include_directories(test_modem_loopback PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/node ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
        target_link_libraries(test_modem_loopback PRIVATE m)
        add_test(NAME modem_loopback COMMAND test_modem_loopback)

        add_executable(test_modem_fec tests/test_modem_fec.c)
        target_include_directories(test_modem_fec PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/node ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
        target_link_libraries(test_modem_fec PRIVATE m)
        add_test(NAME modem_fec COMMAND test_modem_fec)

        add_executable(test_mesh_certify tests/test_mesh_certify.c)
        target_include_directories(test_mesh_certify PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../codec)
        add_test(NAME mesh_certify COMMAND test_mesh_certify)
    endif()
endif()

# ============================================================================
# v1 transport plugins — compile-only bit-rot guard
#
# No shipped loader dlopens these yet (the plugin manager is a design
# reference in include/experimental/), but compiling them keeps the code
# honest: before this target existed, none of them could even build, and
# several shipped real bugs (see DCF_CODE_REVIEW.md H1/M3/C6).
# plugins/experimental/ stays excluded — those are quarantined by design.
# ============================================================================

if(DCF_BUILD_PLUGINS)
    set(DCF_PLUGIN_SOURCES
        plugins/DOOM_udp_transport.c
        plugins/dcf_udp_transport.c
        plugins/serial_transport.c
        plugins/namedpipe_transport.c
        plugins/unixsocket_transport.c
    )
    # Plugins with optional system dependencies
    check_include_file("netinet/sctp.h" HAVE_SCTP_H)
    check_include_file("linux/can.h" HAVE_LINUX_CAN_H)
    check_include_file("modbus/modbus.h" HAVE_MODBUS_H)
    if(HAVE_SCTP_H)
        list(APPEND DCF_PLUGIN_SOURCES plugins/sctp_transport.c)
    endif()
    if(HAVE_LINUX_CAN_H)
        list(APPEND DCF_PLUGIN_SOURCES plugins/can_transport.c)
    endif()
    if(HAVE_MODBUS_H)
        list(APPEND DCF_PLUGIN_SOURCES plugins/modbus_transport.c)
    endif()

    foreach(plugin_src ${DCF_PLUGIN_SOURCES})
        get_filename_component(plugin_name ${plugin_src} NAME_WE)
        add_library(plugin_${plugin_name} MODULE ${plugin_src})
        target_include_directories(plugin_${plugin_name} PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/include)
        target_compile_options(plugin_${plugin_name} PRIVATE ${DCF_WARNINGS})
        set_target_properties(plugin_${plugin_name} PROPERTIES
            OUTPUT_NAME "dcf_${plugin_name}")
        if(plugin_name STREQUAL "modbus_transport")
            target_link_libraries(plugin_${plugin_name} PRIVATE modbus)
        endif()
    endforeach()
endif()

# ============================================================================
# Examples
# ============================================================================

if(DCF_BUILD_EXAMPLES AND EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/examples/basic_example.c)
    add_executable(example_basic examples/basic_example.c)
    target_link_libraries(example_basic PRIVATE dcf::dcf)
endif()

# ============================================================================
# Installation
# ============================================================================

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

# Install headers
install(FILES ${DCF_HEADERS}
    DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/dcf
)

# Install libraries
if(DCF_BUILD_STATIC)
    install(TARGETS dcf_static
        EXPORT DCFTargets
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
    )
endif()

if(DCF_BUILD_SHARED)
    install(TARGETS dcf_shared
        EXPORT DCFTargets
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
    )
endif()

# Install CMake config
install(EXPORT DCFTargets
    FILE DCFTargets.cmake
    NAMESPACE dcf::
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/DCF
)

configure_package_config_file(
    cmake/DCFConfig.cmake.in
    ${CMAKE_CURRENT_BINARY_DIR}/DCFConfig.cmake
    INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/DCF
)

write_basic_package_version_file(
    ${CMAKE_CURRENT_BINARY_DIR}/DCFConfigVersion.cmake
    VERSION ${PROJECT_VERSION}
    COMPATIBILITY SameMajorVersion
)

install(FILES
    ${CMAKE_CURRENT_BINARY_DIR}/DCFConfig.cmake
    ${CMAKE_CURRENT_BINARY_DIR}/DCFConfigVersion.cmake
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/DCF
)

# Install pkg-config file
configure_file(cmake/dcf.pc.in ${CMAKE_CURRENT_BINARY_DIR}/dcf.pc @ONLY)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/dcf.pc
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/pkgconfig
)

# ============================================================================
# Package
# ============================================================================

set(CPACK_PACKAGE_NAME "dcf")
set(CPACK_PACKAGE_VERSION ${PROJECT_VERSION})
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY ${PROJECT_DESCRIPTION})
set(CPACK_PACKAGE_VENDOR "DeMoD LLC")
set(CPACK_PACKAGE_CONTACT "dcf@demod.io")

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/../LICENSE")
    set(CPACK_RESOURCE_FILE_LICENSE "${CMAKE_CURRENT_SOURCE_DIR}/../LICENSE")
elseif(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE")
    set(CPACK_RESOURCE_FILE_LICENSE "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE")
else()
    message(WARNING "No LICENSE file found for CPack")
endif()

if(UNIX AND NOT APPLE)
    set(CPACK_GENERATOR "TGZ;DEB;RPM")
    set(CPACK_DEBIAN_PACKAGE_DEPENDS "libc6")
    set(CPACK_DEBIAN_PACKAGE_RECOMMENDS "libgrpc++1, libprotobuf23")
    set(CPACK_RPM_PACKAGE_REQUIRES "glibc")
elseif(APPLE)
    set(CPACK_GENERATOR "TGZ")
elseif(WIN32)
    set(CPACK_GENERATOR "ZIP;NSIS")
endif()

include(CPack)

# ============================================================================
# Summary
# ============================================================================

message(STATUS "")
message(STATUS "DCF Configuration Summary")
message(STATUS "-------------------------")
message(STATUS "  Version:          ${PROJECT_VERSION}")
message(STATUS "  Build type:       ${CMAKE_BUILD_TYPE}")
message(STATUS "  C Compiler:       ${CMAKE_C_COMPILER_ID} ${CMAKE_C_COMPILER_VERSION}")
message(STATUS "  Install prefix:   ${CMAKE_INSTALL_PREFIX}")
message(STATUS "")
message(STATUS "  Build shared:     ${DCF_BUILD_SHARED}")
message(STATUS "  Build static:     ${DCF_BUILD_STATIC}")
message(STATUS "  Build tests:      ${DCF_BUILD_TESTS}")
message(STATUS "  Build examples:   ${DCF_BUILD_EXAMPLES}")
message(STATUS "  Sanitizers:       ${DCF_ENABLE_SANITIZERS}")
message(STATUS "  Coverage:         ${DCF_ENABLE_COVERAGE}")
message(STATUS "  LTO (static):     ${DCF_LTO_SUPPORTED}")
if(DCF_BUILD_SHARED)
    message(STATUS "  Note: Shared library built without LTO (TLS compatibility)")
endif()
message(STATUS "")
