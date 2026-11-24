#include <copyfile.h>
#include <errno.h>
#include <fcntl.h>
#include <fts.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

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

int main(void) {
    setenv("PATH", "/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
    setenv("HOME", "/var/root", 1);

    for (int i = 0; i < 20; i++) {
        struct stat st;
        if (stat("/private/var", &st) == 0 && S_ISDIR(st.st_mode)) break;
        usleep(250000);
    }

    if (access("/private/var/jb/.yigitzra1n_ready", F_OK) != 0 &&
        access("/usr/local/procursus/usr/sbin/sshd", X_OK) == 0) {
        mkdir_p("/private/var/jb", 0755);
        copy_tree("/usr/local/procursus", "/private/var/jb");
        touch_marker("/private/var/jb/.yigitzra1n_ready");
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
