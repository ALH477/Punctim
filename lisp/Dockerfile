# ============================================================================
# Punctim Lisp - Hardened Docker Image
# Multi-stage build with SBCL and security best practices
# ============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Build Environment with Quicklisp
# -----------------------------------------------------------------------------
FROM fukamachi/sbcl:2.4.11-debian AS builder

# Install build dependencies
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    zlib1g-dev \
    uuid-dev \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy source files
COPY src/ /build/src/
COPY punctim.asd /build/
COPY streamdb/ /build/streamdb/

# Build the StreamDB foreign library that punctim.lisp loads at startup
# (cffi:use-foreign-library libstreamdb). Compiled from source so it is portable
# into the runtime image; installed on the default loader path.
RUN gcc -shared -fPIC -O2 \
      /build/streamdb/streamdb.c /build/streamdb/libstreamdb_wrapper.c \
      -o /usr/local/lib/libstreamdb.so -luuid \
    && ldconfig

# The fukamachi/sbcl image uses Roswell - Quicklisp is already available via ros
# Install dependencies using ros/quicklisp
RUN ros -e '(ql:quickload (quote (:cffi :uuid :usocket :bordeaux-threads :log4cl :trivial-backtrace :flexi-streams :fiveam :ieee-floats :cl-json)) :silent t)' -e '(quit)'

# Build the executable core image using ros
RUN ros -e '(ql:quickload (quote (:cffi :uuid :usocket :bordeaux-threads :log4cl :trivial-backtrace :flexi-streams :fiveam :ieee-floats :cl-json)) :silent t)' \
    -l /build/src/punctim.lisp \
    -e '(in-package :d-lisp)' \
    -e '(sb-ext:save-lisp-and-die "/build/punctim" :executable t :toplevel (lambda () (apply (function main) (rest sb-ext:*posix-argv*))) :compression 9)'

# -----------------------------------------------------------------------------
# Stage 2: Hardened Runtime
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime

# Security hardening
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libssl3 \
    libffi8 \
    zlib1g \
    libuuid1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/share/man /tmp/* /var/tmp/* \
    && chmod 700 /root \
    && find / -type f -perm /4000 -exec chmod u-s {} \; 2>/dev/null || true \
    && find / -type f -perm /2000 -exec chmod g-s {} \; 2>/dev/null || true

# Create non-root user
RUN groupadd -g 10001 punctim && \
    useradd -u 10001 -g punctim -s /usr/sbin/nologin -M -d /var/lib/punctim punctim

# Create directories
RUN mkdir -p /etc/punctim /var/lib/punctim /var/log/punctim && \
    chown -R punctim:punctim /var/lib/punctim /var/log/punctim

# Copy executable from builder
COPY --from=builder --chown=punctim:punctim /build/punctim /usr/local/bin/punctim

# Copy the StreamDB foreign library (re-dlopen'd by the saved core on startup)
COPY --from=builder /usr/local/lib/libstreamdb.so /usr/local/lib/
RUN ldconfig

# Copy config schema (the runtime config.json is created by `punctim init` or mounted)
COPY --chown=punctim:punctim config.schema.json /etc/punctim/

# Set secure permissions
RUN chmod 755 /usr/local/bin/punctim && \
    chmod 750 /etc/punctim /var/lib/punctim /var/log/punctim

# Use non-root user
USER punctim:punctim

# Working directory
WORKDIR /var/lib/punctim

# Environment
ENV PUNCTIM_CONFIG=/etc/punctim/config.json
ENV HOME=/var/lib/punctim

# Expose ports
EXPOSE 7777/udp 50051/tcp

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD /usr/local/bin/punctim status || exit 1

ENTRYPOINT ["/usr/local/bin/punctim"]
CMD ["help"]

# -----------------------------------------------------------------------------
# Stage 3: Development Runtime (with SBCL REPL)
# -----------------------------------------------------------------------------
FROM fukamachi/sbcl:2.4.11-debian AS runtime-dev

USER root

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libssl3 \
    libffi8 \
    zlib1g \
    libuuid1 \
    rlwrap \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 10001 punctim && \
    useradd -u 10001 -g punctim -m -s /bin/bash -d /home/punctim punctim

# Copy source
COPY --chown=punctim:punctim src/ /home/punctim/src/

# StreamDB foreign library for the REPL (loaded by punctim.lisp on load)
COPY --from=builder /usr/local/lib/libstreamdb.so /usr/local/lib/
RUN ldconfig

# Create directories
RUN mkdir -p /etc/punctim /var/lib/punctim /var/log/punctim && \
    chown -R punctim:punctim /var/lib/punctim /var/log/punctim /home/punctim

# Copy config schema (runtime config.json is created via init or mounted)
COPY --chown=punctim:punctim config.schema.json /etc/punctim/

# Pre-install dependencies as root (Roswell cache is system-wide)
RUN ros -e '(ql:quickload (quote (:cffi :uuid :usocket :bordeaux-threads :log4cl :trivial-backtrace :flexi-streams :fiveam :ieee-floats :cl-json)) :silent t)' -e '(quit)'

# Create init script for REPL
RUN printf '#!/bin/bash\nrlwrap ros -e "(ql:quickload (quote (:cffi :uuid :usocket :bordeaux-threads :log4cl :trivial-backtrace :flexi-streams :fiveam :ieee-floats :cl-json)) :silent t)" -l /home/punctim/src/punctim.lisp -e "(in-package :d-lisp)" "$@"\n' > /usr/local/bin/punctim-repl && \
    chmod +x /usr/local/bin/punctim-repl

# Use non-root user
USER punctim:punctim
WORKDIR /home/punctim

# Environment
ENV HOME=/home/punctim
ENV PUNCTIM_CONFIG=/etc/punctim/config.json

# Expose ports
EXPOSE 7777/udp 50051/tcp

# Default to REPL
CMD ["/usr/local/bin/punctim-repl"]

# -----------------------------------------------------------------------------
# Default target
# -----------------------------------------------------------------------------
FROM runtime AS default
