# syntax=docker/dockerfile:1
# ============================================================================
# Punctim / DCF — umbrella certification image
# ============================================================================
# There is no single "Punctim app". The one invariant is the 17-byte
# DeModFrame wire quantum and its cross-language certificate; everything else
# is an adapter over it. So this image ships the PROOF, not a server: it builds
# the reference toolchains (Python + Rust + C + Go) and runs the certification
# suite that gates every push — `make certify` (Python laws + reference-codec
# selftest, regenerated-vs-committed audio vectors, Rust wire+audio certs, C
# wire+audio certs) plus the Go SDK cert (wire + game/audio/text + UDP node).
#
#   docker build -t alh477/punctim:latest .
#   docker run --rm alh477/punctim:latest          # re-runs the full cert
#   docker run --rm alh477/punctim:latest test     # per-language tests
# ----------------------------------------------------------------------------
FROM rust:1.83-bookworm AS cert

ENV DEBIAN_FRONTEND=noninteractive

# Go's toolchain from the official image (Debian's golang-go is too old for the
# module's `go 1.21`). Rust/cargo come from the base image.
COPY --from=golang:1.23-bookworm /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"
ENV GOTOOLCHAIN=local

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        build-essential \
        gcc \
        make \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Only what the certification suite touches — keeps the image lean and the
# build cache stable.
COPY Makefile ./
COPY python/ ./python/
COPY codec/ ./codec/
COPY C_SDK/ ./C_SDK/
COPY Documentation/ ./Documentation/
COPY go/ ./go/

# Run the full cross-language cert at build time so a broken image is never
# produced, let alone pushed.
RUN make certify
RUN cd go && go vet ./... && go test ./...

ENTRYPOINT ["make"]
CMD ["certify"]
