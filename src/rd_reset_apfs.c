#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/ucred.h>
#include <sys/wait.h>
#include <unistd.h>

static void try_unmount(const char *path) {
    if (unmount(path, MNT_FORCE) == 0) {
        printf("[+] Unmounted %s\n", path);
    } else if (errno != EINVAL && errno != ENOENT) {
        printf("[!] unmount(%s): %s\n", path, strerror(errno));
    }
}

static void unmount_all_non_root(void) {
    int count = getfsstat(NULL, 0, MNT_NOWAIT);
    if (count > 0) {
        struct statfs *fs = calloc((size_t)count, sizeof(struct statfs));
        if (fs) {
            int n = getfsstat(fs, (int)((size_t)count * sizeof(struct statfs)), MNT_NOWAIT);
            for (int i = n - 1; i >= 0; i--) {
                const char *mp = fs[i].f_mntonname;
                if (strcmp(mp, "/") == 0 || strcmp(mp, "/dev") == 0) continue;
                try_unmount(mp);
            }
            free(fs);
        }
    }
    try_unmount("/mnt2");
    try_unmount("/mnt1");
    sync();
}

int main(int argc, char **argv) {
    if (argc < 2 || strcmp(argv[1], "--yes") != 0) {
        printf("WARNING: This erases the APFS container (/dev/disk0s1) so stock iOS 15.8.8\n");
        printf("         can restore cleanly without Error 9. Raw /dev/disk0 & Syscfg are untouched.\n");
        printf("Run: rd_reset_apfs --yes\n");
        return 1;
    }

    unmount_all_non_root();

    const char *dev = "/dev/disk0s1";
    if (access(dev, F_OK) != 0 && access("/usr/bin/nvmefwupdater", X_OK) == 0) {
        pid_t npid = fork();
        if (npid == 0) {
            char *nargs[] = { "/usr/bin/nvmefwupdater", "--skip", NULL };
            execv("/usr/bin/nvmefwupdater", nargs);
            _exit(127);
        }
        if (npid > 0) {
            int nst = 0;
            waitpid(npid, &nst, 0);
        }
    }
    for (int w = 0; w < 20 && access(dev, F_OK) != 0; w++) {
        sleep(1);
    }
    if (access(dev, F_OK) != 0) {
        fprintf(stderr, "[ERROR] %s not found after 20s.\n", dev);
        return 1;
    }

    printf("[*] Reinitializing APFS container on %s...\n", dev);
    pid_t pid = fork();
    if (pid == 0) {
        char *args[] = { "/sbin/newfs_apfs", "-v", "Clean", (char *)dev, NULL };
        execv("/sbin/newfs_apfs", args);
        perror("execv /sbin/newfs_apfs");
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "[ERROR] newfs_apfs failed (status=%d)\n", status);
        return 1;
    }
    sync();
    printf("[+] APFS container reset complete. You can now reboot into DFU/Recovery and restore iOS 15.8.8.\n");
    return 0;
}
