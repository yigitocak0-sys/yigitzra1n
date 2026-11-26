#include <copyfile.h>
#include <errno.h>
#include <fcntl.h>
#include <fts.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

extern int proc_pidpath(int pid, void *buffer, uint32_t buffersize);
extern char **environ;

#ifndef LOCAL_PEERPID
#define LOCAL_PEERPID 0x002
#endif
#ifndef SOL_LOCAL
#define SOL_LOCAL 0
#endif

#define GMR_SOCK_PATH "/var/run/yigitzra1n_gmr.sock"
#define GMR_PID_PATH  "/var/run/yigitzra1n_gmr.pid"
#define GMR_LOG_PATH  "/var/jb/var/log/yigitzra1n_gmr.log"
#define GMR_MAX_ARGS  256
#define GMR_MAX_ENV   256
#define GMR_MAX_STR   65536
#define GMR_MAX_FDS   64
#define GMR_SCAN_FDS  512
#define GMR_DEFAULT_PATH "/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

static const char GMR_DAEMON_MARKER[] __attribute__((used)) = "yigitzra1n_gmr_daemon_v4";

static void mkdir_p(const char *path, mode_t mode) {
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof(tmp), "%s", path);
    size_t len = strlen(tmp);
    if (len == 0) return;
    if (tmp[len - 1] == '/') tmp[len - 1] = '\0';
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, mode);
            *p = '/';
        }
    }
    mkdir(tmp, mode);
}

static void gmr_log_exec(const char *cmd, const char *arg1, uint32_t fd_count, int32_t rc) {
    mkdir_p("/var/jb/var/log", 0755);
    struct stat st;
    int flags = O_WRONLY | O_CREAT | O_APPEND;
    if (stat(GMR_LOG_PATH, &st) == 0 && st.st_size > 32768) {
        flags = O_WRONLY | O_CREAT | O_TRUNC;
    }
    int lfd = open(GMR_LOG_PATH, flags, 0644);
    if (lfd < 0) return;
    char line[512];
    int n = snprintf(
        line,
        sizeof(line),
        "gmr fds=%u cmd=%s %s rc=%d\n",
        (unsigned)fd_count,
        cmd ? cmd : "-",
        arg1 ? arg1 : "-",
        (int)rc
    );
    if (n > 0) (void)write(lfd, line, (size_t)n);
    close(lfd);
}

static int run_cmd(const char *prog, char *const argv[]) {
    if (access(prog, X_OK) != 0) return -1;
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        execv(prog, argv);
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return -1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static int copy_tree(const char *src_root, const char *dst_root) {
    char *paths[] = { (char *)src_root, NULL };
    FTS *ftsp = fts_open(paths, FTS_PHYSICAL | FTS_NOCHDIR, NULL);
    if (!ftsp) return -1;

    size_t src_len = strlen(src_root);
    FTSENT *ent;
    while ((ent = fts_read(ftsp)) != NULL) {
        const char *rel = ent->fts_path + src_len;
        while (*rel == '/') rel++;

        char dst[PATH_MAX];
        if (*rel == '\0') {
            snprintf(dst, sizeof(dst), "%s", dst_root);
        } else {
            snprintf(dst, sizeof(dst), "%s/%s", dst_root, rel);
        }

        switch (ent->fts_info) {
            case FTS_D: {
                mode_t m = ent->fts_statp ? (ent->fts_statp->st_mode & 07777) : 0755;
                mkdir_p(dst, m);
                chmod(dst, m);
                if (ent->fts_statp) chown(dst, ent->fts_statp->st_uid, ent->fts_statp->st_gid);
                break;
            }
            case FTS_SL:
            case FTS_SLNONE: {
                char linkbuf[PATH_MAX];
                ssize_t n = readlink(ent->fts_path, linkbuf, sizeof(linkbuf) - 1);
                if (n >= 0) {
                    linkbuf[n] = '\0';
                    unlink(dst);
                    symlink(linkbuf, dst);
                    if (ent->fts_statp) lchown(dst, ent->fts_statp->st_uid, ent->fts_statp->st_gid);
                }
                break;
            }
            case FTS_F: {
                unlink(dst);
                copyfile(ent->fts_path, dst, NULL, COPYFILE_ALL | COPYFILE_NOFOLLOW);
                if (ent->fts_statp) {
                    chmod(dst, ent->fts_statp->st_mode & 07777);
                    chown(dst, ent->fts_statp->st_uid, ent->fts_statp->st_gid);
                }
                break;
            }
            default:
                break;
        }
    }
    fts_close(ftsp);
    return 0;
}

static void touch_marker(const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) close(fd);
}

static void write_file_str(const char *path, const char *content, mode_t mode) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
    if (fd < 0) return;
    size_t len = strlen(content);
    (void)write(fd, content, len);
    fchmod(fd, mode);
    close(fd);
}

static int gmr_read_all(int fd, void *buf, size_t len) {
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

static int gmr_write_all(int fd, const void *buf, size_t len) {
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

static char **gmr_recv_str_vec(int fd, uint32_t max_count, uint32_t *out_count) {
    uint32_t cnt = 0;
    if (gmr_read_all(fd, &cnt, sizeof(cnt)) != 0 || cnt > max_count) return NULL;
    char **vec = calloc((size_t)cnt + 1, sizeof(char *));
    if (!vec) return NULL;
    for (uint32_t i = 0; i < cnt; i++) {
        uint32_t slen = 0;
        if (gmr_read_all(fd, &slen, sizeof(slen)) != 0 || slen > GMR_MAX_STR) {
            for (uint32_t j = 0; j < i; j++) free(vec[j]);
            free(vec);
            return NULL;
        }
        vec[i] = malloc((size_t)slen + 1);
        if (!vec[i]) {
            for (uint32_t j = 0; j < i; j++) free(vec[j]);
            free(vec);
            return NULL;
        }
        if (slen > 0 && gmr_read_all(fd, vec[i], slen) != 0) {
            for (uint32_t j = 0; j <= i; j++) free(vec[j]);
            free(vec);
            return NULL;
        }
        vec[i][slen] = '\0';
    }
    vec[cnt] = NULL;
    if (out_count) *out_count = cnt;
    return vec;
}

static void gmr_free_vec(char **vec) {
    if (!vec) return;
    for (size_t i = 0; vec[i] != NULL; i++) free(vec[i]);
    free(vec);
}

static void gmr_handle_client(int cfd) {
    int peer_pid = -1;
    socklen_t plen = sizeof(peer_pid);
    if (getsockopt(cfd, SOL_LOCAL, LOCAL_PEERPID, &peer_pid, &plen) != 0 || peer_pid <= 0) {
        close(cfd);
        return;
    }

    char peer_path[PATH_MAX * 2];
    memset(peer_path, 0, sizeof(peer_path));
    if (proc_pidpath(peer_pid, peer_path, (uint32_t)sizeof(peer_path)) <= 0 ||
        strcmp(peer_path, "/Applications/Sileo.app/giveMeRoot") != 0) {
        close(cfd);
        return;
    }

    uint32_t fd_count = 0;
    struct iovec iov = { .iov_base = &fd_count, .iov_len = sizeof(fd_count) };
    union {
        struct cmsghdr hdr;
        char buf[CMSG_SPACE(sizeof(int) * GMR_MAX_FDS) + 64];
    } cmsg_u;
    memset(&cmsg_u, 0, sizeof(cmsg_u));

    struct msghdr msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cmsg_u.buf;
    msg.msg_controllen = sizeof(cmsg_u.buf);

    ssize_t rn = recvmsg(cfd, &msg, 0);
    if (rn < (ssize_t)sizeof(fd_count) || fd_count > GMR_MAX_FDS) {
        close(cfd);
        return;
    }

    int recv_fds[GMR_MAX_FDS];
    int32_t target_fds[GMR_MAX_FDS];
    for (int i = 0; i < GMR_MAX_FDS; i++) {
        recv_fds[i] = -1;
        target_fds[i] = -1;
    }

    uint32_t got_fds = 0;
    for (struct cmsghdr *cm = CMSG_FIRSTHDR(&msg); cm != NULL; cm = CMSG_NXTHDR(&msg, cm)) {
        if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_RIGHTS) {
            size_t payload_len = cm->cmsg_len > CMSG_LEN(0) ? (size_t)(cm->cmsg_len - CMSG_LEN(0)) : 0;
            uint32_t n = (uint32_t)(payload_len / sizeof(int));
            if (n > GMR_MAX_FDS) n = GMR_MAX_FDS;
            if (n > 0) memcpy(recv_fds, CMSG_DATA(cm), sizeof(int) * n);
            got_fds = n;
            break;
        }
    }
    if (got_fds < fd_count) {
        for (uint32_t i = 0; i < got_fds; i++) if (recv_fds[i] >= 0) close(recv_fds[i]);
        close(cfd);
        return;
    }
    for (uint32_t i = fd_count; i < got_fds; i++) {
        if (recv_fds[i] >= 0) close(recv_fds[i]);
    }

    if (fd_count > 0 && gmr_read_all(cfd, target_fds, sizeof(int32_t) * fd_count) != 0) {
        for (uint32_t i = 0; i < fd_count; i++) if (recv_fds[i] >= 0) close(recv_fds[i]);
        close(cfd);
        return;
    }
    for (uint32_t i = 0; i < fd_count; i++) {
        if (recv_fds[i] < 0 || target_fds[i] < 0 || target_fds[i] >= GMR_SCAN_FDS) {
            for (uint32_t j = 0; j < fd_count; j++) if (recv_fds[j] >= 0) close(recv_fds[j]);
            close(cfd);
            return;
        }
    }

    uint32_t c_argc = 0, c_envc = 0;
    char **c_argv = gmr_recv_str_vec(cfd, GMR_MAX_ARGS, &c_argc);
    char **c_envp = c_argv ? gmr_recv_str_vec(cfd, GMR_MAX_ENV, &c_envc) : NULL;
    if (!c_argv || c_argc == 0 || !c_envp) {
        gmr_free_vec(c_argv);
        gmr_free_vec(c_envp);
        for (uint32_t i = 0; i < fd_count; i++) if (recv_fds[i] >= 0) close(recv_fds[i]);
        close(cfd);
        return;
    }

    char log_cmd[160];
    char log_arg1[160];
    snprintf(log_cmd, sizeof(log_cmd), "%s", c_argv[0] ? c_argv[0] : "-");
    snprintf(log_arg1, sizeof(log_arg1), "%s", (c_argc > 1 && c_argv[1]) ? c_argv[1] : "-");

    signal(SIGCHLD, SIG_DFL);
    pid_t cmd_pid = fork();
    if (cmd_pid == 0) {
        int tmp_fds[GMR_MAX_FDS];
        for (uint32_t i = 0; i < fd_count; i++) {
            tmp_fds[i] = fcntl(recv_fds[i], F_DUPFD_CLOEXEC, GMR_SCAN_FDS);
            if (tmp_fds[i] < 0) tmp_fds[i] = recv_fds[i];
            else close(recv_fds[i]);
        }
        for (uint32_t i = 0; i < fd_count; i++) {
            if (tmp_fds[i] != target_fds[i]) {
                dup2(tmp_fds[i], target_fds[i]);
                close(tmp_fds[i]);
            } else {
                fcntl(target_fds[i], F_SETFD, 0);
            }
        }
        close(cfd);
        setgid(0);
        setuid(0);
        environ = c_envp;
        setenv("HOME", "/var/root", 1);
        const char *cur_path = getenv("PATH");
        if (!cur_path || !strstr(cur_path, "/var/jb/usr/bin")) {
            setenv("PATH", GMR_DEFAULT_PATH, 1);
        }
        execvp(c_argv[0], c_argv);
        _exit(127);
    }

    for (uint32_t i = 0; i < fd_count; i++) {
        if (recv_fds[i] >= 0) close(recv_fds[i]);
    }
    gmr_free_vec(c_argv);
    gmr_free_vec(c_envp);

    int32_t rc = 71;
    if (cmd_pid > 0) {
        int st = 0;
        while (waitpid(cmd_pid, &st, 0) < 0 && errno == EINTR) {}
        if (WIFEXITED(st)) {
            rc = (int32_t)WEXITSTATUS(st);
        } else if (WIFSIGNALED(st)) {
            rc = (int32_t)(128 + WTERMSIG(st));
        }
    }
    gmr_log_exec(log_cmd, log_arg1, fd_count, rc);
    (void)gmr_write_all(cfd, &rc, sizeof(rc));
    close(cfd);
}

static int gmr_socket_alive(void) {
    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s < 0) return 0;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", GMR_SOCK_PATH);
    int ok = (connect(s, (struct sockaddr *)&addr, sizeof(addr)) == 0);
    close(s);
    return ok;
}

static int run_gmr_daemon(int replace_existing) {
    (void)GMR_DAEMON_MARKER;
    mkdir_p("/private/var/run", 0755);

    if (replace_existing) {
        FILE *pf = fopen(GMR_PID_PATH, "r");
        if (pf) {
            long old_pid = 0;
            if (fscanf(pf, "%ld", &old_pid) == 1 && old_pid > 1 && old_pid != (long)getpid()) {
                kill((pid_t)old_pid, SIGTERM);
                usleep(150000);
            }
            fclose(pf);
        }
    }

    int sfd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sfd < 0) return 1;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", GMR_SOCK_PATH);
    unlink(GMR_SOCK_PATH);

    if (bind(sfd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(sfd);
        return 1;
    }
    chown(GMR_SOCK_PATH, 0, 501);
    chmod(GMR_SOCK_PATH, 0660);

    if (listen(sfd, 16) != 0) {
        close(sfd);
        return 1;
    }

    char pidbuf[32];
    snprintf(pidbuf, sizeof(pidbuf), "%ld\n", (long)getpid());
    write_file_str(GMR_PID_PATH, pidbuf, 0644);

    signal(SIGCHLD, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);

    for (;;) {
        int cfd = accept(sfd, NULL, NULL);
        if (cfd < 0) {
            if (errno == EINTR) continue;
            usleep(50000);
            continue;
        }
        pid_t w = fork();
        if (w == 0) {
            close(sfd);
            gmr_handle_client(cfd);
            _exit(0);
        }
        close(cfd);
    }
}

static int ensure_gmr_daemon_started(int replace_existing) {
    if (!replace_existing && gmr_socket_alive()) return 0;
    if (replace_existing) {
        FILE *pf = fopen(GMR_PID_PATH, "r");
        if (pf) {
            long old_pid = 0;
            if (fscanf(pf, "%ld", &old_pid) == 1 && old_pid > 1 && old_pid != (long)getpid()) {
                kill((pid_t)old_pid, SIGTERM);
                usleep(150000);
            }
            fclose(pf);
        }
        unlink(GMR_SOCK_PATH);
    }
    pid_t pid = fork();
    if (pid < 0) return 1;
    if (pid == 0) {
        setsid();
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            dup2(devnull, STDIN_FILENO);
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            if (devnull > STDERR_FILENO) close(devnull);
        }
        _exit(run_gmr_daemon(0));
    }
    for (int i = 0; i < 40; i++) {
        if (gmr_socket_alive()) return 0;
        usleep(50000);
    }
    return 1;
}

int main(int argc, char **argv) {
    setenv("PATH", GMR_DEFAULT_PATH, 1);
    setenv("HOME", "/var/root", 1);

    if (argc >= 2 && strcmp(argv[1], "--gmr-version") == 0) {
        puts(GMR_DAEMON_MARKER);
        return 0;
    }

    if (argc >= 2 && strcmp(argv[1], "--gmr-daemon") == 0) {
        return ensure_gmr_daemon_started(1);
    }

    for (int i = 0; i < 20; i++) {
        struct stat st;
        if (stat("/private/var", &st) == 0 && S_ISDIR(st.st_mode)) break;
        usleep(250000);
    }

    (void)ensure_gmr_daemon_started(0);

    /* Non-fatal: seed statusBarOverrides ("yigitzra1n" + full signal bars) on new installs */
    {
        struct stat st_mob, st_sb;
        if (stat("/private/var/mobile", &st_mob) == 0 && S_ISDIR(st_mob.st_mode) &&
            access("/usr/local/share/yigitzra1n/statusBarOverrides", R_OK) == 0 &&
            (stat("/private/var/mobile/Library/SpringBoard/statusBarOverrides", &st_sb) != 0 ||
             st_sb.st_size != 3944)) {
            struct stat st_lib;
            if (stat("/private/var/mobile/Library", &st_lib) != 0) {
                mkdir_p("/private/var/mobile/Library", 0755);
                chown("/private/var/mobile/Library", 501, 501);
            }
            if (stat("/private/var/mobile/Library/SpringBoard", &st_lib) != 0) {
                mkdir_p("/private/var/mobile/Library/SpringBoard", 0755);
                chown("/private/var/mobile/Library/SpringBoard", 501, 501);
            }
            copyfile("/usr/local/share/yigitzra1n/statusBarOverrides",
                     "/private/var/mobile/Library/SpringBoard/statusBarOverrides",
                     NULL, COPYFILE_DATA);
            chmod("/private/var/mobile/Library/SpringBoard/statusBarOverrides", 0644);
            chown("/private/var/mobile/Library/SpringBoard/statusBarOverrides", 501, 501);
        }
    }

    if (access("/private/var/jb/.yigitzra1n_ready", F_OK) != 0 &&
        access("/usr/local/procursus/usr/sbin/sshd", X_OK) == 0) {
        mkdir_p("/private/var/jb", 0755);
        copy_tree("/usr/local/procursus", "/private/var/jb");
        touch_marker("/private/var/jb/.yigitzra1n_ready");
    }

    if (access("/private/var/jb/.procursus_strapped", F_OK) != 0 &&
        access("/private/var/jb/.yigitzra1n_ready", F_OK) == 0) {
        touch_marker("/private/var/jb/.procursus_strapped");
    }

    mkdir_p("/private/var/jb/var/mobile/Library/Caches", 0755);
    mkdir_p("/private/var/jb/var/mobile/Library/Preferences", 0755);
    chown("/private/var/jb/var/mobile", 501, 501);
    chmod("/private/var/jb/var/mobile", 0755);
    chown("/private/var/jb/var/mobile/Library", 501, 501);
    chmod("/private/var/jb/var/mobile/Library", 0755);
    chown("/private/var/jb/var/mobile/Library/Caches", 501, 501);
    chmod("/private/var/jb/var/mobile/Library/Caches", 0755);
    chown("/private/var/jb/var/mobile/Library/Preferences", 501, 501);
    chmod("/private/var/jb/var/mobile/Library/Preferences", 0755);

    if (access("/Applications/Sileo.app/giveMeRoot", F_OK) == 0) {
        chown("/Applications/Sileo.app/giveMeRoot", 0, 0);
        chmod("/Applications/Sileo.app/giveMeRoot", 04755);
    }

    /* Non-fatal background init: register virtual firmware package & Sileo icon via uicache */
    if ((access("/private/var/jb/.yigitzra1n_fw_ready", F_OK) != 0 &&
         access("/var/jb/usr/libexec/firmware", X_OK) == 0) ||
        (access("/Applications/Sileo.app/Sileo", X_OK) == 0 &&
         access("/private/var/jb/.yigitzra1n_sileo_ready", F_OK) != 0 &&
         access("/var/jb/usr/bin/uicache", X_OK) == 0)) {
        pid_t bg_pid = fork();
        if (bg_pid == 0) {
            if (access("/private/var/jb/.yigitzra1n_fw_ready", F_OK) != 0 &&
                access("/var/jb/usr/libexec/firmware", X_OK) == 0) {
                char *fw_args[] = { "/var/jb/usr/libexec/firmware", NULL };
                if (run_cmd("/var/jb/usr/libexec/firmware", fw_args) == 0) {
                    touch_marker("/private/var/jb/.yigitzra1n_fw_ready");
                }
            }
            if (access("/Applications/Sileo.app/Sileo", X_OK) == 0 &&
                access("/private/var/jb/.yigitzra1n_sileo_ready", F_OK) != 0 &&
                access("/var/jb/usr/bin/uicache", X_OK) == 0) {
                sleep(3);
                char *uc_args[] = { "/var/jb/usr/bin/uicache", "-p", "/Applications/Sileo.app", NULL };
                for (int attempt = 0; attempt < 3; attempt++) {
                    if (run_cmd("/var/jb/usr/bin/uicache", uc_args) == 0) {
                        touch_marker("/private/var/jb/.yigitzra1n_sileo_ready");
                        break;
                    }
                    sleep(4);
                }
            }
            _exit(0);
        }
    }

    mkdir_p("/private/var/jb/var/empty", 0755);
    chmod("/private/var/jb/var/empty", 0755);
    chown("/private/var/jb/var/empty", 0, 0);

    mkdir_p("/private/var/empty", 0755);
    chmod("/private/var/empty", 0755);
    chown("/private/var/empty", 0, 0);

    mkdir_p("/private/var/jb/var/root", 0700);
    mkdir_p("/private/var/root", 0700);
    mkdir_p("/private/var/jb/etc/ssh", 0755);
    mkdir_p("/private/var/log", 0755);

    if (access("/private/var/jb/.yigitzra1n_pwd_ready", F_OK) != 0 &&
        access("/var/jb/etc/master.passwd", R_OK) == 0) {
        chmod("/var/jb/etc/master.passwd", 0600);
        chown("/var/jb/etc/master.passwd", 0, 0);
        char *pwd_args[] = {
            "/var/jb/usr/sbin/pwd_mkdb", "-p", "-d", "/var/jb/etc", "/var/jb/etc/master.passwd", NULL
        };
        if (run_cmd("/var/jb/usr/sbin/pwd_mkdb", pwd_args) == 0) {
            touch_marker("/private/var/jb/.yigitzra1n_pwd_ready");
        }
    }

    const char *ktypes[] = { "rsa", "ecdsa", "ed25519" };
    for (size_t i = 0; i < 3; i++) {
        char kpath[PATH_MAX];
        snprintf(kpath, sizeof(kpath), "/var/jb/etc/ssh/ssh_host_%s_key", ktypes[i]);
        if (access(kpath, R_OK) != 0) {
            char *kg_args[] = {
                "/var/jb/usr/bin/ssh-keygen", "-q", "-t", (char *)ktypes[i], "-N", "", "-f", kpath, NULL
            };
            run_cmd("/var/jb/usr/bin/ssh-keygen", kg_args);
            chmod(kpath, 0600);
        }
    }

    const char *cfg_path = "/var/jb/etc/ssh/sshd_config_ios16";
    if (access(cfg_path, R_OK) != 0) {
        const char *cfg =
            "Port 22\n"
            "ListenAddress 0.0.0.0\n"
            "HostKey /var/jb/etc/ssh/ssh_host_rsa_key\n"
            "HostKey /var/jb/etc/ssh/ssh_host_ecdsa_key\n"
            "HostKey /var/jb/etc/ssh/ssh_host_ed25519_key\n"
            "PermitRootLogin yes\n"
            "PasswordAuthentication yes\n"
            "UsePAM no\n"
            "StrictModes no\n"
            "Subsystem sftp /var/jb/usr/libexec/sftp-server\n";
        write_file_str(cfg_path, cfg, 0644);
    }

    char *sshd_args[] = {
        "/var/jb/usr/sbin/sshd", "-D", "-e", "-f", (char *)cfg_path, NULL
    };
    execv("/var/jb/usr/sbin/sshd", sshd_args);
    sleep(5);
    return 1;
}
