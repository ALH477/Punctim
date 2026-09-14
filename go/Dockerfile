# syntax=docker/dockerfile:1
# ============================================================================
# DCF Go SDK — certification image
# ============================================================================
# The Go module (github.com/ALH477/Punctim/go) is a stdlib-only *library*
# with no main package, so there is no app binary to ship. What this image
# ships instead is the PROOF: it builds the module and its entrypoint runs the
# full certification suite — the 246 golden wire vectors plus the game/audio/
# text adapters and the two-node UDP DcfNode loopback test.
#
# The cert tests read the golden vectors via ../../Documentation/*.json, so this
# MUST be built from the repository ROOT, not from go/:
#
#   docker build -f go/Dockerfile -t alh477/dcf-go:latest .
#   docker run --rm alh477/dcf-go:latest            # runs `go test ./...`
#   docker run --rm alh477/dcf-go:latest vet ./...  # or any other go subcommand
# ----------------------------------------------------------------------------
FROM golang:1.23-alpine AS cert

# The module is stdlib-only (no `require` block) and the in-image cert needs no
# cgo, so no extra packages and no network are required.
WORKDIR /src

# Golden vectors the cert tests load relative to each package (../../Documentation).
COPY Documentation/golden_vectors.json \
     Documentation/game_vectors.json \
     Documentation/audio_vectors.json \
     Documentation/pm_param_vectors.json \
     Documentation/text_vectors.json \
     Documentation/superpack_vectors.json \
     ./Documentation/

# The SDK itself.
COPY go/ ./go/

WORKDIR /src/go

# Fail the image build if the SDK does not vet, build, or certify.
RUN go vet ./... \
 && go build ./... \
 && go test ./...

# Default invocation re-runs the certification suite.
ENTRYPOINT ["go"]
CMD ["test", "./..."]
