/*
 * yigitzra1n_givemeroot — Root privilege bridge for Sileo on nosuid iOS 16 RootFS.
 * Verifies parent process is /Applications/Sileo.app/Sileo, then delegates execution
 * (including stdin/stdout/stderr via SCM_RIGHTS) to ios16_sshd_launcher's root daemon.
 */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

extern int proc_pidpath(int pid, void *buffer, uint32_t buffersize);
extern char **environ;

#define GMR_SOCK_PATH "/var/run/yigitzra1n_gmr.sock"
#define GMR_MAX_ARGS 256
#define GMR_MAX_ENV  256
#define GMR_MAX_FDS  32

static const char GMR_MARKER[] __attribute__((used)) = "yigitzra1n_gmr_v2";

static int write_all(int fd, const void *buf, size_t len) {
    const uint8_t *p = (const uint8_t *)buf;
    size_t off = 0;
    while (off < len) {
        ssize_t w = write(fd, p + off, len - off);
        if (w < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (w == 0) return -1;
        off += (size_t)w;
    }
    return 0;
}

static int read_all(int fd, void *buf, size_t len) {
    uint8_t *p = (uint8_t *)buf;
    size_t off = 0;
    while (off < len) {
        ssize_t r = read(fd, p + off, len - off);
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (r == 0) return -1;
        off += (size_t)r;
    }
    return 0;
}

static int send_str_vec(int sock, char *const vec[], uint32_t count) {
    if (write_all(sock, &count, sizeof(count)) != 0) return -1;
    for (uint32_t i = 0; i < count; i++) {
        const char *s = vec[i] ? vec[i] : "";
        uint32_t slen = (uint32_t)strlen(s);
        if (write_all(sock, &slen, sizeof(slen)) != 0) return -1;
        if (slen > 0 && write_all(sock, s, slen) != 0) return -1;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (getenv("YIGITZRA1N_GMR_VER") != NULL) {
        puts(GMR_MARKER);
        return 0;
    }

    char *sileo_real = realpath("/Applications/Sileo.app/Sileo", NULL);
    if (!sileo_real) {
        fprintf(stderr, "Error: failed to retrieve Sileo app path\n");
        return 72;
    }

    char parent_path[PATH_MAX * 2];
    memset(parent_path, 0, sizeof(parent_path));
    if (proc_pidpath((int)getppid(), parent_path, (uint32_t)sizeof(parent_path)) <= 0) {
        free(sileo_real);
        fprintf(stderr, "Error: failed to retrieve parent process path\n");
        return 71;
    }

    if (strcmp(parent_path, sileo_real) != 0) {
        free(sileo_real);
        fprintf(stderr, "Error: permission denied, parent process is not Sileo\n");
        return 77;
    }
    free(sileo_real);

    if (argc < 2) {
        return 0;
    }

    if (strcmp(argv[1], "whoami") == 0) {
        puts("root");
        return 0;
    }

    if (geteuid() == 0) {
        execvp(argv[1], &argv[1]);
        fprintf(stderr, "Error: failed to exec specified task\n");
        return 71;
    }

    int sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock < 0) {
        fprintf(stderr, "Error: failed to create yigitzra1n root helper socket\n");
        return 71;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", GMR_SOCK_PATH);

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(sock);
        /* Fallback attempt in case suid is ever enabled */
        if (setuid(0) == 0 && setgid(0) == 0 && getuid() == 0) {
            execvp(argv[1], &argv[1]);
        }
        fprintf(stderr, "Error: failed to connect to yigitzra1n_gmr root helper (%s)\n", strerror(errno));
        return 71;
    }

    /* Forward stdio (0..2) plus any inheritable pipes/FDs (e.g. APT::Status-Fd=5, Keep-Fds=6) */
    int pass_fds[GMR_MAX_FDS];
    int32_t pass_nums[GMR_MAX_FDS];
    uint32_t fd_count = 0;
    for (int fd = 0; fd < GMR_MAX_FDS; fd++) {
        if (fd == sock) continue;
        int flags = fcntl(fd, F_GETFD);
        if (flags == -1) continue;
        if (fd > STDERR_FILENO && (flags & FD_CLOEXEC) != 0) continue;
        pass_fds[fd_count] = fd;
        pass_nums[fd_count] = (int32_t)fd;
        fd_count++;
    }

    struct iovec iov = { .iov_base = &fd_count, .iov_len = sizeof(fd_count) };
    union {
        struct cmsghdr hdr;
        char buf[CMSG_SPACE(sizeof(int) * GMR_MAX_FDS)];
    } cmsg_u;
    memset(&cmsg_u, 0, sizeof(cmsg_u));

    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    if (fd_count > 0) {
        msg.msg_control = cmsg_u.buf;
        msg.msg_controllen = (socklen_t)CMSG_SPACE(sizeof(int) * fd_count);
        struct cmsghdr *cm = CMSG_FIRSTHDR(&msg);
        cm->cmsg_level = SOL_SOCKET;
        cm->cmsg_type = SCM_RIGHTS;
        cm->cmsg_len = (socklen_t)CMSG_LEN(sizeof(int) * fd_count);
        memcpy(CMSG_DATA(cm), pass_fds, sizeof(int) * fd_count);
    }

    if (sendmsg(sock, &msg, 0) < 0) {
        close(sock);
        fprintf(stderr, "Error: failed to send file descriptors\n");
        return 71;
    }

    if (fd_count > 0 && write_all(sock, pass_nums, sizeof(int32_t) * fd_count) != 0) {
        close(sock);
        fprintf(stderr, "Error: failed to send fd numbers\n");
        return 71;
    }

    uint32_t send_argc = (uint32_t)(argc - 1);
    if (send_argc > GMR_MAX_ARGS) send_argc = GMR_MAX_ARGS;
    if (send_str_vec(sock, &argv[1], send_argc) != 0) {
        close(sock);
        fprintf(stderr, "Error: failed to send command arguments\n");
        return 71;
    }

    uint32_t envc = 0;
    if (environ) {
        while (environ[envc] != NULL && envc < GMR_MAX_ENV) envc++;
    }
    if (send_str_vec(sock, environ, envc) != 0) {
        close(sock);
        fprintf(stderr, "Error: failed to send environment\n");
        return 71;
    }

    int32_t rc = 71;
    if (read_all(sock, &rc, sizeof(rc)) != 0) {
        rc = 71;
    }
    close(sock);
    return (int)rc;
}
