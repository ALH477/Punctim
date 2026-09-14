# syntax=docker/dockerfile:1
# ============================================================================
# DCF Rust SDK - Hardened Production Dockerfile
# ============================================================================
# The SDK crate path-depends on the wire+audio codec crate (../codec), so this
# image MUST be built from the repository ROOT, not from rust/:
#
#   docker build -f rust/Dockerfile -t alh477/dcf-rs:latest .
# ----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM rust:1.83-alpine3.21 AS builder

# Build dependencies. protobuf-dev provides protoc for tonic-build (gRPC).
RUN apk add --no-cache \
    musl-dev \
    openssl-dev \
    openssl-libs-static \
    pkgconfig \
    protobuf-dev \
    git \
    && rm -rf /var/cache/apk/*

WORKDIR /build

# Both crates: the SDK and the codec it path-depends on (rust/Cargo.toml ->
# dcf-wire-codec = { path = "../codec" }). Layout under /build mirrors the repo
# so the relative path resolves.
COPY codec/ ./codec/
COPY rust/ ./rust/

WORKDIR /build/rust

# Build the release binary against the committed lockfile, then strip it.
RUN cargo build --release --locked \
    && strip target/release/dcf

# -----------------------------------------------------------------------------
# Stage 2: Hardened Runtime (Alpine)
# Balances security with debuggability (has a shell for troubleshooting)
# -----------------------------------------------------------------------------
FROM alpine:3.21 AS runtime

# Security hardening & runtime dependencies
RUN apk add --no-cache \
    ca-certificates \
    libgcc \
    && rm -rf /var/cache/apk/* \
    && rm -rf /tmp/* /var/tmp/*

# Dedicated non-root user for the application
RUN addgroup -g 10001 dcf && \
    adduser -u 10001 -G dcf -s /sbin/nologin -D -H dcf

# Standard directories with correct permissions
RUN mkdir -p /etc/dcf /var/lib/dcf /var/log/dcf && \
    chown -R dcf:dcf /var/lib/dcf /var/log/dcf

# Compiled binary + default configuration from the builder stage
COPY --from=builder --chown=dcf:dcf /build/rust/target/release/dcf /usr/local/bin/dcf
COPY --from=builder /build/rust/dcf_config.toml* /etc/dcf/

# Strict permissions
RUN chmod 755 /usr/local/bin/dcf && \
    chmod 750 /etc/dcf /var/lib/dcf /var/log/dcf

USER dcf:dcf
WORKDIR /var/lib/dcf

ENV DCF_CONFIG=/etc/dcf/dcf_config.toml
ENV RUST_LOG=info

# 7777/udp: Punctim game/audio traffic; 50051/tcp: gRPC management
EXPOSE 7777/udp 50051/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD /usr/local/bin/dcf status || exit 1

ENTRYPOINT ["/usr/local/bin/dcf"]
CMD ["start"]
