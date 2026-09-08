/*
 * woolflow port stub — the container's only listener.
 *
 * Render requires a web service to hold an open port: the healthcheck probes
 * "/" and the Free-tier watchdog flags a service with no listener. Hermes
 * itself reaches Telegram over an outbound long-poll and opens nothing, so
 * this stub is what keeps the service green.
 *
 * Why C instead of the python3 one-liner this replaces: a CPython interpreter
 * costs ~10-13 MB RSS just to exist. This binary idles at ~0.5-1 MB. On a
 * 512 MB instance that is ~2% of the whole budget recovered for the agent.
 *
 * Single-threaded on purpose — one poll() loop, no fork, no pthreads, no
 * malloc in the hot path. The same loop doubles as the memory reporter: every
 * WOOLFLOW_MEM_INTERVAL seconds it prints the cgroup's current/limit bytes to
 * stderr, which is where Render's log stream picks it up. That line is the
 * only way to see real RSS on Free (no metrics on that plan), so leave it on.
 *
 * Env:
 *   PORT                    listen port (default 10000)
 *   HOST                    bind address (default 0.0.0.0)
 *   WOOLFLOW_MEM_INTERVAL   seconds between memory lines; 0 disables
 */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

/* Body is "woolflow alive" (14 bytes). The X-Woolflow header lets an external
 * pinger tell "the stub answered" from "Render answered 200 for a service that
 * is spun down", which otherwise look identical. */
static const char RESPONSE[] =
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: text/plain\r\n"
    "Content-Length: 14\r\n"
    "X-Woolflow: alive\r\n"
    "Connection: close\r\n"
    "\r\n"
    "woolflow alive";

/* Read a whole small file into buf. Returns 0 on success. */
static int slurp(const char *path, char *buf, size_t len) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    size_t n = fread(buf, 1, len - 1, f);
    fclose(f);
    if (n == 0) return -1;
    buf[n] = '\0';
    return 0;
}

/* Fill *used and *limit from cgroup v2, falling back to v1. Either may stay
 * at 0 when the kernel exposes neither (non-Linux, or a runtime that hides
 * the cgroupfs) — the caller reports what it got. */
static void read_cgroup_memory(unsigned long long *used, unsigned long long *limit) {
    char buf[64];
    *used = 0;
    *limit = 0;

    if (slurp("/sys/fs/cgroup/memory.current", buf, sizeof buf) == 0)
        *used = strtoull(buf, NULL, 10);
    else if (slurp("/sys/fs/cgroup/memory/memory.usage_in_bytes", buf, sizeof buf) == 0)
        *used = strtoull(buf, NULL, 10);

    if (slurp("/sys/fs/cgroup/memory.max", buf, sizeof buf) == 0) {
        /* cgroup v2 writes the literal "max" when the group is uncapped. */
        if (strncmp(buf, "max", 3) != 0) *limit = strtoull(buf, NULL, 10);
    } else if (slurp("/sys/fs/cgroup/memory/memory.limit_in_bytes", buf, sizeof buf) == 0) {
        unsigned long long v = strtoull(buf, NULL, 10);
        /* v1 encodes "no limit" as a huge sentinel rather than a keyword. */
        if (v < (1ULL << 62)) *limit = v;
    }
}

static void report_memory(void) {
    unsigned long long used = 0, limit = 0;
    read_cgroup_memory(&used, &limit);
    if (used == 0) return;  /* nothing readable here; stay quiet */

    double used_mb = (double)used / (1024.0 * 1024.0);
    if (limit > 0) {
        double limit_mb = (double)limit / (1024.0 * 1024.0);
        fprintf(stderr, "[woolflow] mem used=%.1fMiB limit=%.0fMiB (%.0f%%)\n",
                used_mb, limit_mb, 100.0 * used_mb / limit_mb);
    } else {
        fprintf(stderr, "[woolflow] mem used=%.1fMiB limit=none\n", used_mb);
    }
    fflush(stderr);
}

/* Drain the request line(s) and answer. Both sides get a short timeout so a
 * client that connects and then stalls can't wedge the single-threaded loop
 * and take the healthcheck down with it. */
static void serve(int fd) {
    struct timeval tv = {.tv_sec = 5, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

    char scratch[1024];
    (void)recv(fd, scratch, sizeof scratch, 0);
    (void)send(fd, RESPONSE, sizeof RESPONSE - 1, 0);
    close(fd);
}

int main(void) {
    /* A peer that hangs up mid-write must not kill the process. */
    signal(SIGPIPE, SIG_IGN);

    const char *port_env = getenv("PORT");
    const char *host_env = getenv("HOST");
    const char *iv_env = getenv("WOOLFLOW_MEM_INTERVAL");

    int port = port_env && *port_env ? atoi(port_env) : 10000;
    if (port <= 0 || port > 65535) port = 10000;
    long interval = iv_env && *iv_env ? strtol(iv_env, NULL, 10) : 60;
    if (interval < 0) interval = 0;

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("[woolflow] socket");
        return 1;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (host_env && *host_env && strcmp(host_env, "0.0.0.0") != 0)
        inet_pton(AF_INET, host_env, &addr.sin_addr);

    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        perror("[woolflow] bind");
        return 1;
    }
    if (listen(fd, 64) < 0) {
        perror("[woolflow] listen");
        return 1;
    }
    fprintf(stderr, "[woolflow] port stub listening on %s:%d\n",
            host_env && *host_env ? host_env : "0.0.0.0", port);
    fflush(stderr);

    report_memory();

    struct pollfd pfd = {.fd = fd, .events = POLLIN};
    for (;;) {
        /* poll() doubles as the report timer: it wakes either because a
         * client arrived or because the interval elapsed. -1 = block forever
         * when reporting is switched off. */
        int timeout_ms = interval > 0 ? (int)(interval * 1000) : -1;
        int rc = poll(&pfd, 1, timeout_ms);
        if (rc < 0) {
            if (errno == EINTR) continue;
            perror("[woolflow] poll");
            return 1;
        }
        if (rc == 0) {
            report_memory();
            continue;
        }
        int client = accept(fd, NULL, NULL);
        if (client < 0) {
            if (errno == EINTR || errno == ECONNABORTED) continue;
            perror("[woolflow] accept");
            continue;
        }
        serve(client);
    }
}
