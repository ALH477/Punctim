// SPDX-License-Identifier: LGPL-3.0-only
/**
 * @file dcf_error.c
 * @brief Production Error Handling and Logging Implementation
 * @version 5.2.0
 */

#include "dcf_error.h"
#include "dcf_platform.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>

#ifdef DCF_PLATFORM_POSIX
    #include <signal.h>
    #include <execinfo.h>
    #include <unistd.h>
    #include <syslog.h>
    #include <fcntl.h>
    #include <strings.h>
    #define dcf_strcasecmp strcasecmp
    #define dcf_isatty      isatty
#elif defined(DCF_PLATFORM_WINDOWS)
    #include <io.h>
    #include <fcntl.h>
    #define dcf_strcasecmp _stricmp
    #define dcf_isatty      _isatty
#endif

/* ============================================================================
 * Thread-Local Error Context
 * ============================================================================ */

static DCF_THREAD_LOCAL DCFErrorContext tls_error_ctx;
static DCF_THREAD_LOCAL bool tls_has_error = false;

/* ============================================================================
 * Logger State
 * ============================================================================ */

static struct {
    DCFLoggerConfig config;
    FILE* log_file;
    dcf_mutex_t mutex;
    dcf_atomic_bool initialized;
    DCFLogStats stats;
    dcf_atomic_int current_level;
} g_logger;

/* Both logger locks are created exactly once and never destroyed: a thread
 * can always be about to log, so destroying the mutex (as the old shutdown
 * did) raced every late logger. Init/shutdown transitions serialize on
 * g_log_init_lock; the first initializer wins, later ones see ALREADY. */
static dcf_once_t g_log_once = DCF_ONCE_INIT;
static dcf_mutex_t g_log_init_lock;

static void log_locks_once(void) {
    dcf_mutex_init(&g_log_init_lock);
    dcf_mutex_init(&g_logger.mutex);
}

/* ANSI color codes */
static const char* const log_colors[] = {
    [DCF_LOG_TRACE] = "\033[90m",      /* Gray */
    [DCF_LOG_DEBUG] = "\033[36m",      /* Cyan */
    [DCF_LOG_INFO]  = "\033[32m",      /* Green */
    [DCF_LOG_WARN]  = "\033[33m",      /* Yellow */
    [DCF_LOG_ERROR] = "\033[31m",      /* Red */
    [DCF_LOG_FATAL] = "\033[35;1m",    /* Bold Magenta */
};
static const char* const color_reset = "\033[0m";

static const char* const log_level_names[] = {
    [DCF_LOG_TRACE] = "TRACE",
    [DCF_LOG_DEBUG] = "DEBUG",
    [DCF_LOG_INFO]  = "INFO ",
    [DCF_LOG_WARN]  = "WARN ",
    [DCF_LOG_ERROR] = "ERROR",
    [DCF_LOG_FATAL] = "FATAL",
    [DCF_LOG_OFF]   = "OFF  ",
};

/* ============================================================================
 * Error Functions
 * ============================================================================ */

const char* dcf_error_str(DCFError err) {
    switch (err) {
        case DCF_SUCCESS: return "Success";
        case DCF_PENDING: return "Operation pending";
        case DCF_WOULD_BLOCK: return "Would block";
        case DCF_CANCELLED: return "Operation cancelled";
        case DCF_EOF: return "End of stream";
        
        case DCF_ERR_NULL_PTR: return "Null pointer";
        case DCF_ERR_MALLOC_FAIL: return "Allocation failed";
        case DCF_ERR_OUT_OF_MEMORY: return "Out of memory";
        case DCF_ERR_BUFFER_TOO_SMALL: return "Buffer too small";
        
        case DCF_ERR_CONFIG_NOT_FOUND: return "Config not found";
        case DCF_ERR_CONFIG_INVALID: return "Invalid config";
        case DCF_ERR_CONFIG_PARSE_FAIL: return "Config parse failed";
        
        case DCF_ERR_NETWORK_FAIL: return "Network failure";
        case DCF_ERR_CONNECTION_REFUSED: return "Connection refused";
        case DCF_ERR_CONNECTION_TIMEOUT: return "Connection timeout";
        case DCF_ERR_PEER_UNREACHABLE: return "Peer unreachable";
        case DCF_ERR_SOCKET_CLOSED: return "Socket closed";
        
        case DCF_ERR_INVALID_MESSAGE: return "Invalid message";
        case DCF_ERR_MESSAGE_TOO_LARGE: return "Message too large";
        case DCF_ERR_CRC_MISMATCH: return "CRC mismatch";
        
        case DCF_ERR_PLUGIN_NOT_FOUND: return "Plugin not found";
        case DCF_ERR_PLUGIN_INIT_FAIL: return "Plugin init failed";
        
        case DCF_ERR_INVALID_STATE: return "Invalid state";
        case DCF_ERR_NOT_INITIALIZED: return "Not initialized";
        case DCF_ERR_ALREADY_RUNNING: return "Already running";
        case DCF_ERR_SHUTDOWN_IN_PROGRESS: return "Shutdown in progress";
        case DCF_ERR_CIRCUIT_OPEN: return "Circuit breaker open";
        
        case DCF_ERR_INVALID_ARG: return "Invalid argument";
        case DCF_ERR_TIMEOUT: return "Timeout";
        case DCF_ERR_QUEUE_FULL: return "Queue full";
        case DCF_ERR_RATE_LIMITED: return "Rate limited";
        
        default: return "Unknown error";
    }
}

const char* dcf_error_category_str(DCFError err) {
    static const char* cats[] = {
        "Success", "Memory", "Config", "Network", "Serial",
        "Plugin", "State", "Security", "Argument", "IO",
        "Timeout", "Resource", "Protocol"
    };
    uint8_t cat = DCF_ERROR_CATEGORY(err);
    return (cat < 13) ? cats[cat] : "Unknown";
}

bool dcf_error_is_category(DCFError err, uint8_t category) {
    return DCF_ERROR_CATEGORY(err) == category;
}

bool dcf_error_is_retriable(DCFError err) {
    return err == DCF_WOULD_BLOCK || err == DCF_ERR_TIMEOUT ||
           err == DCF_ERR_CONNECTION_TIMEOUT || err == DCF_ERR_QUEUE_FULL ||
           err == DCF_ERR_RATE_LIMITED || err == DCF_ERR_CIRCUIT_OPEN;
}

bool dcf_error_is_transient(DCFError err) {
    return err == DCF_WOULD_BLOCK || err == DCF_ERR_QUEUE_FULL ||
           err == DCF_ERR_RATE_LIMITED;
}

DCFError dcf_error_from_errno(int err_no) {
    switch (err_no) {
        case 0: return DCF_SUCCESS;
        case ENOMEM: return DCF_ERR_OUT_OF_MEMORY;
        case ENOENT: return DCF_ERR_FILE_NOT_FOUND;
        case EEXIST: return DCF_ERR_FILE_EXISTS;
        case EACCES: case EPERM: return DCF_ERR_PERMISSION_DENIED;
        case EINVAL: return DCF_ERR_INVALID_ARG;
        case ETIMEDOUT: return DCF_ERR_TIMEOUT;
        case ECONNREFUSED: return DCF_ERR_CONNECTION_REFUSED;
        case ECONNRESET: return DCF_ERR_NETWORK_RESET;
        case EAGAIN: return DCF_WOULD_BLOCK;
        case EMFILE: case ENFILE: return DCF_ERR_TOO_MANY_OPEN_FILES;
        case ENOSPC: return DCF_ERR_DISK_FULL;
        case EPIPE: return DCF_ERR_PIPE_BROKEN;
        default: return DCF_ERR_UNKNOWN;
    }
}

void dcf_set_error_v(DCFError code, const char* file, int line,
                     const char* func, const char* fmt, va_list args) {
    tls_error_ctx.code = code;
    tls_error_ctx.line = line;
    tls_error_ctx.os_errno = errno;
    tls_error_ctx.timestamp = dcf_time_realtime_us();
    tls_error_ctx.thread_id = dcf_thread_self();
    tls_error_ctx.chain_depth = 0;
    
    if (file) {
        const char* basename = strrchr(file, '/');
        DCF_SAFE_STRCPY(tls_error_ctx.file, basename ? basename + 1 : file,
                        sizeof(tls_error_ctx.file));
    }
    if (func) {
        DCF_SAFE_STRCPY(tls_error_ctx.function, func, sizeof(tls_error_ctx.function));
    }
    
    if (fmt) {
        vsnprintf(tls_error_ctx.message, sizeof(tls_error_ctx.message), fmt, args);
    } else {
        DCF_SAFE_STRCPY(tls_error_ctx.message, dcf_error_str(code),
                        sizeof(tls_error_ctx.message));
    }
    
    /* Capture stack trace */
#ifdef DCF_PLATFORM_POSIX
    void* frames[DCF_MAX_STACK_FRAMES];
    int n = backtrace(frames, DCF_MAX_STACK_FRAMES);
    tls_error_ctx.stack_depth = 0;
    for (int i = 2; i < n && tls_error_ctx.stack_depth < DCF_MAX_STACK_FRAMES; i++) {
        tls_error_ctx.stack[tls_error_ctx.stack_depth].address = frames[i];
        tls_error_ctx.stack[tls_error_ctx.stack_depth].function = NULL;
        tls_error_ctx.stack[tls_error_ctx.stack_depth].file = NULL;
        tls_error_ctx.stack[tls_error_ctx.stack_depth].line = 0;
        tls_error_ctx.stack_depth++;
    }
#endif
    
    tls_has_error = true;
}

void dcf_set_error(DCFError code, const char* file, int line,
                   const char* func, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    dcf_set_error_v(code, file, line, func, fmt, args);
    va_end(args);
}

void dcf_wrap_error(DCFError new_code, const char* file, int line,
                    const char* func, const char* fmt, ...) {
    int saved_chain_depth = 0;
    
    if (tls_has_error && tls_error_ctx.chain_depth < DCF_MAX_ERROR_CHAIN) {
        int idx = tls_error_ctx.chain_depth;
        tls_error_ctx.chain[idx].code = tls_error_ctx.code;
        DCF_SAFE_STRCPY(tls_error_ctx.chain[idx].message, tls_error_ctx.message,
                        sizeof(tls_error_ctx.chain[idx].message));
        saved_chain_depth = idx + 1;
    }
    
    va_list args;
    va_start(args, fmt);
    dcf_set_error_v(new_code, file, line, func, fmt, args);
    va_end(args);
    
    /* Restore chain depth after set_error_v resets it */
    tls_error_ctx.chain_depth = saved_chain_depth;
}

const DCFErrorContext* dcf_get_last_error(void) {
    return tls_has_error ? &tls_error_ctx : NULL;
}

void dcf_clear_error(void) {
    tls_has_error = false;
    memset(&tls_error_ctx, 0, sizeof(tls_error_ctx));
}

bool dcf_has_error(void) {
    return tls_has_error;
}

int dcf_error_format(const DCFErrorContext* ctx, char* buf, size_t size) {
    if (!ctx || !buf || size == 0) return -1;
    
    int written = snprintf(buf, size,
        "Error 0x%04X (%s): %s\n"
        "  at %s:%d in %s()\n",
        ctx->code, dcf_error_category_str(ctx->code), ctx->message,
        ctx->file, ctx->line, ctx->function);
    
    /* Add error chain if present */
    for (int i = 0; i < ctx->chain_depth && written < (int)size; i++) {
        written += snprintf(buf + written, size - written,
            "  caused by: 0x%04X: %s\n",
            ctx->chain[i].code, ctx->chain[i].message);
    }
    
    return written;
}

int dcf_capture_stack_trace(DCFStackFrame* frames, int max_frames, int skip) {
#ifdef DCF_PLATFORM_POSIX
    void* addrs[64];
    int n = backtrace(addrs, DCF_MIN(64, max_frames + skip));
    int captured = 0;
    for (int i = skip; i < n && captured < max_frames; i++) {
        frames[captured].address = addrs[i];
        frames[captured].function = NULL;
        frames[captured].file = NULL;
        frames[captured].line = 0;
        captured++;
    }
    return captured;
#else
    (void)frames; (void)max_frames; (void)skip;
    return 0;
#endif
}

/* ============================================================================
 * Logging Implementation
 * ============================================================================ */

DCFError dcf_log_init(const DCFLoggerConfig* config) {
    dcf_once(&g_log_once, log_locks_once);
    dcf_mutex_lock(&g_log_init_lock);
    if (dcf_atomic_load(&g_logger.initialized)) {
        dcf_mutex_unlock(&g_log_init_lock);
        return DCF_ERR_ALREADY_INITIALIZED;
    }

    if (config) {
        memcpy(&g_logger.config, config, sizeof(DCFLoggerConfig));
    } else {
        DCFLoggerConfig defaults = DCF_LOGGER_CONFIG_DEFAULT;
        memcpy(&g_logger.config, &defaults, sizeof(DCFLoggerConfig));
    }

    dcf_atomic_store(&g_logger.current_level, g_logger.config.min_level);

    /* Open log file if configured */
    if (g_logger.config.log_to_file && g_logger.config.log_file_path[0]) {
        g_logger.log_file = fopen(g_logger.config.log_file_path, "a");
        if (!g_logger.log_file) {
            dcf_mutex_unlock(&g_log_init_lock);
            return DCF_ERR_IO_FAIL;
        }
    }

#ifdef DCF_PLATFORM_POSIX
    if (g_logger.config.log_to_syslog) {
        openlog("dcf", LOG_PID | LOG_NDELAY, LOG_USER);
    }
#endif

    dcf_atomic_store(&g_logger.initialized, true);
    dcf_mutex_unlock(&g_log_init_lock);
    return DCF_SUCCESS;
}

void dcf_log_shutdown(void) {
    dcf_once(&g_log_once, log_locks_once);
    dcf_mutex_lock(&g_log_init_lock);
    if (!dcf_atomic_load(&g_logger.initialized)) {
        dcf_mutex_unlock(&g_log_init_lock);
        return;
    }

    dcf_log_flush();

    dcf_mutex_lock(&g_logger.mutex);

    if (g_logger.log_file) {
        fclose(g_logger.log_file);
        g_logger.log_file = NULL;
    }

#ifdef DCF_PLATFORM_POSIX
    if (g_logger.config.log_to_syslog) {
        closelog();
    }
#endif

    dcf_mutex_unlock(&g_logger.mutex);
    dcf_atomic_store(&g_logger.initialized, false);
    dcf_mutex_unlock(&g_log_init_lock);
    /* g_logger.mutex is intentionally left alive; see log_locks_once. */
}

void dcf_log_flush(void) {
    if (!dcf_atomic_load(&g_logger.initialized)) return;
    
    dcf_mutex_lock(&g_logger.mutex);
    if (g_logger.log_file) fflush(g_logger.log_file);
    fflush(stderr);
    dcf_mutex_unlock(&g_logger.mutex);
}

void dcf_log_set_level(DCFLogLevel level) {
    dcf_atomic_store(&g_logger.current_level, level);
}

DCFLogLevel dcf_log_get_level(void) {
    return (DCFLogLevel)dcf_atomic_load(&g_logger.current_level);
}

DCFLogLevel dcf_log_level_from_string(const char* str) {
    if (!str) return DCF_LOG_INFO;
    if (dcf_strcasecmp(str, "trace") == 0) return DCF_LOG_TRACE;
    if (dcf_strcasecmp(str, "debug") == 0) return DCF_LOG_DEBUG;
    if (dcf_strcasecmp(str, "info") == 0) return DCF_LOG_INFO;
    if (dcf_strcasecmp(str, "warn") == 0 || dcf_strcasecmp(str, "warning") == 0) return DCF_LOG_WARN;
    if (dcf_strcasecmp(str, "error") == 0) return DCF_LOG_ERROR;
    if (dcf_strcasecmp(str, "fatal") == 0) return DCF_LOG_FATAL;
    if (dcf_strcasecmp(str, "off") == 0) return DCF_LOG_OFF;
    return DCF_LOG_INFO;
}

const char* dcf_log_level_str(DCFLogLevel level) {
    if (level >= DCF_LOG_OFF) return "OFF";
    return log_level_names[level];
}

bool dcf_log_is_enabled(DCFLogLevel level) {
    return level >= (DCFLogLevel)dcf_atomic_load(&g_logger.current_level);
}

void dcf_log_write_v(DCFLogLevel level, const char* file, int line,
                     const char* func, const char* fmt, va_list args) {
    if (!dcf_atomic_load(&g_logger.initialized)) {
        /* Thread-safe lazy default: dcf_log_init serializes internally, so
         * two first-loggers can race here and exactly one initializes. */
        dcf_log_init(&(DCFLoggerConfig){.min_level = DCF_LOG_WARN});
        if (!dcf_atomic_load(&g_logger.initialized)) return;
    }
    if (!dcf_log_is_enabled(level)) return;
    
    /* Format timestamp */
    char timestamp[64] = "";
    if (g_logger.config.include_timestamp) {
        time_t now = time(NULL);
        struct tm* tm_info = localtime(&now);
        strftime(timestamp, sizeof(timestamp), 
                 g_logger.config.timestamp_format[0] ? g_logger.config.timestamp_format : "%Y-%m-%d %H:%M:%S",
                 tm_info);
    }
    
    /* Format message */
    char message[4096];
    vsnprintf(message, sizeof(message), fmt, args);
    
    /* Get source location */
    const char* basename = file ? strrchr(file, '/') : NULL;
    basename = basename ? basename + 1 : (file ? file : "unknown");
    
    /* Build log line */
    char log_line[8192];
    int len = 0;
    
    if (g_logger.config.include_timestamp) {
        len += snprintf(log_line + len, sizeof(log_line) - len, "%s ", timestamp);
    }
    if (g_logger.config.include_level) {
        len += snprintf(log_line + len, sizeof(log_line) - len, "[%s] ", log_level_names[level]);
    }
    if (g_logger.config.include_source_location) {
        len += snprintf(log_line + len, sizeof(log_line) - len, "%s:%d ", basename, line);
    }
    if (g_logger.config.include_function) {
        len += snprintf(log_line + len, sizeof(log_line) - len, "%s(): ", func ? func : "?");
    }
    len += snprintf(log_line + len, sizeof(log_line) - len, "%s", message);
    
    /* Output to destinations */
    dcf_mutex_lock(&g_logger.mutex);
    
    /* DCFLogStats fields are plain uint64_t (public struct); they are updated
     * under g_logger.mutex, so no atomic op is needed (clang rejects C11
     * atomic_fetch_add on non-_Atomic objects). */
    g_logger.stats.messages_logged[level] += 1;
    g_logger.stats.bytes_written += (uint64_t)len;
    
    /* stderr */
    if (g_logger.config.log_to_stderr) {
        if (g_logger.config.use_colors && dcf_isatty(fileno(stderr))) {
            fprintf(stderr, "%s%s%s\n", log_colors[level], log_line, color_reset);
        } else {
            fprintf(stderr, "%s\n", log_line);
        }
    }
    
    /* File */
    if (g_logger.log_file && level >= g_logger.config.min_file_level) {
        fprintf(g_logger.log_file, "%s\n", log_line);
        
        /* Check rotation */
        long pos = ftell(g_logger.log_file);
        if (pos > 0 && (size_t)pos > g_logger.config.max_file_size) {
            dcf_log_rotate();
        }
    }
    
#ifdef DCF_PLATFORM_POSIX
    /* Syslog */
    if (g_logger.config.log_to_syslog) {
        int priority = LOG_INFO;
        switch (level) {
            case DCF_LOG_TRACE:
            case DCF_LOG_DEBUG: priority = LOG_DEBUG; break;
            case DCF_LOG_INFO:  priority = LOG_INFO; break;
            case DCF_LOG_WARN:  priority = LOG_WARNING; break;
            case DCF_LOG_ERROR: priority = LOG_ERR; break;
            case DCF_LOG_FATAL: priority = LOG_CRIT; break;
            default: break;
        }
        syslog(priority, "%s", message);
    }
#endif
    
    /* Custom callback */
    if (g_logger.config.callback) {
        g_logger.config.callback(level, file, line, func, message,
                                  g_logger.config.callback_user_data);
    }
    
    dcf_mutex_unlock(&g_logger.mutex);
    
    /* Fatal errors should flush immediately */
    if (level == DCF_LOG_FATAL) {
        dcf_log_flush();
    }
}

void dcf_log_write(DCFLogLevel level, const char* file, int line,
                   const char* func, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    dcf_log_write_v(level, file, line, func, fmt, args);
    va_end(args);
}

DCFError dcf_log_set_file(const char* path) {
    dcf_mutex_lock(&g_logger.mutex);
    
    if (g_logger.log_file) {
        fclose(g_logger.log_file);
    }
    
    g_logger.log_file = fopen(path, "a");
    if (!g_logger.log_file) {
        dcf_mutex_unlock(&g_logger.mutex);
        return DCF_ERR_IO_FAIL;
    }
    
    DCF_SAFE_STRCPY(g_logger.config.log_file_path, path,
                    sizeof(g_logger.config.log_file_path));
    
    dcf_mutex_unlock(&g_logger.mutex);
    return DCF_SUCCESS;
}

DCFError dcf_log_rotate(void) {
    if (!g_logger.log_file) return DCF_SUCCESS;
    
    fclose(g_logger.log_file);
    
    /* Rotate existing files */
    char old_path[DCF_MAX_PATH_LEN];
    char new_path[DCF_MAX_PATH_LEN];
    
    for (int i = g_logger.config.max_file_count - 1; i >= 0; i--) {
        if (i == 0) {
            snprintf(old_path, sizeof(old_path), "%s", g_logger.config.log_file_path);
        } else {
            snprintf(old_path, sizeof(old_path), "%s.%d", g_logger.config.log_file_path, i);
        }
        snprintf(new_path, sizeof(new_path), "%s.%d", g_logger.config.log_file_path, i + 1);
        rename(old_path, new_path);
    }
    
    g_logger.log_file = fopen(g_logger.config.log_file_path, "w");
    g_logger.stats.rotations += 1;  /* plain uint64_t; see dcf_log_write */
    
    return g_logger.log_file ? DCF_SUCCESS : DCF_ERR_IO_FAIL;
}

void dcf_log_get_stats(DCFLogStats* stats) {
    if (stats) memcpy(stats, &g_logger.stats, sizeof(DCFLogStats));
}

/* ============================================================================
 * Crash Handler
 * ============================================================================ */

static DCFCrashCallback g_crash_callback = NULL;
static void* g_crash_user_data = NULL;
static char g_crash_log_path[DCF_MAX_PATH_LEN] = "";
static int g_crash_log_fd = -1;

#ifdef DCF_PLATFORM_POSIX
static struct sigaction g_old_handlers[32];
static const int crash_signals[] = { SIGSEGV, SIGABRT, SIGFPE, SIGILL, SIGBUS };

/* write() everything, tolerating short writes and EINTR; signal-safe. */
static void crash_write(int fd, const char* buf, size_t n) {
    while (n > 0) {
        ssize_t w = write(fd, buf, n);
        if (w <= 0) {
            if (w < 0 && errno == EINTR) continue;
            return;
        }
        buf += w;
        n -= (size_t)w;
    }
}

static void crash_handler(int sig) {
    /* Async-signal-safe: only use write(), backtrace_symbols_fd(), and
     * signal-safe functions. No fopen/fprintf/malloc/free/ctime/strsignal
     * inside a signal handler — those are UB per POSIX. */
    static const char crash_prefix[] = "\n=== CRASH: signal ";
    static const char stack_prefix[] = "Stack trace:\n";
    char sigbuf[8];
    int len = 0;
    int tmp = sig;
    if (tmp < 0) { sigbuf[len++] = '-'; tmp = -tmp; }
    do { sigbuf[len++] = '0' + (tmp % 10); tmp /= 10; } while (tmp);
    /* reverse digits in place */
    for (int i = 0; i < len / 2; i++) { char c = sigbuf[i]; sigbuf[i] = sigbuf[len-1-i]; sigbuf[len-1-i] = c; }
    sigbuf[len] = '\n';

    /* Try to write to a pre-opened crash log fd; fall back to stderr */
    int fd = STDERR_FILENO;
    /* g_crash_log_fd is set by dcf_crash_handler_set_log_file (or remains -1) */
    if (g_crash_log_fd >= 0) fd = g_crash_log_fd;

    crash_write(fd, crash_prefix, sizeof(crash_prefix) - 1);
    crash_write(fd, sigbuf, (size_t)len + 1);

    crash_write(fd, stack_prefix, sizeof(stack_prefix) - 1);
    void* frames[64];
    int n = backtrace(frames, 64);
    backtrace_symbols_fd(frames, n, fd);

    /* Call user callback (MUST be async-signal-safe if set) */
    if (g_crash_callback) {
        g_crash_callback(sig, g_crash_user_data);
    }

    /* Restore default handler and re-raise */
    signal(sig, SIG_DFL);
    raise(sig);
}
#endif

DCFError dcf_crash_handler_install(void) {
#ifdef DCF_PLATFORM_POSIX
    /* Pre-warm backtrace(): its first call may dlopen/malloc (libgcc lazy
     * init), which is not signal-safe — take that hit here, outside any
     * signal context, so the in-handler call is a plain walk. */
    void* warm[4];
    (void)backtrace(warm, 4);

    for (size_t i = 0; i < sizeof(crash_signals) / sizeof(crash_signals[0]); i++) {
        int signo = crash_signals[i];
        if (signo < 0 || (size_t)signo >= sizeof(g_old_handlers) / sizeof(g_old_handlers[0]))
            continue; /* keep the saved-handler table in bounds */
        struct sigaction sa;
        sa.sa_handler = crash_handler;
        sigemptyset(&sa.sa_mask);
        sa.sa_flags = SA_RESETHAND;
        if (sigaction(signo, &sa, &g_old_handlers[signo]) != 0)
            return DCF_ERR_IO_FAIL;
    }
    return DCF_SUCCESS;
#else
    return DCF_ERR_NOT_INITIALIZED;
#endif
}

void dcf_crash_handler_uninstall(void) {
#ifdef DCF_PLATFORM_POSIX
    for (size_t i = 0; i < sizeof(crash_signals) / sizeof(crash_signals[0]); i++) {
        int signo = crash_signals[i];
        if (signo < 0 || (size_t)signo >= sizeof(g_old_handlers) / sizeof(g_old_handlers[0]))
            continue;
        sigaction(signo, &g_old_handlers[signo], NULL);
    }
#endif
}

void dcf_crash_handler_set_callback(DCFCrashCallback callback, void* user_data) {
    g_crash_callback = callback;
    g_crash_user_data = user_data;
}

void dcf_crash_handler_set_log_file(const char* path) {
    if (g_crash_log_fd >= 0) {
        close(g_crash_log_fd);
        g_crash_log_fd = -1;
    }
    if (path) {
        DCF_SAFE_STRCPY(g_crash_log_path, path, sizeof(g_crash_log_path));
#ifdef DCF_PLATFORM_POSIX
        g_crash_log_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
#elif defined(DCF_PLATFORM_WINDOWS)
        g_crash_log_fd = _open(path, _O_WRONLY | _O_CREAT | _O_APPEND, 0644);
#endif
    } else {
        g_crash_log_path[0] = '\0';
    }
}
