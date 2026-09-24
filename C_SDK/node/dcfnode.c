/* SPDX-License-Identifier: LGPL-3.0-only
 *
 * dcfnode — a real DCF mesh node in C, the analogue of the Rust `dcf` and Go
 * `dcfnode` binaries. It carries the certified 17-byte DeModFrame quantum over a
 * UDP transport using the binary ProtoMessage envelope (dcf_proto.h), so it
 * meshes with the Go/Rust nodes; the certified DCF-Text / DCF-Game adapters
 * (codec/demod_text.h, demod_game.h) fragment messages into frames.
 *
 *   dcfnode version
 *   dcfnode start        [--bind 0.0.0.0:7777]
 *   dcfnode send-text     --peer host:port --channel NAME --text "hi"
 *   dcfnode send-game     --peer host:port --channel-id N --hex DEADBEEF [--type 2]
 *   dcfnode send-position --peer host:port --x 1 --y 2 --z 3
 *
 * Build: gcc -std=c11 -I codec -I C_SDK/node dcfnode.c -o dcfnode  (POSIX sockets)
 */
#define _POSIX_C_SOURCE 200112L  /* getaddrinfo + POSIX sockets under -std=c11 */

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "demod_frame.h"
#include "demod_game.h"
#include "demod_text.h"
#include "demod_modulation.h"
#include "demod_fec.h"
#include "dcf_modem.h"
#include "dcf_proto.h"
#include "dcf_mesh_runtime.h"

static const char *opt(int argc, char **argv, const char *key, const char *def);

#define DCF_NODE_DEFAULT_PORT 7777

static uint64_t now_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (uint64_t)tv.tv_sec * 1000000ull + (uint64_t)tv.tv_usec;
}

static int parse_hostport(const char *s, char *host, size_t hostlen, int *port) {
    const char *colon = strrchr(s, ':');
    if (!colon) return -1;
    size_t hl = (size_t)(colon - s);
    if (hl == 0 || hl >= hostlen) return -1;
    memcpy(host, s, hl);
    host[hl] = '\0';
    *port = atoi(colon + 1);
    return (*port > 0) ? 0 : -1;
}

static int udp_socket_bound(const char *host, int port) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    a.sin_addr.s_addr = (host && *host) ? inet_addr(host) : INADDR_ANY;
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { close(fd); return -1; }
    return fd;
}

/* Send one ProtoMessage(msg_type, payload) to host:port from an ephemeral socket. */
static int send_proto(const char *host, int port, uint8_t msg_type,
                      const uint8_t *payload, uint32_t plen) {
    /* Resolve host (accepts both numeric IPs and DNS names, e.g. container names). */
    char portstr[16];
    snprintf(portstr, sizeof portstr, "%d", port);
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) return -1;
    int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) { freeaddrinfo(res); return -1; }
    uint8_t buf[DCF_PROTO_HEADER_LEN + 4096];
    size_t n = dcf_proto_serialize(msg_type, 0, now_us(), payload, plen, buf, sizeof buf);
    if (n == 0) { close(fd); freeaddrinfo(res); return -1; }
    ssize_t sent = sendto(fd, buf, n, 0, res->ai_addr, res->ai_addrlen);
    close(fd);
    freeaddrinfo(res);
    return (sent == (ssize_t)n) ? 0 : -1;
}

/* Ship each DeModFrame of a packetized message as its own ProtoMessage. */
static int send_frames(const char *host, int port, uint8_t msg_type,
                       uint8_t frames[][DCF_FRAME_SIZE], size_t n) {
    for (size_t i = 0; i < n; i++)
        if (send_proto(host, port, msg_type, frames[i], DCF_FRAME_SIZE) != 0) return -1;
    return 0;
}

static int cmd_version(void) {
    printf("dcfnode 0.3.0 — DCF C SDK node (17-byte DeModFrame, ProtoMessage/UDP)\n");
    printf("  CRC anchors: CRC(\"123456789\")=0x%04X  CRC(0^15)=0x%04X\n",
           dcf_crc16((const uint8_t *)"123456789", 9), dcf_crc16((const uint8_t[15]){0}, 15));
    return 0;
}

static int cmd_start(int argc, char **argv) {
    char host[64] = "0.0.0.0";
    int port = DCF_NODE_DEFAULT_PORT;
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], "--bind") && i + 1 < argc) parse_hostport(argv[++i], host, sizeof host, &port);

    const char *mode = opt(argc, argv, "--mode", "p2p");
    int node_id = atoi(opt(argc, argv, "--node-id", "0"));
    int master_peer = atoi(opt(argc, argv, "--master", "0"));

    int fd = udp_socket_bound(host, port);
    if (fd < 0) { fprintf(stderr, "bind %s:%d failed\n", host, port); return 1; }

    dcf_text_reasm_t tr; dcf_text_reasm_init(&tr);
    dcf_game_reasm_t gr; dcf_game_reasm_init(&gr);

    dcf_mesh_node_t mesh;
    bool mesh_on = strcmp(mode, "p2p") != 0;
    if (mesh_on) {
        dcf_mesh_init(&mesh, fd, mode, node_id, master_peer, 50);
        for (int i = 0; i < argc; i++) {
            if (strcmp(argv[i], "--peer") != 0 || i + 1 >= argc) continue;
            char spec[128];
            snprintf(spec, sizeof spec, "%s", argv[i + 1]);
            char *at = strchr(spec, '@');
            if (!at) continue;
            *at = '\0';
            char ph[64]; int pp;
            if (parse_hostport(at + 1, ph, sizeof ph, &pp) == 0)
                dcf_mesh_add_peer(&mesh, atoi(spec), ph, pp);
        }
        fprintf(stderr, "self-healing mesh: mode=%s node-id=%d master=%d peers=%d\n",
                mode, node_id, master_peer, mesh.n_peers);
        struct timeval tv = {0, 250000}; /* 250 ms recv timeout so the loop ticks */
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    }
    fprintf(stderr, "dcfnode listening on %s:%d (Ctrl-C to stop)\n", host, port);

    uint64_t last_tick = dcf_mesh_now_us();
    for (;;) {
        uint8_t dg[DCF_PROTO_HEADER_LEN + 4096]; /* matches send_proto's max */
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        /* MSG_TRUNC reports the true datagram length so oversize input is
         * dropped loudly instead of parsed as a silently clipped message. */
        ssize_t r = recvfrom(fd, dg, sizeof(dg), MSG_TRUNC, (struct sockaddr *)&from, &fl);
        if (r > (ssize_t)sizeof(dg)) {
            fprintf(stderr, "dcfnode: dropped %zd-byte datagram (max %zu)\n", r, sizeof(dg));
        } else if (r > 0) {
            char fip[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &from.sin_addr, fip, sizeof fip);
            uint8_t mt; uint32_t seq, plen; uint64_t ts; const uint8_t *p;
            if (dcf_proto_deserialize(dg, (size_t)r, &mt, &seq, &ts, &p, &plen)) {
                if (mt == DCF_MSG_PING) {
                    uint8_t pb[DCF_PROTO_HEADER_LEN];
                    size_t n = dcf_proto_serialize(DCF_MSG_PONG, seq, ts, NULL, 0, pb, sizeof pb);
                    if (n == 0 || sendto(fd, pb, n, 0, (struct sockaddr *)&from, fl) != (ssize_t)n)
                        fprintf(stderr, "dcfnode: PONG send to %s failed\n", fip);
                } else if (mt == DCF_MSG_PONG && mesh_on) {
                    uint64_t now = dcf_mesh_now_us();
                    dcf_mesh_on_pong(&mesh, &from, ts < now ? (int)((now - ts) / 1000) : 0);
                } else if (mt == DCF_MSG_MESH && mesh_on) {
                    dcf_mesh_on_control(&mesh, p, (int)plen, &from);
                } else if (mt == DCF_MSG_TEXT_DCF && plen == DCF_FRAME_SIZE) {
                    dcf_text_packet_t msg;
                    if (dcf_text_reasm_push(&tr, p, &msg) == DCF_TEXT_REASM_MESSAGE)
                        printf("text from %s on ch 0x%04X: %.*s\n", fip, msg.dst, msg.payload_len, msg.payload);
                } else if (mt == DCF_MSG_GAME_DCF && plen == DCF_FRAME_SIZE) {
                    dcf_game_packet_t pkt;
                    if (dcf_game_reasm_push(&gr, p, &pkt) == DCF_GAME_REASM_PACKET)
                        printf("game from %s: type=%u %u bytes (packet %u)\n", fip, pkt.msg_type_id, pkt.payload_len, pkt.packet_id);
                } else if (mt == DCF_MSG_FRAME && plen == DCF_FRAME_SIZE) {
                    /* DCF-Medium udp:dialect=proto: a bare frame (e.g. from `punctim io`) */
                    static const char hx[] = "0123456789abcdef";
                    char fh[2 * DCF_FRAME_SIZE + 1];
                    for (size_t k = 0; k < DCF_FRAME_SIZE; k++) {
                        fh[2 * k] = hx[p[k] >> 4];
                        fh[2 * k + 1] = hx[p[k] & 0xFu];
                    }
                    fh[2 * DCF_FRAME_SIZE] = '\0';
                    printf("frame %s from %s:%u\n", fh, fip, (unsigned)ntohs(from.sin_port));
                } else if (mt == DCF_MSG_POSITION && plen == 12) {
                    uint32_t xi = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
                    uint32_t yi = ((uint32_t)p[4] << 24) | ((uint32_t)p[5] << 16) | ((uint32_t)p[6] << 8) | p[7];
                    uint32_t zi = ((uint32_t)p[8] << 24) | ((uint32_t)p[9] << 16) | ((uint32_t)p[10] << 8) | p[11];
                    float x, y, z;
                    memcpy(&x, &xi, 4); memcpy(&y, &yi, 4); memcpy(&z, &zi, 4);
                    printf("position from %s: (%.2f, %.2f, %.2f)\n", fip, x, y, z);
                }
            }
            fflush(stdout);
        } else if (!mesh_on) {
            break; /* blocking recv error in non-mesh mode */
        }
        if (mesh_on) {
            uint64_t now = dcf_mesh_now_us();
            if (now - last_tick >= 1000000) { dcf_mesh_tick(&mesh); last_tick = now; }
        }
    }
    close(fd);
    return 0;
}

static const char *opt(int argc, char **argv, const char *key, const char *def) {
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], key) && i + 1 < argc) return argv[i + 1];
    return def;
}

static size_t unhex(const char *s, uint8_t *out, size_t max) {
    size_t n = 0;
    for (; s[0] && s[1] && n < max; s += 2) {
        unsigned v;
        if (sscanf(s, "%2x", &v) != 1) break;
        out[n++] = (uint8_t)v;
    }
    return n;
}

static int cmd_send_text(int argc, char **argv) {
    const char *peer = opt(argc, argv, "--peer", NULL);
    const char *chan = opt(argc, argv, "--channel", "lobby");
    const char *text = opt(argc, argv, "--text", "hello over DeModFrame");
    if (!peer) { fprintf(stderr, "--peer host:port required\n"); return 2; }
    char host[64]; int port;
    if (parse_hostport(peer, host, sizeof host, &port) != 0) { fprintf(stderr, "bad --peer\n"); return 2; }

    uint16_t dst = dcf_text_channel_id(chan);
    uint8_t frames[DCF_TEXT_MAX_FRAMES][DCF_FRAME_SIZE];
    size_t nf = 0;
    if (!dcf_text_packetize((const uint8_t *)text, strlen(text), 1, (uint32_t)now_us(),
                            1, dst, 0, frames, DCF_TEXT_MAX_FRAMES, &nf)) {
        fprintf(stderr, "packetize failed\n"); return 1;
    }
    if (send_frames(host, port, DCF_MSG_TEXT_DCF, frames, nf) != 0) { fprintf(stderr, "send failed\n"); return 1; }
    printf("sent %zu text frame(s) on ch 0x%04X to %s\n", nf, dst, peer);
    return 0;
}

static int cmd_send_game(int argc, char **argv) {
    const char *peer = opt(argc, argv, "--peer", NULL);
    uint8_t type = (uint8_t)atoi(opt(argc, argv, "--type", "2"));
    const char *hx = opt(argc, argv, "--hex", "deadbeef");
    uint16_t chan = (uint16_t)strtol(opt(argc, argv, "--channel-id", "1"), NULL, 0);
    if (!peer) { fprintf(stderr, "--peer host:port required\n"); return 2; }
    char host[64]; int port;
    if (parse_hostport(peer, host, sizeof host, &port) != 0) { fprintf(stderr, "bad --peer\n"); return 2; }

    uint8_t body[DCF_GAME_MAX_PAYLOAD];
    size_t blen = unhex(hx, body, sizeof body);
    uint8_t frames[DCF_GAME_MAX_FRAMES][DCF_FRAME_SIZE];
    size_t nf = 0;
    if (!dcf_game_packetize(type, body, blen, 1, (uint32_t)now_us(), 1, chan, 0,
                            frames, DCF_GAME_MAX_FRAMES, &nf)) {
        fprintf(stderr, "packetize failed\n"); return 1;
    }
    if (send_frames(host, port, DCF_MSG_GAME_DCF, frames, nf) != 0) { fprintf(stderr, "send failed\n"); return 1; }
    printf("sent %zu game frame(s) (type %u) to %s\n", nf, type, peer);
    return 0;
}

static void put_f32_be(uint8_t *out, float v) {
    uint32_t u;
    memcpy(&u, &v, 4);
    out[0] = (uint8_t)(u >> 24); out[1] = (uint8_t)(u >> 16);
    out[2] = (uint8_t)(u >> 8);  out[3] = (uint8_t)u;
}

static int cmd_send_position(int argc, char **argv) {
    const char *peer = opt(argc, argv, "--peer", NULL);
    if (!peer) { fprintf(stderr, "--peer host:port required\n"); return 2; }
    char host[64]; int port;
    if (parse_hostport(peer, host, sizeof host, &port) != 0) { fprintf(stderr, "bad --peer\n"); return 2; }
    float x = (float)atof(opt(argc, argv, "--x", "0"));
    float y = (float)atof(opt(argc, argv, "--y", "0"));
    float z = (float)atof(opt(argc, argv, "--z", "0"));
    uint8_t payload[12];
    put_f32_be(payload, x); put_f32_be(payload + 4, y); put_f32_be(payload + 8, z);
    if (send_proto(host, port, DCF_MSG_POSITION, payload, 12) != 0) { fprintf(stderr, "send failed\n"); return 1; }
    printf("sent position (%.2f, %.2f, %.2f) to %s\n", x, y, z, peer);
    return 0;
}

/* ── Faust-DSP modem over a file "medium" (stand-in for an acoustic/RF channel) ──
 * Medium file: "DCFM" | mod(u8) | nbytes(u32 BE) | nsamples(u32 BE) | double[nsamples] */
static int modem_id(const char *name) {
    if (!strcmp(name, "fsk")) return DCF_MOD_FSK;
    if (!strcmp(name, "ook")) return DCF_MOD_OOK;
    if (!strcmp(name, "psk")) return DCF_MOD_PSK;
    if (!strcmp(name, "qam")) return DCF_MOD_QAM;
    return -1;
}
static const char *modem_name(uint8_t m) {
    static const char *n[4] = {"fsk", "ook", "psk", "qam"};
    return m < 4 ? n[m] : "?";
}
static void put_u32_be(uint8_t *o, uint32_t v) {
    o[0] = (uint8_t)(v >> 24); o[1] = (uint8_t)(v >> 16); o[2] = (uint8_t)(v >> 8); o[3] = (uint8_t)v;
}
static uint32_t get_u32_be(const uint8_t *o) {
    return ((uint32_t)o[0] << 24) | ((uint32_t)o[1] << 16) | ((uint32_t)o[2] << 8) | o[3];
}

static int cmd_send_modem(int argc, char **argv) {
    const char *medium = opt(argc, argv, "--medium", NULL);
    const char *modn = opt(argc, argv, "--modulation", "qam");
    const char *text = opt(argc, argv, "--text", NULL);
    const char *hx = opt(argc, argv, "--hex", NULL);
    if (!medium) { fprintf(stderr, "--medium PATH required\n"); return 2; }
    int mod = modem_id(modn);
    if (mod < 0) { fprintf(stderr, "--modulation must be fsk|ook|psk|qam\n"); return 2; }

    /* --fec: wrap the payload in Reed-Solomon so the medium can CORRECT errors,
     * not just detect them (codec/demod_fec.h). Single-codeword (<=239 B) in v1. */
    int fec = 0;
    for (int i = 0; i < argc; i++) if (!strcmp(argv[i], "--fec")) fec = 1;
    uint8_t nparity = fec ? (uint8_t)atoi(opt(argc, argv, "--parity", "16")) : 0;

    uint8_t data[1024]; size_t len;
    if (hx) len = unhex(hx, data, sizeof data);
    else { const char *t = text ? text : "modem over different mediums"; len = strlen(t); memcpy(data, t, len); }

    /* Multi-codeword FEC: any-length payload -> chunked, interleaved, RS-coded blob
     * behind a self-protecting header (codec/demod_fec.h). */
    static uint8_t coded[DCF_FEC_BLOB_MAX];
    const uint8_t *modbytes = data;
    size_t clen = len;
    if (fec) {
        size_t bl = dcf_fec_encode_message(data, len, nparity, coded);
        if (bl == 0) { fprintf(stderr, "--fec: payload too large (max %u bytes)\n", DCF_FEC_MSG_MAX); return 2; }
        clen = bl; modbytes = coded;
    }

    static uint8_t syms[16384];
    size_t nsyms = dcf_modulate((uint8_t)mod, modbytes, clen, syms, sizeof syms);
    static double samples[16384 * DCF_MODEM_SPS];
    size_t ns = dcf_modem_render((uint8_t)mod, syms, nsyms, samples, sizeof(samples) / sizeof(samples[0]));

    FILE *f = fopen(medium, "wb");
    if (!f) { fprintf(stderr, "open %s failed\n", medium); return 1; }
    uint8_t hdr[14]; memcpy(hdr, "DCFM", 4); hdr[4] = (uint8_t)mod;
    put_u32_be(hdr + 5, (uint32_t)clen); put_u32_be(hdr + 9, (uint32_t)ns);
    hdr[13] = (uint8_t)(fec ? 1 : 0);               /* 0 = no FEC; blob self-describes parity */
    fwrite(hdr, 1, sizeof hdr, f);
    fwrite(samples, sizeof(double), ns, f);
    fclose(f);
    printf("modulated %zu bytes%s -> %zu %s symbols -> %zu samples on medium %s\n",
           len, fec ? " (RS-FEC)" : "", nsyms, modem_name((uint8_t)mod), ns, medium);
    return 0;
}

static int cmd_recv_modem(int argc, char **argv) {
    const char *medium = opt(argc, argv, "--medium", NULL);
    if (!medium) { fprintf(stderr, "--medium PATH required\n"); return 2; }
    FILE *f = fopen(medium, "rb");
    if (!f) { fprintf(stderr, "open %s failed\n", medium); return 1; }
    uint8_t hdr[14];
    if (fread(hdr, 1, sizeof hdr, f) != sizeof hdr || memcmp(hdr, "DCFM", 4) != 0) {
        fprintf(stderr, "not a DCFM medium\n"); fclose(f); return 1;
    }
    uint8_t mod = hdr[4];
    uint32_t nbytes = get_u32_be(hdr + 5), ns = get_u32_be(hdr + 9);
    uint8_t fec_flag = hdr[13];
    static double samples[16384 * DCF_MODEM_SPS];
    if (ns > sizeof(samples) / sizeof(samples[0])) { fclose(f); return 1; }
    if (fread(samples, sizeof(double), ns, f) != ns) { fclose(f); return 1; }
    fclose(f);

    static uint8_t syms[16384];
    size_t rn = dcf_modem_recover(mod, samples, ns, syms, sizeof syms);
    static uint8_t out[DCF_FEC_BLOB_MAX];
    if (nbytes > sizeof out) return 1;
    dcf_demodulate(mod, syms, rn, out, nbytes);

    if (fec_flag) {
        static uint8_t msg[1024];
        int L = dcf_fec_decode_message(out, nbytes, msg);
        if (L < 0) {
            printf("demodulated %s medium: RS-FEC uncorrectable (%u-byte blob)\n",
                   modem_name(mod), nbytes);
            return 0;
        }
        printf("demodulated %s medium (RS-FEC ok): %d bytes: %.*s\n",
               modem_name(mod), L, (int)L, msg);
    } else {
        printf("demodulated %s medium: %u bytes: %.*s\n", modem_name(mod), nbytes, (int)nbytes, out);
    }
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "dcfnode — DCF C SDK node\n\n"
        "  version\n"
        "  start         [--bind host:port] [--mode p2p|auto|master] [--node-id N]\n"
        "                [--master PEER_ID] [--peer id@host:port ...]\n"
        "  send-text     --peer host:port --channel NAME --text S\n"
        "  send-game     --peer host:port --channel-id N --hex BYTES [--type T]\n"
        "  send-position --peer host:port --x X --y Y --z Z\n"
        "  send-modem    --medium PATH --modulation fsk|ook|psk|qam [--text S | --hex B] [--fec [--parity N]]\n"
        "  recv-modem    --medium PATH\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    const char *cmd = argv[1];
    int rest = argc - 2;
    char **ra = argv + 2;
    if (!strcmp(cmd, "version") || !strcmp(cmd, "-v") || !strcmp(cmd, "--version")) return cmd_version();
    if (!strcmp(cmd, "start")) return cmd_start(rest, ra);
    if (!strcmp(cmd, "send-text")) return cmd_send_text(rest, ra);
    if (!strcmp(cmd, "send-game")) return cmd_send_game(rest, ra);
    if (!strcmp(cmd, "send-position")) return cmd_send_position(rest, ra);
    if (!strcmp(cmd, "send-modem")) return cmd_send_modem(rest, ra);
    if (!strcmp(cmd, "recv-modem")) return cmd_recv_modem(rest, ra);
    usage();
    return 2;
}
