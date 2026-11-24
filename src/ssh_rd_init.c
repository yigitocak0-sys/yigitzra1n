#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

typedef const void *CFTypeRef;
typedef const void *CFStringRef;
typedef uint32_t mach_port_t;
typedef int kern_return_t;
typedef void *IOUSBDeviceControllerRef;
typedef void *IOUSBDeviceDescriptionRef;

#define kCFStringEncodingUTF8 0x08000100

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

static void write_file_str(const char *path, const char *content, mode_t mode) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
    if (fd < 0) return;
    (void)write(fd, content, strlen(content));
    fchmod(fd, mode);
    close(fd);
}

static void *dlopen_any(const char *const *paths) {
    for (size_t i = 0; paths[i]; i++) {
        void *h = dlopen(paths[i], RTLD_NOW);
        if (h) return h;
    }
    return NULL;
}

static void init_usb_mux(void) {
    const char *cf_paths[] = {
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
        NULL
    };
    const char *io_paths[] = {
        "/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit",
        "/System/Library/Frameworks/IOKit.framework/IOKit",
        NULL
    };
    void *cf = dlopen_any(cf_paths);
    void *io = dlopen_any(io_paths);
    if (!cf || !io) {
        fprintf(stderr, "[ssh_rd_init] dlopen CF/IOKit failed: %s\n", dlerror());
        return;
    }

    typedef CFStringRef (*fn_CFStr)(CFTypeRef, const char *, uint32_t);
    typedef void (*fn_CFRel)(CFTypeRef);
    typedef kern_return_t (*fn_CtrlCreate)(CFTypeRef, IOUSBDeviceControllerRef *);
    typedef IOUSBDeviceDescriptionRef (*fn_DescCreateType)(CFTypeRef, CFStringRef);
    typedef void (*fn_DescSetSerial)(IOUSBDeviceDescriptionRef, CFStringRef);
    typedef kern_return_t (*fn_CtrlSetDesc)(IOUSBDeviceControllerRef, IOUSBDeviceDescriptionRef);

    fn_CFStr pCFStr = (fn_CFStr)dlsym(cf, "CFStringCreateWithCString");
    fn_CFRel pCFRel = (fn_CFRel)dlsym(cf, "CFRelease");
    fn_CtrlCreate pCtrlCreate = (fn_CtrlCreate)dlsym(io, "IOUSBDeviceControllerCreate");
    fn_DescCreateType pDescCreateType = (fn_DescCreateType)dlsym(io, "IOUSBDeviceDescriptionCreateWithType");
    fn_DescSetSerial pDescSetSerial = (fn_DescSetSerial)dlsym(io, "IOUSBDeviceDescriptionSetSerialString");
    fn_CtrlSetDesc pCtrlSetDesc = (fn_CtrlSetDesc)dlsym(io, "IOUSBDeviceControllerSetDescription");

    if (!pCFStr || !pCtrlCreate || !pDescCreateType || !pCtrlSetDesc) {
        fprintf(stderr, "[ssh_rd_init] Missing IOUSBDeviceController symbols\n");
        return;
    }

    IOUSBDeviceControllerRef ctrl = NULL;
    for (int attempt = 0; attempt < 20; attempt++) {
        if (pCtrlCreate(NULL, &ctrl) == 0 && ctrl != NULL) break;
        usleep(250000);
    }
    if (!ctrl) {
        fprintf(stderr, "[ssh_rd_init] IOUSBDeviceControllerCreate failed\n");
        return;
    }

    CFStringRef muxType = pCFStr(NULL, "standardMuxOnly", kCFStringEncodingUTF8);
    IOUSBDeviceDescriptionRef desc = pDescCreateType(NULL, muxType);
    if (desc) {
        if (pDescSetSerial) {
            CFStringRef ser = pCFStr(NULL, "yigitzra1n_SSHRD", kCFStringEncodingUTF8);
            if (ser) {
                pDescSetSerial(desc, ser);
                if (pCFRel) pCFRel(ser);
            }
        }
        kern_return_t kr = pCtrlSetDesc(ctrl, desc);
        printf("[ssh_rd_init] USB standardMuxOnly init kr=0x%x\n", kr);
        if (pCFRel) pCFRel(desc);
    }
    if (muxType && pCFRel) pCFRel(muxType);
}

int main(void) {
    setenv("PATH", "/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
    setenv("HOME", "/var/root", 1);

    char *remount_args[] = { "/sbin/mount", "-u", "-w", "/", NULL };
    run_cmd("/sbin/mount", remount_args);

    mkdir_p("/var/jb/var/empty", 0755);
    chmod("/var/jb/var/empty", 0755);
    chown("/var/jb/var/empty", 0, 0);

    mkdir_p("/var/empty", 0755);
    chmod("/var/empty", 0755);
    chown("/var/empty", 0, 0);

    mkdir_p("/var/jb/var/root", 0700);
    mkdir_p("/var/root", 0700);
    mkdir_p("/var/jb/etc/ssh", 0755);
    mkdir_p("/var/log", 0755);
    mkdir_p("/var/run", 0755);
    mkdir_p("/mnt1", 0755);
    mkdir_p("/mnt2", 0755);

    if (access("/var/jb/etc/master.passwd", R_OK) == 0) {
        chmod("/var/jb/etc/master.passwd", 0600);
        chown("/var/jb/etc/master.passwd", 0, 0);
        char *pwd_args[] = {
            "/var/jb/usr/sbin/pwd_mkdb", "-p", "-d", "/var/jb/etc", "/var/jb/etc/master.passwd", NULL
        };
        run_cmd("/var/jb/usr/sbin/pwd_mkdb", pwd_args);
    }

    const char *ed_key = "/var/jb/etc/ssh/ssh_host_ed25519_key";
    if (access(ed_key, R_OK) != 0) {
        char *kg_args[] = {
            "/var/jb/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", (char *)ed_key, NULL
        };
        run_cmd("/var/jb/usr/bin/ssh-keygen", kg_args);
        chmod(ed_key, 0600);
    }

    const char *cfg_path = "/var/jb/etc/ssh/sshd_config_rd";
    const char *cfg =
        "Port 22\n"
        "ListenAddress 0.0.0.0\n"
        "HostKey /var/jb/etc/ssh/ssh_host_ed25519_key\n"
        "PermitRootLogin yes\n"
        "PasswordAuthentication yes\n"
        "UsePAM no\n"
        "StrictModes no\n"
        "Subsystem sftp /var/jb/usr/libexec/sftp-server\n";
    write_file_str(cfg_path, cfg, 0644);

    if (access("/usr/bin/nvmefwupdater", X_OK) == 0) {
        char *nvme_args[] = { "/usr/bin/nvmefwupdater", "--skip", NULL };
        int nrc = run_cmd("/usr/bin/nvmefwupdater", nvme_args);
        printf("[ssh_rd_init] nvmefwupdater --skip rc=%d\n", nrc);
    }

    init_usb_mux();

    for (;;) {
        char *sshd_args[] = {
            "/var/jb/usr/sbin/sshd", "-D", "-e", "-f", (char *)cfg_path, NULL
        };
        run_cmd("/var/jb/usr/sbin/sshd", sshd_args);
        sleep(2);
    }
}
