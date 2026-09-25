/* SPDX-License-Identifier: LGPL-3.0-only
 *
 * punctim.c — `punctim`, the DCF medium tool (C implementation).
 *
 * Reads DeModFrames from any medium and emits them on any other, byte-deterministically;
 * encodes/decodes single frames; certifies the medium codecs against the compiled-in
 * golden vectors. Same CLI and semantics as the Python reference (python/punctim.py +
 * python/dcf/medium.py) and the Rust/Go/Node ports (Documentation/DCF_MEDIUM_SPEC.md):
 *
 *   punctim version [--json]
 *   punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]
 *                   [--no-validate] [--stats] [--queue N]
 *   punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]
 *   punctim decode  (HEX | --stdin) [--json]
 *   punctim certify [--vectors DIR] [--family NAME ...]
 *
 * Media in this build (URI grammar SCHEME[:k=v,...], `|` separates multi-values, `name=`
 * accepted everywhere, unknown keys are a usage error):
 *   file:path=,append=,follow=,mode=,in=,out=   .dcf stream (byte-wise resync scan)
 *   stdio:                                      .dcf stream on stdin / stdout
 *   hex:path=,append=,follow=,mode=             34 lowercase hex chars + LF per frame
 *   udp:dialect=proto|bare,bind=,peer=a|b,pair=1,flush_ms=20,ts=0|now,seq_start=1
 *   loop:id=NAME     the in-process broadcast medium: a loop: writer delivers to a loop:
 *                    reader with the same id in this process (Python LoopbackMedium) and
 *                    otherwise discards; a loop: reader is an infinite, silent source.
 *   hydra:in=,out=,profile=default|aux,fec=none|rep3|conv,interleave=0|1,base_freq=,
 *         tone_spacing=,baud=,n_tones=,impl=tool,tx=,rx=
 *                    HydraModem WAV spool dirs via fork/exec of frame_tx / frame_rx
 *                    (tx=/rx=, $HYDRA_TX/$HYDRA_RX, or PATH), identical spool semantics
 *                    to python/dcf/transport.py:_DirMedium so every language shares one dir.
 *   l2eth: afsk: audio: sdr: janus:  -> exit 3 (Python-only in v0.1; impl=cffi likewise).
 *
 * `io` is a single-threaded ordered pipeline reader -> frame gate -> writer. Finite inputs
 * (stdio, file/hex without follow) are handled synchronously to EOF and never drop;
 * infinite inputs (udp, loop, hydra, file/hex follow) cross a bounded FIFO (--queue, 256;
 * oldest shed, counted in `dropped`) and run until --count / --seconds / SIGINT (or, with
 * --expect and no --count, until N frames were written).
 *
 * Exit codes: 0 ok · 1 I/O error · 2 usage · 3 medium unsupported · 4 certification
 * failed · 5 invalid frame · 6 --expect not met.
 */
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "demod_medium.h"
#define DCF_MEDIUM_CERTIFY_NO_MAIN
#include "../tests/test_medium_certify.c"

#define PUNCTIM_VERSION "0.3.0"
#define PUNCTIM_IMPL    "c"
#define PT_PATH         4096
#define PT_MAX_PEERS    16

enum { PX_OK = 0, PX_IO = 1, PX_USAGE = 2, PX_UNSUPPORTED = 3, PX_CERT = 4,
       PX_INVALID = 5, PX_EXPECT = 6 };

#if defined(EWOULDBLOCK) && EWOULDBLOCK != EAGAIN
#define PT_WOULDBLOCK(e) ((e) == EAGAIN || (e) == EWOULDBLOCK)
#else
#define PT_WOULDBLOCK(e) ((e) == EAGAIN)
#endif

#if defined(__GNUC__)
#define PT_PRINTF(a, b) __attribute__((format(printf, a, b)))
#else
#define PT_PRINTF(a, b)
#endif

static volatile sig_atomic_t g_stop = 0;
static const char *g_cmd = "punctim";

static int msg(int code, const char *fmt, ...) PT_PRINTF(2, 3);
static int msg(int code, const char *fmt, ...) {
    va_list ap;
    fprintf(stderr, "%s: ", g_cmd);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    return code;
}

static void on_signal(int sig) {
    (void)sig;
    g_stop = 1;
}

/* ── small utilities ───────────────────────────────────────────────────────────── */
static double mono_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static uint64_t epoch_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

static void sleep_s(double s) {
    if (!(s > 0.0)) return;
    struct timespec ts;
    ts.tv_sec = (time_t)s;
    ts.tv_nsec = (long)((s - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
}

static double dmin(double a, double b) { return a < b ? a : b; }

static bool is_ws(int c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\v' || c == '\f' || c == '\n';
}

/* Copy s without surrounding whitespace into out[cap]; false if it does not fit. */
static bool strip_copy(const char *s, char *out, size_t cap) {
    while (*s && is_ws((unsigned char)*s)) s++;
    size_t n = strlen(s);
    while (n && is_ws((unsigned char)s[n - 1])) n--;
    if (n >= cap) return false;
    memcpy(out, s, n);
    out[n] = '\0';
    return true;
}

/* Python-style integer: decimal, or 0x/0X hex; surrounding whitespace allowed. */
static bool parse_u64(const char *s, uint64_t *out) {
    char t[64];
    if (!strip_copy(s, t, sizeof t) || !t[0]) return false;
    const char *p = t;
    unsigned base = 10;
    if (p[0] == '+') p++;
    if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) { base = 16; p += 2; }
    if (!*p) return false;
    uint64_t v = 0;
    for (; *p; p++) {
        int d = dcf__hexval(*p);
        if (d < 0 || (unsigned)d >= base) return false;
        if (v > (UINT64_MAX - (uint64_t)d) / base) return false;
        v = v * base + (uint64_t)d;
    }
    *out = v;
    return true;
}

static bool parse_double(const char *s, double *out) {
    char t[64];
    if (!strip_copy(s, t, sizeof t) || !t[0]) return false;
    char *end = NULL;
    errno = 0;
    double v = strtod(t, &end);
    if (end == t || *end || errno == ERANGE) return false;
    *out = v;
    return true;
}

/* Python str() of the value `int(v) if v.is_integer() else v` (the hydra tool flags). */
static void fmt_num(double v, char *out, size_t cap) {
    if (v >= -9.0e15 && v <= 9.0e15 && (double)(long long)v <= v && (double)(long long)v >= v) {
        snprintf(out, cap, "%lld", (long long)v);
        return;
    }
    for (int prec = 1; prec <= 17; prec++) {       /* shortest round-trip repr */
        snprintf(out, cap, "%.*g", prec, v);
        double back = strtod(out, NULL);
        if (back <= v && back >= v) return;
    }
}

/* JSON string (Python json.dumps, ensure_ascii; bytes >= 0x80 read as latin-1). */
static void json_str(FILE *f, const char *s) {
    fputc('"', f);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        unsigned c = *p;
        switch (c) {
            case '"':  fputs("\\\"", f); break;
            case '\\': fputs("\\\\", f); break;
            case '\n': fputs("\\n", f); break;
            case '\r': fputs("\\r", f); break;
            case '\t': fputs("\\t", f); break;
            case '\b': fputs("\\b", f); break;
            case '\f': fputs("\\f", f); break;
            default:
                if (c < 0x20u || c >= 0x80u) fprintf(f, "\\u%04x", c);
                else fputc((int)c, f);
                break;
        }
    }
    fputc('"', f);
}

/* mkdir -p */
static int mkdirs(const char *path) {
    char tmp[PT_PATH];
    size_t n = strlen(path);
    if (n == 0 || n >= sizeof tmp) return -1;
    memcpy(tmp, path, n + 1);
    for (size_t i = 1; i < n; i++) {
        if (tmp[i] != '/') continue;
        tmp[i] = '\0';
        if (mkdir(tmp, 0777) != 0 && errno != EEXIST) return -1;
        tmp[i] = '/';
    }
    if (mkdir(tmp, 0777) != 0 && errno != EEXIST) return -1;
    return 0;
}

/* ── the medium URI grammar (mirrors python/dcf/medium.py:parse_uri) ─────────────── */
typedef struct { const char *scheme; const char *keys[14]; } scheme_keys_t;
static const scheme_keys_t SCHEMES[] = {
    {"file",  {"path", "mode", "append", "follow", "in", "out", NULL}},
    {"stdio", {NULL}},
    {"hex",   {"path", "mode", "append", "follow", NULL}},
    {"udp",   {"dialect", "bind", "peer", "pair", "flush_ms", "ts", "seq_start", NULL}},
    {"l2eth", {"if", "ethertype", "dst", "mtu", "impl", "id", "flush_ms", NULL}},
    {"loop",  {"id", NULL}},
    {"hydra", {"in", "out", "profile", "fec", "interleave", "base_freq", "tone_spacing",
               "baud", "n_tones", "impl", "tx", "rx", NULL}},
    {"afsk",  {"in", "out", "profile", "fec", NULL}},
    {"audio", {"in", "out", "profile", "fec", NULL}},
    {"sdr",   {"in", "out", "mod", NULL}},
    {"janus", {"in", "out", "pset", "fs", "pset_file", "tx", "rx", NULL}},
    {"mc",    {"rcon", "pass_file", "pass_env", "fifo", "log", "bot", "egress", "ns", "poll_hz", NULL}},
};
#define N_SCHEMES (sizeof SCHEMES / sizeof SCHEMES[0])
#define URI_MAX_KV 32

typedef struct {
    char        buf[PT_PATH];
    char        scheme[16];
    const char *key[URI_MAX_KV];
    const char *val[URI_MAX_KV];
    int         nkv;
} uri_t;

/* 0 ok; otherwise prints the usage error and returns PX_USAGE. */
static int uri_parse(const char *spec, uri_t *u) {
    memset(u, 0, sizeof *u);
    size_t n = strlen(spec);
    if (n >= sizeof u->buf) return msg(PX_USAGE, "medium URI too long");
    memcpy(u->buf, spec, n + 1);
    char *rest = strchr(u->buf, ':');
    if (rest) *rest++ = '\0';
    bool blank = true;
    for (const char *q = spec; *q; q++) if (!is_ws((unsigned char)*q)) blank = false;
    if (blank) return msg(PX_USAGE, "empty medium URI");
    char sch[64];
    if (!strip_copy(u->buf, sch, sizeof sch)) return msg(PX_USAGE, "unknown medium '%.16s...'", u->buf);
    for (char *p = sch; *p; p++) if (*p >= 'A' && *p <= 'Z') *p = (char)(*p - 'A' + 'a');
    const scheme_keys_t *sk = NULL;
    for (size_t i = 0; i < N_SCHEMES; i++)
        if (!strcmp(SCHEMES[i].scheme, sch)) sk = &SCHEMES[i];
    if (!sk)
        return msg(PX_USAGE, "unknown medium '%s' (one of: file, stdio, hex, udp, l2eth, "
                   "loop, hydra, afsk, audio, sdr, janus, mc)", sch);
    snprintf(u->scheme, sizeof u->scheme, "%s", sk->scheme);
    while (rest && *rest) {
        char *item = rest;
        char *comma = strchr(item, ',');
        if (comma) { *comma = '\0'; rest = comma + 1; } else rest = NULL;
        if (!*item) continue;
        char *eq = strchr(item, '=');
        if (!eq) return msg(PX_USAGE, "%s: bad item '%s' (want key=value)", u->scheme, item);
        *eq = '\0';
        /* strip the key */
        while (*item && is_ws((unsigned char)*item)) item++;
        size_t kl = strlen(item);
        while (kl && is_ws((unsigned char)item[kl - 1])) item[--kl] = '\0';
        bool ok = !strcmp(item, "name");
        for (int k = 0; !ok && sk->keys[k]; k++) ok = !strcmp(sk->keys[k], item);
        if (!ok) {
            char keys[256] = "name";
            for (int k = 0; sk->keys[k]; k++) {
                size_t l = strlen(keys);
                snprintf(keys + l, sizeof keys - l, ", %s", sk->keys[k]);
            }
            return msg(PX_USAGE, "%s: unknown key '%s' (keys: %s)", u->scheme, item, keys);
        }
        if (u->nkv >= URI_MAX_KV) return msg(PX_USAGE, "%s: too many keys", u->scheme);
        u->key[u->nkv] = item;
        u->val[u->nkv] = eq + 1;
        u->nkv++;
    }
    return 0;
}

/* The last value given for key (a later duplicate wins), or NULL. */
static const char *uri_get(const uri_t *u, const char *key) {
    const char *v = NULL;
    for (int i = 0; i < u->nkv; i++)
        if (!strcmp(u->key[i], key)) v = u->val[i];
    return v;
}

/* Python truthiness of kw.get(key): present and non-empty. */
static const char *uri_str(const uri_t *u, const char *key) {
    const char *v = uri_get(u, key);
    return (v && *v) ? v : NULL;
}

/* _bool: 1|true|yes|on / 0|false|no|off|"" (case-insensitive). -1 = usage error. */
static int uri_bool(const uri_t *u, const char *key, bool def, bool *out) {
    const char *v = uri_get(u, key);
    if (!v) { *out = def; return 0; }
    char t[16];
    if (!strip_copy(v, t, sizeof t)) return msg(PX_USAGE, "%s='%s': want 0|1", key, v);
    for (char *p = t; *p; p++) if (*p >= 'A' && *p <= 'Z') *p = (char)(*p - 'A' + 'a');
    if (!strcmp(t, "1") || !strcmp(t, "true") || !strcmp(t, "yes") || !strcmp(t, "on")) {
        *out = true;
        return 0;
    }
    if (!t[0] || !strcmp(t, "0") || !strcmp(t, "false") || !strcmp(t, "no") || !strcmp(t, "off")) {
        *out = false;
        return 0;
    }
    return msg(PX_USAGE, "%s='%s': want 0|1", key, v);
}

static int uri_int(const uri_t *u, const char *key, uint64_t def, uint64_t *out) {
    const char *v = uri_get(u, key);
    if (!v) { *out = def; return 0; }
    if (!parse_u64(v, out)) return msg(PX_USAGE, "%s='%s': want an integer (decimal or 0x hex)", key, v);
    return 0;
}

/* host:port (rpartition on ':'); resolves IPv4. */
static int resolve_hostport(const char *key, const char *s, struct sockaddr_in *sa, bool passive) {
    char buf[512];
    if (strlen(s) >= sizeof buf) return msg(PX_USAGE, "%s='%s': want host:port", key, s);
    strcpy(buf, s);
    char *colon = strrchr(buf, ':');
    if (!colon || colon == buf) return msg(PX_USAGE, "%s='%s': want host:port", key, s);
    *colon = '\0';
    uint64_t port;
    if (!parse_u64(colon + 1, &port))
        return msg(PX_USAGE, "%s='%s': want an integer (decimal or 0x hex)", key, colon + 1);
    if (port > 65535) return msg(PX_USAGE, "%s='%s': port out of range", key, s);
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    if (passive) hints.ai_flags = AI_PASSIVE;
    int rc = getaddrinfo(buf, NULL, &hints, &res);
    if (rc != 0 || !res) return msg(PX_IO, "I/O error: cannot resolve '%s': %s", buf, gai_strerror(rc));
    memcpy(sa, res->ai_addr, sizeof *sa);
    sa->sin_port = htons((uint16_t)port);
    freeaddrinfo(res);
    return 0;
}

/* python _peers(): each |-separated item is [id@]host:port (syntax only; not resolved). */
static int check_peers(const char *v) {
    char buf[PT_PATH];
    if (!v) return 0;
    snprintf(buf, sizeof buf, "%s", v);
    char *save = NULL;
    for (char *tok = strtok_r(buf, "|", &save); tok; tok = strtok_r(NULL, "|", &save)) {
        char *at = strrchr(tok, '@');
        char *hp = at ? at + 1 : tok;
        char *colon = strrchr(hp, ':');
        uint64_t port;
        if (!colon || colon == hp) return msg(PX_USAGE, "peer='%s': want host:port", hp);
        if (!parse_u64(colon + 1, &port))
            return msg(PX_USAGE, "peer='%s': want an integer (decimal or 0x hex)", colon + 1);
    }
    return 0;
}

/* ── the FIFO for infinite inputs (drop-oldest, counted) ─────────────────────────── */
typedef struct {
    uint8_t (*buf)[DCF_MEDIUM_FRAME_LEN];
    size_t   cap, head, n;
    uint64_t dropped;
} fifo_t;

static int fifo_init(fifo_t *q, size_t cap) {
    memset(q, 0, sizeof *q);
    q->buf = calloc(cap, DCF_MEDIUM_FRAME_LEN);
    q->cap = cap;
    return q->buf ? 0 : -1;
}

static void fifo_put(fifo_t *q, const uint8_t *f) {
    if (q->n == q->cap) {                 /* shed the oldest */
        q->head = (q->head + 1) % q->cap;
        q->n--;
        q->dropped++;
    }
    memcpy(q->buf[(q->head + q->n) % q->cap], f, DCF_MEDIUM_FRAME_LEN);
    q->n++;
}

static bool fifo_get(fifo_t *q, uint8_t *f) {
    if (!q->n) return false;
    memcpy(f, q->buf[q->head], DCF_MEDIUM_FRAME_LEN);
    q->head = (q->head + 1) % q->cap;
    q->n--;
    return true;
}

/* ── HydraModem tool glue (fork/exec, identical flags to python HydraTransport) ─────── */
enum { HY_CAP_PROFILE = 1, HY_CAP_INTERLEAVE = 2, HY_CAP_PREAMBLE = 4 };

typedef struct {
    char tx[PT_PATH], rx[PT_PATH];
    char in_dir[PT_PATH], out_dir[PT_PATH];
    char name[256];
    char fec[8];
    char prof[16][40];
    int  nprof;
} hydra_t;

/* PATH lookup of an executable (a name with '/' is taken as-is). */
static bool which(const char *name, char *out, size_t cap) {
    if (strchr(name, '/')) {
        if (strlen(name) >= cap) return false;
        strcpy(out, name);
        return access(out, X_OK) == 0;
    }
    const char *path = getenv("PATH");
    if (!path) path = "/usr/local/bin:/usr/bin:/bin";
    while (*path) {
        const char *colon = strchr(path, ':');
        size_t dl = colon ? (size_t)(colon - path) : strlen(path);
        int w = (dl == 0) ? snprintf(out, cap, "./%s", name)
                          : snprintf(out, cap, "%.*s/%s", (int)dl, path, name);
        struct stat st;
        if (w > 0 && (size_t)w < cap && stat(out, &st) == 0 && S_ISREG(st.st_mode) &&
            access(out, X_OK) == 0)
            return true;
        if (!colon) break;
        path = colon + 1;
    }
    return false;
}

/* fork/exec argv (stdin = /dev/null). With out != NULL, stdout (+ stderr when merge) is
 * captured into out[cap] (NUL-terminated, truncated); else both go to /dev/null.
 * Returns the exit status, or -1 if the tool could not be run / was killed. */
static int run_tool(char *const argv[], char *out, size_t cap, bool merge) {
    int pfd[2] = {-1, -1};
    if (out && pipe(pfd) != 0) return -1;
    fflush(stdout);
    fflush(stderr);
    pid_t pid = fork();
    if (pid < 0) {
        if (out) { close(pfd[0]); close(pfd[1]); }
        return -1;
    }
    if (pid == 0) {
        signal(SIGPIPE, SIG_DFL);
        int dn = open("/dev/null", O_RDWR);
        if (dn >= 0) dup2(dn, 0);
        if (out) {
            dup2(pfd[1], 1);
            if (merge) dup2(pfd[1], 2);
            else if (dn >= 0) dup2(dn, 2);
            close(pfd[0]);
            close(pfd[1]);
        } else if (dn >= 0) {
            dup2(dn, 1);
            dup2(dn, 2);
        }
        execv(argv[0], argv);
        _exit(127);
    }
    if (out) {
        close(pfd[1]);
        size_t len = 0;
        char tmp[4096];
        for (;;) {
            ssize_t r = read(pfd[0], tmp, sizeof tmp);
            if (r < 0 && errno == EINTR) continue;
            if (r <= 0) break;
            size_t take = (size_t)r;
            if (cap && len + take > cap - 1) take = cap - 1 - len;
            memcpy(out + len, tmp, take);
            len += take;
        }
        if (cap) out[len] = '\0';
        close(pfd[0]);
    }
    int st = 0;
    while (waitpid(pid, &st, 0) < 0)
        if (errno != EINTR) return -1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

/* The optional flags a frame_tx/frame_rx build understands, from its usage text. */
static int hydra_caps(const char *tool) {
    static char cached_tool[4][PT_PATH];
    static int cached_caps[4], ncached = 0;
    for (int i = 0; i < ncached; i++)
        if (!strcmp(cached_tool[i], tool)) return cached_caps[i];
    char path[PT_PATH], usage[8192];
    snprintf(path, sizeof path, "%s", tool);
    char *argv[2] = {path, NULL};
    usage[0] = '\0';
    if (run_tool(argv, usage, sizeof usage, true) < 0) usage[0] = '\0';
    int caps = (strstr(usage, "--profile") ? HY_CAP_PROFILE : 0) |
               (strstr(usage, "--interleave") ? HY_CAP_INTERLEAVE : 0) |
               (strstr(usage, "--preamble") ? HY_CAP_PREAMBLE : 0);
    if (ncached < 4) {
        snprintf(cached_tool[ncached], PT_PATH, "%s", tool);
        cached_caps[ncached++] = caps;
    }
    return caps;
}

static void hy_add(hydra_t *h, const char *a) {
    if (h->nprof < 16) snprintf(h->prof[h->nprof++], sizeof h->prof[0], "%s", a);
}

static int hydra_tool(const uri_t *u, const char *key, const char *env, const char *def,
                      char *out) {
    const char *t = uri_str(u, key);
    if (!t) t = getenv(env);
    if (t && *t) {
        if (strchr(t, '/')) {
            if (strlen(t) >= PT_PATH) return -1;
            strcpy(out, t);
            return 0;
        }
        return which(t, out, PT_PATH) ? 0 : -1;
    }
    return which(def, out, PT_PATH) ? 0 : -1;
}

/* Parse + validate a hydra: URI into h (fec flag + profile flags in the Python order). */
static int hydra_setup(const uri_t *u, hydra_t *h, bool as_reader) {
    memset(h, 0, sizeof *h);
    const char *in = uri_str(u, "in"), *out = uri_str(u, "out");
    if (as_reader && !in) return msg(PX_USAGE, "hydra as input needs in=<dir>");
    if (!as_reader && !out) return msg(PX_USAGE, "hydra as output needs out=<dir>");
    const char *name = uri_get(u, "name");
    snprintf(h->name, sizeof h->name, "%s", name ? name : "hydra");
    const char *fec = uri_get(u, "fec");
    if (!fec) fec = "conv";
    int fec_mode;
    if (!strcmp(fec, "none")) fec_mode = DCF_HYDRA_FEC_NONE;
    else if (!strcmp(fec, "rep3")) fec_mode = DCF_HYDRA_FEC_REP3;
    else if (!strcmp(fec, "conv")) fec_mode = DCF_HYDRA_FEC_CONV;
    else return msg(PX_USAGE, "fec='%s': want none|rep3|conv", fec);
    snprintf(h->fec, sizeof h->fec, "--%s", fec);
    const char *profile = uri_get(u, "profile");
    if (!profile) profile = "default";
    if (strcmp(profile, "default") != 0 && strcmp(profile, "aux") != 0)
        return msg(PX_USAGE, "profile='%s': want default|aux", profile);
    bool il = true;
    bool il_given = uri_get(u, "interleave") != NULL;
    if (uri_bool(u, "interleave", true, &il)) return PX_USAGE;

    static const char *const NUMK[4] = {"base_freq", "tone_spacing", "baud", "n_tones"};
    static const char *const NUMF[4] = {"--base-freq", "--tone-spacing", "--baud", "--n-tones"};
    char numv[4][40];
    bool numg[4] = {false, false, false, false};
    double numd[4] = {0, 0, 0, 0};
    for (int k = 0; k < 4; k++) {
        const char *v = uri_get(u, NUMK[k]);
        if (!v) continue;
        numg[k] = true;
        if (k == 3) {
            uint64_t iv;
            if (!parse_u64(v, &iv) || iv > 65536)
                return msg(PX_USAGE, "%s='%s': want an integer (decimal or 0x hex)", NUMK[k], v);
            numd[k] = (double)iv;
            snprintf(numv[k], sizeof numv[k], "%llu", (unsigned long long)iv);
        } else {
            if (!parse_double(v, &numd[k])) return msg(PX_USAGE, "%s='%s': want a number", NUMK[k], v);
            fmt_num(numd[k], numv[k], sizeof numv[k]);
        }
    }
    const char *impl = uri_get(u, "impl");
    if (!impl) impl = "tool";
    if (strcmp(impl, "tool") != 0 && strcmp(impl, "cffi") != 0)
        return msg(PX_USAGE, "impl='%s': want tool|cffi", impl);
    if (!strcmp(impl, "cffi"))
        return msg(PX_UNSUPPORTED, "medium unsupported: hydra:impl=cffi (libhydramodem is not "
                   "linked into C punctim in v0.1; use impl=tool)");

    /* The effective profile must pass hydra_profile_init (the tools would reject it). */
    dcf_hydra_profile_t p;
    dcf_hydra_profile_named(&p, profile);
    p.fec_mode = fec_mode;
    p.interleave = il ? 1 : 0;
    if (numg[0]) p.base_freq = numd[0];
    if (numg[1]) p.tone_spacing = numd[1];
    if (numg[2]) p.baud = numd[2];
    if (numg[3]) p.n_tones = (int)numd[3];
    if (dcf_hydra_profile_init(&p) != 0)
        return msg(PX_USAGE, "hydra: the profile is rejected by hydra_profile_init (n_tones a "
                   "power of two >= 2, tones below Nyquist, base_freq/tone_spacing integer "
                   "multiples of baud)");

    if (hydra_tool(u, "tx", "HYDRA_TX", "frame_tx", h->tx) != 0 ||
        hydra_tool(u, "rx", "HYDRA_RX", "frame_rx", h->rx) != 0)
        return msg(PX_UNSUPPORTED, "medium unsupported: HydraModem tools not found: build "
                   "hydramodem/dcf-tools (build.sh) and put frame_tx/frame_rx on PATH or in "
                   "$HYDRA_TX/$HYDRA_RX (or tx=/rx=)");
    int caps = hydra_caps(h->tx) & hydra_caps(h->rx);
    if (!strcmp(profile, "aux")) {
        if (caps & HY_CAP_PROFILE) {
            hy_add(h, "--profile");
            hy_add(h, "aux");
        } else {
            static const char *const AUX[6] = {"--base-freq", "1200", "--tone-spacing", "1200",
                                               "--baud", "1200"};
            for (int k = 0; k < 6; k++) hy_add(h, AUX[k]);
            if (caps & HY_CAP_PREAMBLE) { hy_add(h, "--preamble"); hy_add(h, "16"); }
        }
    }
    if (il_given && !il) {
        if (!(caps & HY_CAP_INTERLEAVE))
            return msg(PX_UNSUPPORTED, "medium unsupported: hydra: interleave=0 needs "
                       "frame_tx/frame_rx with --interleave (rebuild hydramodem/dcf-tools)");
        hy_add(h, "--interleave");
        hy_add(h, "0");
    }
    for (int k = 0; k < 4; k++) {
        if (!numg[k]) continue;
        hy_add(h, NUMF[k]);
        hy_add(h, numv[k]);
    }
    if (in) snprintf(h->in_dir, sizeof h->in_dir, "%s", in);
    if (out) snprintf(h->out_dir, sizeof h->out_dir, "%s", out);
    const char *dirs[2] = {in, out};                  /* python _DirMedium makedirs */
    for (int k = 0; k < 2; k++)
        if (dirs[k] && mkdirs(dirs[k]) != 0)
            return msg(PX_IO, "I/O error: cannot create %s: %s", dirs[k], strerror(errno));
    return 0;
}

/* argv = [tool, (hex)?, path, fec, prof...]; returns the tool's exit status. */
static int hydra_run(hydra_t *h, bool tx, char *hex, char *path, char *out, size_t cap) {
    char *argv[24];
    int a = 0;
    argv[a++] = tx ? h->tx : h->rx;
    if (hex) argv[a++] = hex;
    argv[a++] = path;
    argv[a++] = h->fec;
    for (int k = 0; k < h->nprof; k++) argv[a++] = h->prof[k];
    argv[a] = NULL;
    return run_tool(argv, out, cap, false);
}

/* ── the pipeline context ──────────────────────────────────────────────────────── */
typedef struct writer writer_t;
typedef struct {
    fifo_t    q;
    writer_t *w;
    bool      finite, validate, limited, werr;
    uint64_t  limit;
    uint64_t  frames_in, frames_out, invalid;
    int       werrno;
} io_t;

static int wr_write(writer_t *w, const uint8_t *f);

static bool io_done(const io_t *io) { return io->limited && io->frames_out >= io->limit; }

/* reader -> frame gate -> writer, for one frame (python medium.run_io handle()). */
static void io_handle(io_t *io, const uint8_t *f) {
    io->frames_in++;
    if (io->validate && !dcf_medium_gate(f)) {
        io->invalid++;
        return;
    }
    if (wr_write(io->w, f) != 0) {
        io->werr = true;
        io->werrno = errno;
        return;
    }
    io->frames_out++;
}

/* A reader produced a frame: finite inputs handle it synchronously (never drop);
 * infinite inputs queue it (shed the oldest when full). */
static void io_emit(io_t *io, const uint8_t *f) {
    if (io->finite) {
        if (!io_done(io) && !io->werr) io_handle(io, f);
    } else {
        fifo_put(&io->q, f);
    }
}

static void io_emit_cb(const uint8_t *f, void *user) { io_emit((io_t *)user, f); }

/* ── readers ───────────────────────────────────────────────────────────────────── */
enum { MK_FILE, MK_STDIO, MK_HEX, MK_UDP, MK_LOOP, MK_HYDRA };

typedef struct {
    int      kind;
    bool     finite;
    /* stream / hex over an fd, or a followed path */
    int      fd;
    bool     own_fd, hex, follow_path, eof;
    char     path[PT_PATH];
    off_t    pos;
    double   next_scan;
    dcf_stream_scanner_t sc;
    dcf_hex_line_t line;
    uint64_t bad_lines;
    /* udp */
    int      sock;
    bool     bare;
    uint64_t invalid;
    /* loop */
    char     loop_id[256];
    /* hydra */
    hydra_t  hy;
    char   **seen;
    size_t   nseen, capseen;
} reader_t;

static reader_t *g_loop_reader = NULL;   /* the in-process loop: medium */
static io_t     *g_loop_io = NULL;

static uint8_t g_iobuf[65536];

static void rd_consume(reader_t *r, io_t *io, const uint8_t *buf, size_t n) {
    if (!r->hex) {
        dcf_stream_feed(&r->sc, buf, n, io_emit_cb, io);
        return;
    }
    uint8_t f[DCF_MEDIUM_FRAME_LEN];
    for (size_t i = 0; i < n; i++) {
        if (buf[i] != '\n') {
            dcf_hex_line_push(&r->line, (char)buf[i]);
            continue;
        }
        dcf_hex_line_result_t lr = dcf_hex_line_end(&r->line, f);
        if (lr == DCF_HEX_FRAME) io_emit(io, f);
        else if (lr == DCF_HEX_BAD) r->bad_lines++;
    }
}

/* End of a finite input: the last line without '\n' is still a line; a stream's
 * trailing < 17 bytes are tail bytes (not counted as skipped). */
static void rd_finish(reader_t *r, io_t *io) {
    if (r->hex) {
        uint8_t f[DCF_MEDIUM_FRAME_LEN];
        dcf_hex_line_result_t lr = dcf_hex_line_end(&r->line, f);
        if (lr == DCF_HEX_FRAME) io_emit(io, f);
        else if (lr == DCF_HEX_BAD) r->bad_lines++;
    } else {
        (void)dcf_stream_flush(&r->sc);
    }
}

/* Poll protocol: 1 progress, 0 idle, -1 EOF (finite), -2 I/O error (errno set). */
static int rd_fd_poll(reader_t *r, io_t *io, double timeout) {
    if (r->eof) {
        if (r->finite) return -1;
        sleep_s(timeout);              /* an infinite input past EOF (hex:follow=1 on stdin) */
        return 0;
    }
    struct pollfd p;
    p.fd = r->fd;
    p.events = POLLIN;
    p.revents = 0;
    int s = poll(&p, 1, (int)(timeout * 1000.0 + 0.5));
    if (s < 0) return errno == EINTR ? 0 : -2;
    if (s == 0) return 0;
    ssize_t n = read(r->fd, g_iobuf, sizeof g_iobuf);
    if (n < 0) return (errno == EINTR || errno == EAGAIN) ? 0 : -2;
    if (n == 0) {
        rd_finish(r, io);
        r->eof = true;
        return r->finite ? -1 : 0;
    }
    rd_consume(r, io, g_iobuf, (size_t)n);
    return 1;
}

/* follow=1 on a path: re-open every 0.2 s, read the growth (python FileTransport._tail /
 * HexTransport._reader); a missing file is simply not there yet. */
static int rd_follow_poll(reader_t *r, io_t *io, double timeout) {
    double t = mono_now();
    if (t < r->next_scan) {
        sleep_s(dmin(timeout, r->next_scan - t));
        return 0;
    }
    int got = 0;
    int fd = open(r->path, O_RDONLY);
    if (fd >= 0) {
        if (lseek(fd, r->pos, SEEK_SET) >= 0) {
            for (;;) {
                ssize_t n = read(fd, g_iobuf, sizeof g_iobuf);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) break;
                r->pos += (off_t)n;
                rd_consume(r, io, g_iobuf, (size_t)n);
                got = 1;
            }
        }
        close(fd);
    }
    r->next_scan = mono_now() + 0.2;
    return got;
}

static int rd_udp_poll(reader_t *r, io_t *io, double timeout) {
    struct pollfd p;
    p.fd = r->sock;
    p.events = POLLIN;
    p.revents = 0;
    int s = poll(&p, 1, (int)(timeout * 1000.0 + 0.5));
    if (s < 0) return errno == EINTR ? 0 : -2;
    if (s == 0) return 0;
    int got = 0;
    for (int k = 0; k < 64; k++) {
        ssize_t n = recv(r->sock, g_iobuf, sizeof g_iobuf, 0);
        if (n < 0) {
            if (PT_WOULDBLOCK(errno) || errno == EINTR) break;
            return -2;
        }
        got = 1;
        size_t len = (size_t)n;
        uint8_t f[DCF_MEDIUM_FRAME_LEN];
        if (!r->bare) {
            uint8_t t;
            uint32_t seq, plen;
            uint64_t ts;
            const uint8_t *pl;
            if (!dcf_medium_proto_decode(g_iobuf, len, &t, &seq, &ts, &pl, &plen)) {
                r->invalid++;
                continue;
            }
            if (t != DCF_MEDIUM_MSG_FRAME) continue;   /* an adapter envelope, not a frame */
            if (plen != DCF_MEDIUM_FRAME_LEN) { r->invalid++; continue; }
            memcpy(f, pl, DCF_MEDIUM_FRAME_LEN);
            io_emit(io, f);
        } else {
            uint8_t two[2][17];
            int m = dcf_bare_unpack(g_iobuf, len, two);
            if (m == 0) { r->invalid++; continue; }
            for (int j = 0; j < m; j++) io_emit(io, two[j]);
        }
    }
    return got;
}

static int cmp_str(const void *a, const void *b) {
    return strcmp(*(char *const *)a, *(char *const *)b);
}

static bool hy_seen(const reader_t *r, const char *name) {
    size_t lo = 0, hi = r->nseen;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        int c = strcmp(r->seen[mid], name);
        if (c == 0) return true;
        if (c < 0) lo = mid + 1; else hi = mid;
    }
    return false;
}

static void hy_mark(reader_t *r, const char *name) {
    size_t lo = 0, hi = r->nseen;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (strcmp(r->seen[mid], name) < 0) lo = mid + 1; else hi = mid;
    }
    if (r->nseen == r->capseen) {
        size_t nc = r->capseen ? 2 * r->capseen : 64;
        char **ns = realloc(r->seen, nc * sizeof *ns);
        if (!ns) return;
        r->seen = ns;
        r->capseen = nc;
    }
    char *dup = malloc(strlen(name) + 1);
    if (!dup) return;
    strcpy(dup, name);
    memmove(r->seen + lo + 1, r->seen + lo, (r->nseen - lo) * sizeof *r->seen);
    r->seen[lo] = dup;
    r->nseen++;
}

/* The hydra spool dir (python _DirMedium._tail): every 0.1 s, each unseen *.wav (names
 * starting with '.' skipped) in sorted order goes through frame_rx once. */
static int rd_hydra_poll(reader_t *r, io_t *io, double timeout, double deadline) {
    double t = mono_now();
    if (t < r->next_scan) {
        sleep_s(dmin(timeout, r->next_scan - t));
        return 0;
    }
    char **names = NULL;
    size_t n = 0, cap = 0;
    DIR *d = opendir(r->hy.in_dir);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            size_t l = strlen(e->d_name);
            if (e->d_name[0] == '.' || l < 4 || strcmp(e->d_name + l - 4, ".wav") != 0) continue;
            if (hy_seen(r, e->d_name)) continue;
            if (n == cap) {
                size_t nc = cap ? 2 * cap : 32;
                char **nn = realloc(names, nc * sizeof *nn);
                if (!nn) break;
                names = nn;
                cap = nc;
            }
            names[n] = malloc(l + 1);
            if (!names[n]) break;
            strcpy(names[n++], e->d_name);
        }
        closedir(d);
    }
    if (n) qsort(names, n, sizeof *names, cmp_str);
    int got = 0;
    for (size_t i = 0; i < n; i++) {
        if (!g_stop && !(deadline > 0.0 && mono_now() >= deadline)) {
            hy_mark(r, names[i]);
            char path[PT_PATH], out[256];
            if (snprintf(path, sizeof path, "%s/%s", r->hy.in_dir, names[i]) < (int)sizeof path &&
                hydra_run(&r->hy, false, NULL, path, out, sizeof out) == 0) {
                char h[64];
                uint8_t f[DCF_MEDIUM_FRAME_LEN];
                bool ok = strip_copy(out, h, sizeof h) && strlen(h) == 34;
                for (size_t k = 0; ok && k < DCF_MEDIUM_FRAME_LEN; k++) {
                    int hi = dcf__hexval(h[2 * k]), lo = dcf__hexval(h[2 * k + 1]);
                    if (hi < 0 || lo < 0) ok = false;
                    else f[k] = (uint8_t)((hi << 4) | lo);
                }
                if (ok) { io_emit(io, f); got = 1; }
            }
        }
        free(names[i]);
    }
    free(names);
    r->next_scan = mono_now() + 0.1;
    return got;
}

static int rd_poll(reader_t *r, io_t *io, double timeout, double deadline) {
    if (timeout < 0.0) timeout = 0.0;
    switch (r->kind) {
        case MK_FILE: case MK_STDIO: case MK_HEX:
            return r->follow_path ? rd_follow_poll(r, io, timeout) : rd_fd_poll(r, io, timeout);
        case MK_UDP:   return rd_udp_poll(r, io, timeout);
        case MK_HYDRA: return rd_hydra_poll(r, io, timeout, deadline);
        case MK_LOOP:  sleep_s(timeout); return 0;
        default:       return -2;
    }
}

static int open_udp_socket(const struct sockaddr_in *bind_sa) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (bind(fd, (const struct sockaddr *)bind_sa, sizeof *bind_sa) != 0) {
        int e = errno;
        close(fd);
        errno = e;
        return -1;
    }
    return fd;
}

static int media_unsupported(const char *scheme) {
    return msg(PX_UNSUPPORTED, "medium unsupported: %s: is not in the C build (v0.1: file, "
               "stdio, hex, udp, loop, hydra); use the Python punctim for %s:", scheme, scheme);
}

static int rd_open(reader_t *r, const char *spec, io_t *io) {
    memset(r, 0, sizeof *r);
    r->fd = -1;
    r->sock = -1;
    dcf_stream_scanner_init(&r->sc);
    dcf_hex_line_reset(&r->line);
    uri_t u;
    int rc = uri_parse(spec, &u);
    if (rc) return rc;
    const char *s = u.scheme;
    if (!strcmp(s, "stdio")) {
        r->kind = MK_STDIO;
        r->finite = true;
        r->fd = 0;
        return 0;
    }
    if (!strcmp(s, "file") || !strcmp(s, "hex")) {
        bool file = !strcmp(s, "file");
        bool follow;                                     /* python finite(): follow only */
        if (uri_bool(&u, "follow", false, &follow)) return PX_USAGE;
        r->kind = file ? MK_FILE : MK_HEX;
        r->hex = !file;
        r->finite = !follow;
        const char *path = uri_str(&u, "path");
        if (!path && file) path = uri_str(&u, "in");
        if (file && !path) return msg(PX_USAGE, "file: needs path=");
        if (!path) {                                     /* hex: on stdin */
            r->fd = 0;
            return 0;
        }
        snprintf(r->path, sizeof r->path, "%s", path);
        if (follow) {
            r->follow_path = true;
            return 0;
        }
        r->fd = open(path, O_RDONLY);
        if (r->fd < 0) return msg(PX_IO, "I/O error: %s: %s", path, strerror(errno));
        r->own_fd = true;
        return 0;
    }
    if (!strcmp(s, "udp")) {
        r->kind = MK_UDP;
        r->finite = false;
        if (!uri_get(&u, "bind")) return msg(PX_USAGE, "udp as input needs bind=host:port");
        const char *dialect = uri_get(&u, "dialect");
        if (!dialect) dialect = "proto";
        if (strcmp(dialect, "proto") != 0 && strcmp(dialect, "bare") != 0)
            return msg(PX_USAGE, "dialect='%s': want proto|bare", dialect);
        r->bare = !strcmp(dialect, "bare");
        bool pair;
        uint64_t tmp;
        if (uri_bool(&u, "pair", true, &pair) || uri_int(&u, "flush_ms", 20, &tmp) ||
            uri_int(&u, "seq_start", 1, &tmp))
            return PX_USAGE;
        const char *ts = uri_get(&u, "ts");
        if (ts && strcmp(ts, "0") != 0 && strcmp(ts, "now") != 0)
            return msg(PX_USAGE, "ts='%s': want 0|now", ts);
        if ((rc = check_peers(uri_get(&u, "peer"))) != 0) return rc;
        struct sockaddr_in sa;
        if ((rc = resolve_hostport("bind", uri_get(&u, "bind"), &sa, true)) != 0) return rc;
        r->sock = open_udp_socket(&sa);
        if (r->sock < 0) return msg(PX_IO, "I/O error: udp bind %s: %s", uri_get(&u, "bind"), strerror(errno));
        int big = 1 << 22;
        setsockopt(r->sock, SOL_SOCKET, SO_RCVBUF, &big, sizeof big);
        fcntl(r->sock, F_SETFL, fcntl(r->sock, F_GETFL) | O_NONBLOCK);
        return 0;
    }
    if (!strcmp(s, "loop")) {
        r->kind = MK_LOOP;
        r->finite = false;
        const char *id = uri_get(&u, "id");
        snprintf(r->loop_id, sizeof r->loop_id, "%s", id ? id : "default");
        g_loop_reader = r;
        g_loop_io = io;
        return 0;
    }
    if (!strcmp(s, "hydra")) {
        r->kind = MK_HYDRA;
        r->finite = false;
        return hydra_setup(&u, &r->hy, true);
    }
    if (!strcmp(s, "l2eth") || !strcmp(s, "afsk") || !strcmp(s, "audio") ||
        !strcmp(s, "sdr") || !strcmp(s, "janus")) {
        if (strcmp(s, "l2eth") != 0 && !uri_str(&u, "in"))
            return msg(PX_USAGE, "%s as input needs in=<dir>", s);
        return media_unsupported(s);
    }
    return media_unsupported(s);   /* a known scheme this build cannot open (uri_parse already rejected unknown ones) */
}

static void rd_close(reader_t *r) {
    if (r->own_fd && r->fd >= 0) close(r->fd);
    if (r->sock >= 0) close(r->sock);
    for (size_t i = 0; i < r->nseen; i++) free(r->seen[i]);
    free(r->seen);
    if (g_loop_reader == r) { g_loop_reader = NULL; g_loop_io = NULL; }
}

/* ── writers ───────────────────────────────────────────────────────────────────── */
struct writer {
    int      kind;
    FILE    *fp;
    bool     own_fp, hex;
    /* udp */
    int      sock;
    bool     bare, pair, ts_now;
    double   flush_s;
    uint32_t seq;
    struct sockaddr_in peer[PT_MAX_PEERS];
    int      npeer;
    uint8_t  pending[DCF_MEDIUM_FRAME_LEN];
    bool     has_pending;
    double   pending_t;
    /* loop */
    char     loop_id[256];
    /* hydra */
    hydra_t  hy;
    uint64_t nfile;
};

static int udp_send(writer_t *w, const uint8_t *dg, size_t len) {
    for (int i = 0; i < w->npeer; i++) {
        for (;;) {
            ssize_t n = sendto(w->sock, dg, len, 0, (const struct sockaddr *)&w->peer[i],
                               sizeof w->peer[i]);
            if (n >= 0) break;
            if (errno == EINTR) continue;
            if (errno == ENOBUFS || PT_WOULDBLOCK(errno)) { sleep_s(0.001); continue; }
            return -1;
        }
    }
    return 0;
}

static int wr_flush_pending(writer_t *w) {
    if (!w->has_pending) return 0;
    w->has_pending = false;
    return udp_send(w, w->pending, DCF_MEDIUM_FRAME_LEN);
}

static int wr_write(writer_t *w, const uint8_t *f) {
    switch (w->kind) {
        case MK_FILE: case MK_STDIO:
            return fwrite(f, 1, DCF_MEDIUM_FRAME_LEN, w->fp) == DCF_MEDIUM_FRAME_LEN ? 0 : -1;
        case MK_HEX: {
            char line[36];
            dcf_hex_encode_line(f, line);
            return fwrite(line, 1, DCF_HEX_LINE_LEN, w->fp) == DCF_HEX_LINE_LEN ? 0 : -1;
        }
        case MK_UDP: {
            if (!w->bare) {
                uint8_t dg[DCF_PROTO_FRAME_LEN];
                uint64_t ts = w->ts_now ? epoch_us() : 0;
                dcf_proto_frame_encode(f, w->seq, ts, dg);
                w->seq++;
                return udp_send(w, dg, sizeof dg);
            }
            if (!w->pair || !dcf_medium_gate(f)) {       /* keep order: a held frame first */
                if (wr_flush_pending(w)) return -1;
                return udp_send(w, f, DCF_MEDIUM_FRAME_LEN);
            }
            if (!w->has_pending) {
                memcpy(w->pending, f, DCF_MEDIUM_FRAME_LEN);
                w->has_pending = true;
                w->pending_t = mono_now();
                return 0;
            }
            uint8_t sp[DCF_SUPER_LEN];
            w->has_pending = false;
            if (!dcf_superpack_pack(w->pending, f, sp)) return -1;   /* both gated: unreachable */
            return udp_send(w, sp, DCF_SUPER_LEN);
        }
        case MK_LOOP:
            if (g_loop_reader && g_loop_io && !strcmp(g_loop_reader->loop_id, w->loop_id))
                fifo_put(&g_loop_io->q, f);
            return 0;
        case MK_HYDRA: {
            w->nfile++;
            char tmp[PT_PATH], fin[PT_PATH], hex[36];
            unsigned long long n = (unsigned long long)w->nfile;
            if (snprintf(tmp, sizeof tmp, "%s/.%s-%llu.wav.tmp", w->hy.out_dir, w->hy.name, n) >= (int)sizeof tmp ||
                snprintf(fin, sizeof fin, "%s/%s-%08llu.wav", w->hy.out_dir, w->hy.name, n) >= (int)sizeof fin) {
                errno = ENAMETOOLONG;
                return -1;
            }
            dcf_hex_encode_line(f, hex);
            hex[34] = '\0';
            int st = hydra_run(&w->hy, true, hex, tmp, NULL, 0);
            if (st != 0) {
                unlink(tmp);
                fprintf(stderr, "%s: frame_tx failed (status %d) for %s\n", g_cmd, st, hex);
                errno = EIO;
                return -1;
            }
            if (rename(tmp, fin) != 0) return -1;   /* atomic publish */
            return 0;
        }
        default:
            return -1;
    }
}

/* Idle hook (python Transport._idle): the bare-UDP lone-frame timer; streams flushed so a
 * tailing consumer sees whole frames promptly. Only called for infinite inputs. */
static int wr_poll(writer_t *w) {
    if (w->kind == MK_UDP && w->has_pending && mono_now() - w->pending_t >= w->flush_s)
        return wr_flush_pending(w);
    if (w->fp) fflush(w->fp);
    return 0;
}

static int wr_open(writer_t *w, const char *spec) {
    memset(w, 0, sizeof *w);
    w->sock = -1;
    uri_t u;
    int rc = uri_parse(spec, &u);
    if (rc) return rc;
    const char *s = u.scheme;
    if (!strcmp(s, "stdio")) {
        w->kind = MK_STDIO;
        w->fp = stdout;
        return 0;
    }
    if (!strcmp(s, "file") || !strcmp(s, "hex")) {
        bool file = !strcmp(s, "file");
        bool append;                                     /* python open_writer: append only */
        if (uri_bool(&u, "append", false, &append)) return PX_USAGE;
        w->kind = file ? MK_FILE : MK_HEX;
        w->hex = !file;
        const char *path = uri_str(&u, "path");
        if (!path && file) path = uri_str(&u, "out");
        if (file && !path) return msg(PX_USAGE, "file: needs path=");
        if (!path) {
            w->fp = stdout;
            return 0;
        }
        w->fp = fopen(path, append ? "ab" : "wb");
        if (!w->fp) return msg(PX_IO, "I/O error: %s: %s", path, strerror(errno));
        w->own_fp = true;
        return 0;
    }
    if (!strcmp(s, "udp")) {
        w->kind = MK_UDP;
        if (!uri_str(&u, "peer")) return msg(PX_USAGE, "udp as output needs peer=host:port");
        const char *dialect = uri_get(&u, "dialect");
        if (!dialect) dialect = "proto";
        if (strcmp(dialect, "proto") != 0 && strcmp(dialect, "bare") != 0)
            return msg(PX_USAGE, "dialect='%s': want proto|bare", dialect);
        w->bare = !strcmp(dialect, "bare");
        uint64_t flush_ms, seq_start;
        if (uri_bool(&u, "pair", true, &w->pair) || uri_int(&u, "flush_ms", 20, &flush_ms) ||
            uri_int(&u, "seq_start", 1, &seq_start))
            return PX_USAGE;
        w->flush_s = (double)flush_ms / 1000.0;
        w->seq = (uint32_t)(seq_start & 0xFFFFFFFFu);
        const char *ts = uri_get(&u, "ts");
        if (!ts) ts = "0";
        if (strcmp(ts, "0") != 0 && strcmp(ts, "now") != 0)
            return msg(PX_USAGE, "ts='%s': want 0|now", ts);
        w->ts_now = !strcmp(ts, "now");
        const char *bind_s = uri_get(&u, "bind");
        struct sockaddr_in bsa;
        if ((rc = resolve_hostport("bind", bind_s ? bind_s : "0.0.0.0:0", &bsa, true)) != 0) return rc;
        /* peers: host:port|host:port (the legacy id@host:port accepted) */
        char pbuf[PT_PATH];
        snprintf(pbuf, sizeof pbuf, "%s", uri_get(&u, "peer"));
        char *save = NULL;
        for (char *tok = strtok_r(pbuf, "|", &save); tok; tok = strtok_r(NULL, "|", &save)) {
            if (!*tok) continue;
            if (w->npeer >= PT_MAX_PEERS) return msg(PX_USAGE, "peer: at most %d peers", PT_MAX_PEERS);
            char *at = strrchr(tok, '@');
            if ((rc = resolve_hostport("peer", at ? at + 1 : tok, &w->peer[w->npeer], false)) != 0)
                return rc;
            w->npeer++;
        }
        w->sock = open_udp_socket(&bsa);
        if (w->sock < 0) return msg(PX_IO, "I/O error: udp bind: %s", strerror(errno));
        return 0;
    }
    if (!strcmp(s, "loop")) {
        w->kind = MK_LOOP;
        const char *id = uri_get(&u, "id");
        snprintf(w->loop_id, sizeof w->loop_id, "%s", id ? id : "default");
        return 0;
    }
    if (!strcmp(s, "hydra")) {
        w->kind = MK_HYDRA;
        return hydra_setup(&u, &w->hy, false);
    }
    if (!strcmp(s, "l2eth") || !strcmp(s, "afsk") || !strcmp(s, "audio") ||
        !strcmp(s, "sdr") || !strcmp(s, "janus")) {
        if (strcmp(s, "l2eth") != 0 && !uri_str(&u, "out"))
            return msg(PX_USAGE, "%s as output needs out=<dir>", s);
        return media_unsupported(s);
    }
    return media_unsupported(s);
}

/* Flush (incl. a pending bare frame) and release. 0 ok, -1 I/O error. */
static int wr_close(writer_t *w) {
    int rc = 0;
    if (w->kind == MK_UDP && wr_flush_pending(w) != 0) rc = -1;
    if (w->fp) {
        if (fflush(w->fp) != 0) rc = -1;
        if (w->own_fp && fclose(w->fp) != 0) rc = -1;
        w->fp = NULL;
    }
    if (w->sock >= 0) close(w->sock);
    w->sock = -1;
    return rc;
}

/* ── command-line helpers ─────────────────────────────────────────────────────── */
/* `--key value` or `--key=value`; advances *i past a consumed value. */
static const char *opt_value(int argc, char **argv, int *i, const char *key) {
    size_t kl = strlen(key);
    if (strncmp(argv[*i], key, kl) != 0) return NULL;
    if (argv[*i][kl] == '=') return argv[*i] + kl + 1;
    if (argv[*i][kl] != '\0') return NULL;
    if (*i + 1 >= argc) return NULL;
    return argv[++*i];
}

static bool is_opt(const char *a, const char *key) {
    size_t kl = strlen(key);
    return !strncmp(a, key, kl) && (a[kl] == '\0' || a[kl] == '=');
}

/* ── io ────────────────────────────────────────────────────────────────────────── */
static void print_seconds(FILE *f, double s) {
    char b[48];
    snprintf(b, sizeof b, "%.3f", s);           /* Python round(s, 3) -> json float */
    size_t n = strlen(b);
    while (n > 0 && b[n - 1] == '0' && n >= 2 && b[n - 2] != '.') b[--n] = '\0';
    fputs(b, f);
}

static int cmd_io(int argc, char **argv) {
    g_cmd = "punctim io";
    const char *in = NULL, *out = NULL;
    bool validate = true, stats = false, have_count = false, have_expect = false, have_seconds = false;
    uint64_t count = 0, expect = 0, queue = 256;
    double seconds = 0.0;
    for (int i = 0; i < argc; i++) {
        const char *a = argv[i], *v;
        if (is_opt(a, "--in")) {
            if (!(v = opt_value(argc, argv, &i, "--in"))) return msg(PX_USAGE, "--in wants a URI");
            in = v;
        } else if (is_opt(a, "--out")) {
            if (!(v = opt_value(argc, argv, &i, "--out"))) return msg(PX_USAGE, "--out wants a URI");
            out = v;
        } else if (is_opt(a, "--count")) {
            if (!(v = opt_value(argc, argv, &i, "--count")) || !parse_u64(v, &count))
                return msg(PX_USAGE, "--count wants an integer >= 0");
            have_count = true;
        } else if (is_opt(a, "--expect")) {
            if (!(v = opt_value(argc, argv, &i, "--expect")) || !parse_u64(v, &expect))
                return msg(PX_USAGE, "--expect wants an integer >= 0");
            have_expect = true;
        } else if (is_opt(a, "--queue")) {
            if (!(v = opt_value(argc, argv, &i, "--queue")) || !parse_u64(v, &queue) || queue < 1 ||
                queue > (1u << 24))
                return msg(PX_USAGE, "--queue wants an integer >= 1");
        } else if (is_opt(a, "--seconds")) {
            if (!(v = opt_value(argc, argv, &i, "--seconds")) || !parse_double(v, &seconds))
                return msg(PX_USAGE, "--seconds wants a number");
            have_seconds = true;
        } else if (!strcmp(a, "--no-validate")) {
            validate = false;
        } else if (!strcmp(a, "--stats")) {
            stats = true;
        } else {
            return msg(PX_USAGE, "unrecognized argument: %s", a);
        }
    }
    if (!in || !out) return msg(PX_USAGE, "the following arguments are required: --in, --out");

    double t0 = mono_now();
    static reader_t r;
    static writer_t w;
    io_t io;
    memset(&io, 0, sizeof io);
    int rc = rd_open(&r, in, &io);
    if (rc) { rd_close(&r); return rc; }
    rc = wr_open(&w, out);
    if (rc) { wr_close(&w); rd_close(&r); return rc; }
    io.w = &w;
    io.finite = r.finite;
    io.validate = validate;
    if (have_count) { io.limited = true; io.limit = count; }
    else if (have_expect && !r.finite) { io.limited = true; io.limit = expect; }
    if (fifo_init(&io.q, (size_t)queue) != 0) return msg(PX_IO, "I/O error: out of memory");
    double deadline = have_seconds ? t0 + seconds : 0.0;
    if (w.fp == stdout) setvbuf(stdout, NULL, _IOFBF, 1 << 16);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    int rerr = 0, rerrno = 0;
    if (io.finite) {
        /* synchronous: every frame is handled as it is read; nothing is ever dropped */
        while (!io_done(&io) && !io.werr && !g_stop) {
            double to = 0.05;
            if (have_seconds) {
                double left = deadline - mono_now();
                if (left <= 0.0) break;
                to = dmin(to, left);
            }
            int pr = rd_poll(&r, &io, to, deadline);
            if (pr == -1) break;
            if (pr == -2) { rerr = 1; rerrno = errno; break; }
        }
    } else {
        uint8_t f[DCF_MEDIUM_FRAME_LEN];
        while (!io_done(&io) && !io.werr && !g_stop) {
            double now = mono_now();
            if (have_seconds && now >= deadline) break;
            double to = io.q.n ? 0.0 : 0.05;
            if (w.kind == MK_UDP && w.has_pending) to = dmin(to, w.pending_t + w.flush_s - now);
            if (have_seconds) to = dmin(to, deadline - now);
            int pr = rd_poll(&r, &io, to, deadline);
            if (pr == -2) { rerr = 1; rerrno = errno; break; }
            for (int k = 0; k < 64 && !io_done(&io) && !io.werr && fifo_get(&io.q, f); k++)
                io_handle(&io, f);
            if (!io.q.n && !io.werr && wr_poll(&w) != 0) { io.werr = true; io.werrno = errno; }
        }
        /* frames already received are not dropped */
        while (!io_done(&io) && !io.werr && fifo_get(&io.q, f)) io_handle(&io, f);
    }
    if (wr_close(&w) != 0 && !io.werr) { io.werr = true; io.werrno = errno; }
    uint64_t skipped = (r.kind == MK_FILE || r.kind == MK_STDIO) ? r.sc.skipped : 0;
    uint64_t bad = r.bad_lines, rinvalid = r.invalid;
    rd_close(&r);
    if (rerr) return msg(PX_IO, "I/O error: read: %s", strerror(rerrno));
    if (io.werr) {
        if (io.werrno == EPIPE) return PX_IO;
        return msg(PX_IO, "I/O error: write: %s", strerror(io.werrno));
    }
    if (stats) {
        fputs("{\"in\":", stderr);
        json_str(stderr, in);
        fputs(",\"out\":", stderr);
        json_str(stderr, out);
        fprintf(stderr, ",\"frames_in\":%llu,\"frames_out\":%llu,\"invalid_frames\":%llu,"
                "\"skipped_bytes\":%llu,\"bad_lines\":%llu,\"dropped\":%llu,\"seconds\":",
                (unsigned long long)io.frames_in, (unsigned long long)io.frames_out,
                (unsigned long long)(io.invalid + rinvalid), (unsigned long long)skipped,
                (unsigned long long)bad, (unsigned long long)io.q.dropped);
        print_seconds(stderr, mono_now() - t0);
        fputs("}\n", stderr);
    }
    free(io.q.buf);
    if (have_expect && io.frames_out != expect)
        return msg(PX_EXPECT, "expected %llu frames, wrote %llu", (unsigned long long)expect,
                   (unsigned long long)io.frames_out);
    return PX_OK;
}

/* ── encode ────────────────────────────────────────────────────────────────────── */
static int num_arg(const char *what, const char *s, uint64_t hi, uint64_t *out) {
    if (!parse_u64(s, out)) return msg(PX_USAGE, "%s: '%s' is not an integer (decimal or 0x hex)", what, s);
    if (*out > hi) return msg(PX_USAGE, "%s: %llu out of range 0..%llu", what, (unsigned long long)*out,
                              (unsigned long long)hi);
    return 0;
}

static int cmd_encode(int argc, char **argv) {
    g_cmd = "punctim encode";
    const char *ty = NULL, *sq = NULL, *sr = NULL, *ds = NULL, *pl = NULL, *tx = NULL, *ts = "0";
    for (int i = 0; i < argc; i++) {
        const char *a = argv[i], *v = NULL;
        const char **slot = NULL;
        if (is_opt(a, "--type")) slot = &ty;
        else if (is_opt(a, "--seq")) slot = &sq;
        else if (is_opt(a, "--src")) slot = &sr;
        else if (is_opt(a, "--dst")) slot = &ds;
        else if (is_opt(a, "--payload")) slot = &pl;
        else if (is_opt(a, "--text")) slot = &tx;
        else if (is_opt(a, "--ts")) slot = &ts;
        else return msg(PX_USAGE, "unrecognized argument: %s", a);
        const char *key = a;
        char k[16];
        const char *eq = strchr(a, '=');
        if (eq) { snprintf(k, sizeof k, "%.*s", (int)(eq - a), a); key = k; }
        if (!(v = opt_value(argc, argv, &i, key))) return msg(PX_USAGE, "%s wants a value", key);
        *slot = v;
    }
    if (!ty || !sq || !sr || !ds)
        return msg(PX_USAGE, "the following arguments are required: --type, --seq, --src, --dst");
    if ((pl != NULL) == (tx != NULL))
        return msg(PX_USAGE, "exactly one of --payload HEX8 / --text S is required");
    uint64_t t, seq, src, dst, tsv;
    int rc;
    if ((rc = num_arg("--type", ty, 15, &t)) || (rc = num_arg("--seq", sq, 0xFFFF, &seq)) ||
        (rc = num_arg("--src", sr, 0xFFFF, &src)) || (rc = num_arg("--dst", ds, 0xFFFF, &dst)) ||
        (rc = num_arg("--ts", ts, UINT64_MAX, &tsv)))
        return rc;
    uint8_t payload[4] = {0, 0, 0, 0};
    if (pl) {
        char p[16];
        bool ok = strip_copy(pl, p, sizeof p) && strlen(p) == 8;
        for (int k = 0; ok && k < 4; k++) {
            int hi = dcf__hexval(p[2 * k]), lo = dcf__hexval(p[2 * k + 1]);
            if (hi < 0 || lo < 0) ok = false;
            else payload[k] = (uint8_t)((hi << 4) | lo);
        }
        if (!ok) return msg(PX_USAGE, "--payload wants exactly 8 hex digits (4 bytes)");
    } else {
        size_t n = strlen(tx);
        if (n > 4) return msg(PX_USAGE, "--text is at most 4 UTF-8 bytes (zero-padded)");
        memcpy(payload, tx, n);
    }
    uint8_t f[DCF_MEDIUM_FRAME_LEN];
    f[0] = DCF_SYNC_BYTE;
    f[1] = (uint8_t)(0x10u | (unsigned)t);
    f[2] = (uint8_t)(seq >> 8); f[3] = (uint8_t)seq;
    f[4] = (uint8_t)(src >> 8); f[5] = (uint8_t)src;
    f[6] = (uint8_t)(dst >> 8); f[7] = (uint8_t)dst;
    memcpy(f + 8, payload, 4);
    tsv &= 0xFFFFFFu;                                    /* ts_us is 24-bit on the wire */
    f[12] = (uint8_t)(tsv >> 16); f[13] = (uint8_t)(tsv >> 8); f[14] = (uint8_t)tsv;
    uint16_t crc = dcf_crc16(f, DCF_FRAME_CRC_COVER);
    f[15] = (uint8_t)(crc >> 8);
    f[16] = (uint8_t)crc;
    char line[36];
    dcf_hex_encode_line(f, line);
    fputs(line, stdout);
    return fflush(stdout) == 0 ? PX_OK : PX_IO;
}

/* ── decode (python punctim.decode_record, same keys + order) ─────────────────────── */
static const char *frame_type_name(unsigned t, char *buf, size_t cap) {
    static const char *const N[4] = {"FData", "FAck", "FBeacon", "FCtrl"};
    if (t < 4) return N[t];
    snprintf(buf, cap, "0x%X", t);
    return buf;
}

/* Decode one hex string; prints the record; returns true iff valid. */
static bool decode_one(const char *text, bool json) {
    size_t n = strlen(text);
    const char *s = text;
    while (n && (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\v' || *s == '\f')) { s++; n--; }
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r' || s[n - 1] == '\v' ||
                 s[n - 1] == '\f')) n--;
    char *str = malloc(n + 1);
    if (!str) return false;
    memcpy(str, s, n);
    str[n] = '\0';
    bool hexok = (n % 2 == 0);
    for (size_t i = 0; hexok && i < n; i++) hexok = dcf__hexval(str[i]) >= 0;
    const char *err = NULL;
    char errbuf[48];
    if (!hexok || n != 2 * DCF_MEDIUM_FRAME_LEN) {
        if (!hexok) {
            err = "not hex";
        } else {
            snprintf(errbuf, sizeof errbuf, "length %zu != 17", n / 2);
            err = errbuf;
            for (size_t i = 0; i < n; i++)
                if (str[i] >= 'A' && str[i] <= 'F') str[i] = (char)(str[i] - 'A' + 'a');
        }
        if (json) {
            fputs("{\"hex\":", stdout);
            json_str(stdout, str);
            fputs(",\"valid\":false,\"error\":", stdout);
            json_str(stdout, err);
            fputs("}\n", stdout);
        } else {
            printf("invalid (%s) %s\n", err, str);
        }
        free(str);
        return false;
    }
    uint8_t b[DCF_MEDIUM_FRAME_LEN];
    for (size_t k = 0; k < DCF_MEDIUM_FRAME_LEN; k++)
        b[k] = (uint8_t)((dcf__hexval(str[2 * k]) << 4) | dcf__hexval(str[2 * k + 1]));
    free(str);
    char hex[36];
    dcf_hex_encode_line(b, hex);
    hex[34] = '\0';
    unsigned crc = ((unsigned)b[15] << 8) | b[16];
    unsigned syn = (unsigned)dcf_crc16(b, DCF_FRAME_CRC_COVER) ^ crc;
    if (b[0] != DCF_SYNC_BYTE) err = "bad sync byte";
    else if ((b[1] >> 4) != 1u) err = "bad version nibble";
    else if (syn != 0) err = "CRC mismatch";
    unsigned t = b[1] & 0x0Fu;
    char tn[8];
    const char *tname = frame_type_name(t, tn, sizeof tn);
    unsigned seq = ((unsigned)b[2] << 8) | b[3], src = ((unsigned)b[4] << 8) | b[5];
    unsigned dst = ((unsigned)b[6] << 8) | b[7];
    unsigned long tsu = ((unsigned long)b[12] << 16) | ((unsigned long)b[13] << 8) | b[14];
    char pl[9];
    snprintf(pl, sizeof pl, "%02x%02x%02x%02x", b[8], b[9], b[10], b[11]);
    if (json) {
        printf("{\"hex\":\"%s\",\"valid\":%s,\"syndrome\":%u,\"frame_type\":%u,"
               "\"frame_type_name\":\"%s\",\"seq\":%u,\"src\":%u,\"dst\":%u,\"payload\":\"%s\","
               "\"ts_us\":%lu,\"crc\":%u",
               hex, err ? "false" : "true", syn, t, tname, seq, src, dst, pl, tsu, crc);
        if (err) printf(",\"error\":\"%s\"", err);
        fputs("}\n", stdout);
    } else {
        if (err) printf("invalid (%s)", err);
        else fputs("valid", stdout);
        printf(" type=%u (%s) seq=%u src=%u dst=%u payload=%s ts_us=%lu crc=0x%04x "
               "syndrome=0x%04x\n", t, tname, seq, src, dst, pl, tsu, crc, syn);
    }
    return err == NULL;
}

static int cmd_decode(int argc, char **argv) {
    g_cmd = "punctim decode";
    bool json = false, use_stdin = false;
    const char *hex = NULL;
    for (int i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--json")) json = true;
        else if (!strcmp(argv[i], "--stdin")) use_stdin = true;
        else if (argv[i][0] == '-' && argv[i][1] == '-') return msg(PX_USAGE, "unrecognized argument: %s", argv[i]);
        else if (!hex) hex = argv[i];
        else return msg(PX_USAGE, "unrecognized argument: %s", argv[i]);
    }
    if (use_stdin == (hex != NULL)) return msg(PX_USAGE, "decode wants exactly one of HEX or --stdin");
    int rc = PX_OK;
    if (!use_stdin) {
        if (!decode_one(hex, json)) rc = PX_INVALID;
        return fflush(stdout) == 0 ? rc : PX_IO;
    }
    /* one hex frame per line; blank and '#' lines skipped */
    size_t cap = 256, len = 0;
    char *line = malloc(cap);
    if (!line) return PX_IO;
    int c;
    bool more = true;
    while (more) {
        c = getchar();
        if (c == EOF) more = false;
        if (c == EOF || c == '\n') {
            line[len] = '\0';
            char *s = line;
            while (*s && is_ws((unsigned char)*s)) s++;
            size_t n = strlen(s);
            while (n && is_ws((unsigned char)s[n - 1])) s[--n] = '\0';
            if (n && s[0] != '#' && !decode_one(s, json)) rc = PX_INVALID;
            len = 0;
            continue;
        }
        if (len + 1 >= cap) {
            char *nl = realloc(line, cap * 2);
            if (!nl) { free(line); return PX_IO; }
            line = nl;
            cap *= 2;
        }
        line[len++] = (char)c;
    }
    free(line);
    return fflush(stdout) == 0 ? rc : PX_IO;
}

/* ── certify (the compiled-in codec/medium_vectors.gen.h; same checks as the C cert) ── */
static const char *const FAMILIES[7] = {"stream", "hex", "udp_proto", "udp_bare", "l2eth",
                                        "hydra_symbols", "afsk_bits"};

static int cmd_certify(int argc, char **argv) {
    g_cmd = "punctim certify";
    const char *fams[64];
    int nf = 0;
    for (int i = 0; i < argc; i++) {
        const char *v;
        if (is_opt(argv[i], "--vectors")) {
            if (!(v = opt_value(argc, argv, &i, "--vectors"))) return msg(PX_USAGE, "--vectors wants DIR");
            fprintf(stderr, "%s: note: the C build certifies against the compiled-in "
                    "codec/medium_vectors.gen.h; --vectors %s is not read\n", g_cmd, v);
        } else if (!strcmp(argv[i], "--family")) {
            nf = 0;                                    /* argparse: a later --family wins */
            while (i + 1 < argc && strncmp(argv[i + 1], "--", 2) != 0 && nf < 64) fams[nf++] = argv[++i];
            if (!nf) return msg(PX_USAGE, "--family wants NAME ...");
        } else {
            return msg(PX_USAGE, "unrecognized argument: %s", argv[i]);
        }
    }
    for (int k = 0; k < nf; k++) {
        bool ok = false;
        for (int j = 0; j < 7; j++) ok = ok || !strcmp(fams[k], FAMILIES[j]);
        if (!ok)
            return msg(PX_USAGE, "unknown family '%s' (one of: stream, hex, udp_proto, udp_bare, "
                       "l2eth, hydra_symbols, afsk_bits)", fams[k]);
    }
    if (!nf) {
        for (int j = 0; j < 7; j++) fams[j] = FAMILIES[j];
        nf = 7;
    }
    printf("vectors: codec/medium_vectors.gen.h (compiled in)\n");
    int failed = 0, total = 0;
    for (int k = -1; k < nf; k++) {
        const char *name = (k < 0) ? "anchors" : fams[k];
        int ncases = 0;
        char m[200];
        int bad = dcf_medium_certify_family(name, &ncases, m, sizeof m);
        if (k >= 0) total += ncases;
        if (bad == 0) {
            printf("PASS %s (%d %s)\n", name, ncases, k < 0 ? "basis frames" : "cases");
        } else {
            failed++;
            printf("FAIL %s: %s\n", name, m);
        }
    }
    if (failed) {
        printf("CERTIFICATION FAILED (%d famil%s)\n", failed, failed == 1 ? "y" : "ies");
        fflush(stdout);
        return PX_CERT;
    }
    printf("ALL MEDIUM VECTORS PASS (%d cases, impl %s)\n", total, PUNCTIM_IMPL);
    return fflush(stdout) == 0 ? PX_OK : PX_IO;
}

/* ── version / usage / main ─────────────────────────────────────────────────────── */
static int cmd_version(int argc, char **argv) {
    g_cmd = "punctim version";
    bool json = false;
    for (int i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "--json")) json = true;
        else return msg(PX_USAGE, "unrecognized argument: %s", argv[i]);
    }
    if (json)
        printf("{\"name\":\"punctim\",\"version\":\"%s\",\"impl\":\"%s\",\"media\":[\"file\","
               "\"stdio\",\"hex\",\"udp\",\"loop\",\"hydra\"],\"families\":[\"stream\",\"hex\","
               "\"udp_proto\",\"udp_bare\",\"l2eth\",\"hydra_symbols\",\"afsk_bits\"]}\n",
               PUNCTIM_VERSION, PUNCTIM_IMPL);
    else
        printf("punctim %s (%s)\n", PUNCTIM_VERSION, PUNCTIM_IMPL);
    return fflush(stdout) == 0 ? PX_OK : PX_IO;
}

static void usage(FILE *f) {
    fputs("usage: punctim {version,io,encode,decode,certify} ...\n"
          "\n"
          "DCF medium tool (C): move DeModFrames between any two media, deterministically\n"
          "(Documentation/DCF_MEDIUM_SPEC.md).\n"
          "\n"
          "  punctim version [--json]\n"
          "  punctim io      --in URI --out URI [--count N] [--seconds S] [--expect N]\n"
          "                  [--no-validate] [--stats] [--queue N]\n"
          "  punctim encode  --type T --seq N --src N --dst N (--payload HEX8 | --text S) [--ts N]\n"
          "  punctim decode  (HEX | --stdin) [--json]\n"
          "  punctim certify [--vectors DIR] [--family NAME ...]\n"
          "\n"
          "media (C build): file: stdio: hex: udp: loop: hydra:   (l2eth: afsk: audio: sdr:\n"
          "                 janus: and `sim` are in the Python punctim)\n"
          "exit: 0 ok, 1 I/O, 2 usage, 3 medium unsupported, 4 cert failed, 5 invalid frame,\n"
          "      6 --expect not met\n", f);
}

int main(int argc, char **argv) {
    signal(SIGPIPE, SIG_IGN);
    if (argc < 2) {
        usage(stderr);
        return PX_USAGE;
    }
    const char *cmd = argv[1];
    if (!strcmp(cmd, "-h") || !strcmp(cmd, "--help") || !strcmp(cmd, "help")) {
        usage(stdout);
        return PX_OK;
    }
    int sub_argc = argc - 2;
    char **sub_argv = argv + 2;
    if (!strcmp(cmd, "version")) return cmd_version(sub_argc, sub_argv);
    if (!strcmp(cmd, "io"))      return cmd_io(sub_argc, sub_argv);
    if (!strcmp(cmd, "encode"))  return cmd_encode(sub_argc, sub_argv);
    if (!strcmp(cmd, "decode"))  return cmd_decode(sub_argc, sub_argv);
    if (!strcmp(cmd, "certify")) return cmd_certify(sub_argc, sub_argv);
    if (!strcmp(cmd, "sim"))
        return msg(PX_USAGE, "sim is only in the Python punctim (python3 python/punctim.py sim ...)");
    usage(stderr);
    return msg(PX_USAGE, "unknown command '%s'", cmd);
}
