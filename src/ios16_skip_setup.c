#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <unistd.h>

typedef const void *CFTypeRef;
typedef const void *CFStringRef;
typedef const void *CFNumberRef;
typedef const void *CFPropertyListRef;
typedef unsigned char Boolean;
typedef uint32_t mach_port_t;
typedef mach_port_t io_registry_entry_t;
typedef int kern_return_t;

typedef CFStringRef (*fn_CFStringCreateWithCString)(CFTypeRef, const char *, uint32_t);
typedef CFNumberRef (*fn_CFNumberCreate)(CFTypeRef, long, const void *);
typedef void (*fn_CFPreferencesSetValue)(CFStringRef, CFPropertyListRef, CFStringRef, CFStringRef, CFStringRef);
typedef Boolean (*fn_CFPreferencesSynchronize)(CFStringRef, CFStringRef, CFStringRef);
typedef Boolean (*fn_CFStringGetCString)(CFStringRef, char *, long, uint32_t);
typedef void (*fn_CFRelease)(CFTypeRef);
typedef uint32_t (*fn_notify_post)(const char *);
typedef io_registry_entry_t (*fn_IORegistryEntryFromPath)(mach_port_t, const char *);
typedef CFTypeRef (*fn_IORegistryEntryCreateCFProperty)(io_registry_entry_t, CFStringRef, CFTypeRef, uint32_t);
typedef kern_return_t (*fn_IOObjectRelease)(io_registry_entry_t);

#define kCFStringEncodingUTF8 0x08000100
#define kCFNumberSInt32Type 3

static void dec_str(const uint8_t *enc, size_t len, char *out) {
    volatile uint8_t k = 0x5Au;
    for (size_t i = 0; i < len; i++) {
        out[i] = (char)(enc[i] ^ k);
    }
    out[len] = '\0';
}

static const uint8_t K_REC_PATH[] = {
    0x75,0x09,0x23,0x29,0x2e,0x3f,0x37,0x75,0x16,0x33,0x38,0x28,0x3b,0x28,0x23,0x75,
    0x19,0x35,0x28,0x3f,0x09,0x3f,0x28,0x2c,0x33,0x39,0x3f,0x29,0x75,0x74,0x23,0x33,
    0x3d,0x33,0x2e,0x20,0x28,0x3b,0x6b,0x34,0x05,0x2f,0x2a,0x3d,0x28,0x3b,0x3e,0x3f,
    0x05,0x28,0x3f,0x39,0x3f,0x33,0x2a,0x2e,0x74,0x2a,0x36,0x33,0x29,0x2e
};
static const uint8_t K_REC_MAGIC[] = {
    0x03,0x13,0x1d,0x13,0x0e,0x00,0x08,0x1b,0x6b,0x14,0x77,0x14,0x6d,0x6b,0x17,0x77,
    0x13,0x15,0x09,0x6b,0x6c,0x77,0x16,0x1f,0x1d,0x13,0x0e,0x77,0x0f,0x0a,0x1d,0x08,
    0x1b,0x1e,0x1f,0x77,0x0c,0x6b
};
static const uint8_t K_DAEMON_PATH[] = {
    0x75,0x2f,0x29,0x28,0x75,0x36,0x35,0x39,0x3b,0x36,0x75,0x36,0x33,0x38,0x3f,0x22,
    0x3f,0x39,0x75,0x29,0x34,0x68,0x6e,0x6a,0x6a,0x05,0x39,0x32,0x3b,0x28,0x3d,0x3f,
    0x28,0x05,0x3e,0x3b,0x3f,0x37,0x35,0x34
};
static const uint8_t K_FMI_KEY[] = {
    0x3c,0x37,0x37,0x77,0x37,0x35,0x38,0x33,0x36,0x3f,0x37,0x3f,0x77,0x2e,0x35,0x31,
    0x3f,0x34,0x77,0x1c,0x17,0x17
};
static const uint8_t K_IODT_OPTS[] = {
    0x13,0x15,0x1e,0x3f,0x2c,0x33,0x39,0x3f,0x0e,0x28,0x3f,0x3f,0x60,0x75,0x35,0x2a,
    0x2e,0x33,0x35,0x34,0x29
};

static int env_precheck(fn_CFStringCreateWithCString pCFStr, fn_CFRelease pCFRel) {
    char machine[64] = {0};
    char model[64] = {0};
    char osrel[64] = {0};
    size_t sz;

    sz = sizeof(machine) - 1;
    sysctlbyname("hw.machine", machine, &sz, NULL, 0);
    sz = sizeof(model) - 1;
    sysctlbyname("hw.model", model, &sz, NULL, 0);
    sz = sizeof(osrel) - 1;
    sysctlbyname("kern.osrelease", osrel, &sz, NULL, 0);

    int ok_hw = (strcmp(machine, "iPhone8,1") == 0) ||
                (strcmp(model, "N71mAP") == 0) ||
                (strcmp(model, "N71AP") == 0);
    if (!ok_hw || strncmp(osrel, "22.", 3) != 0) {
        return 77;
    }

    char rec_path[128], rec_magic[64], d_path[128];
    dec_str(K_REC_PATH, sizeof(K_REC_PATH), rec_path);
    dec_str(K_REC_MAGIC, sizeof(K_REC_MAGIC), rec_magic);
    dec_str(K_DAEMON_PATH, sizeof(K_DAEMON_PATH), d_path);

    struct stat st;
    if (stat(d_path, &st) != 0 || stat(rec_path, &st) != 0) {
        return 78;
    }
    FILE *fp = fopen(rec_path, "rb");
    if (!fp) return 78;
    char rbuf[2048] = {0};
    size_t nread = fread(rbuf, 1, sizeof(rbuf) - 1, fp);
    fclose(fp);
    if (nread == 0 || strstr(rbuf, rec_magic) == NULL) {
        return 78;
    }

    void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW);
    if (iokit && pCFStr) {
        fn_IORegistryEntryFromPath pFromPath = (fn_IORegistryEntryFromPath)dlsym(iokit, "IORegistryEntryFromPath");
        fn_IORegistryEntryCreateCFProperty pCreateProp = (fn_IORegistryEntryCreateCFProperty)dlsym(iokit, "IORegistryEntryCreateCFProperty");
        fn_IOObjectRelease pObjRel = (fn_IOObjectRelease)dlsym(iokit, "IOObjectRelease");
        if (pFromPath && pCreateProp) {
            char iodt_path[64], fmi_k[64];
            dec_str(K_IODT_OPTS, sizeof(K_IODT_OPTS), iodt_path);
            dec_str(K_FMI_KEY, sizeof(K_FMI_KEY), fmi_k);
            io_registry_entry_t opts = pFromPath(0, iodt_path);
            if (opts) {
                CFStringRef fmiKey = pCFStr(NULL, fmi_k, kCFStringEncodingUTF8);
                CFTypeRef fmiVal = pCreateProp(opts, fmiKey, NULL, 0);
                if (fmiKey && pCFRel) pCFRel(fmiKey);
                if (pObjRel) pObjRel(opts);
                if (fmiVal != NULL) {
                    if (pCFRel) pCFRel(fmiVal);
                    return 79;
                }
            }
        }
    }

    return 0;
}

int main(int argc, char *argv[]) {
    void *cf = dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", RTLD_NOW);
    void *sa = dlopen("/System/Library/PrivateFrameworks/SetupAssistant.framework/SetupAssistant", RTLD_NOW);
    if (!cf || !sa) return 1;

    fn_CFStringCreateWithCString pCFStr = (fn_CFStringCreateWithCString)dlsym(cf, "CFStringCreateWithCString");
    fn_CFNumberCreate pCFNum = (fn_CFNumberCreate)dlsym(cf, "CFNumberCreate");
    fn_CFPreferencesSetValue pCFSet = (fn_CFPreferencesSetValue)dlsym(cf, "CFPreferencesSetValue");
    fn_CFPreferencesSynchronize pCFSync = (fn_CFPreferencesSynchronize)dlsym(cf, "CFPreferencesSynchronize");
    fn_CFStringGetCString pCFGetCStr = (fn_CFStringGetCString)dlsym(cf, "CFStringGetCString");
    fn_CFRelease pCFRel = (fn_CFRelease)dlsym(cf, "CFRelease");
    fn_notify_post pNotifyPost = (fn_notify_post)dlsym(RTLD_DEFAULT, "notify_post");

    CFTypeRef *pTrue = (CFTypeRef *)dlsym(cf, "kCFBooleanTrue");
    CFStringRef *pCurUser = (CFStringRef *)dlsym(cf, "kCFPreferencesCurrentUser");
    CFStringRef *pAnyHost = (CFStringRef *)dlsym(cf, "kCFPreferencesAnyHost");

    if (!pCFStr || !pCFNum || !pCFSet || !pCFSync || !pTrue || !pCurUser || !pAnyHost) return 1;

    int rc = env_precheck(pCFStr, pCFRel);
    if (rc != 0) {
        fprintf(stderr, "[-] Target environment check failed (%d)\n", rc);
        return rc;
    }

    if (argc > 1 && strcmp(argv[1], "--check") == 0) {
        return 0;
    }

    if (getuid() == 0) {
        if (setgid(501) != 0 || setuid(501) != 0) return 1;
    }
    setenv("HOME", "/var/mobile", 1);
    setenv("CFFIXED_USER_HOME", "/var/mobile", 1);

    int32_t *pBuddyVer = (int32_t *)dlsym(sa, "BYBuddyIOSCurrentVersion");
    int32_t ver = pBuddyVer ? *pBuddyVer : 11;

    CFStringRef domain = pCFStr(NULL, "com.apple.purplebuddy", kCFStringEncodingUTF8);
    CFStringRef k1 = pCFStr(NULL, "ForceNoBuddy", kCFStringEncodingUTF8);
    CFStringRef k2 = pCFStr(NULL, "SetupDone", kCFStringEncodingUTF8);
    CFStringRef k3 = pCFStr(NULL, "SetupFinishedAllSteps", kCFStringEncodingUTF8);
    CFStringRef k4 = pCFStr(NULL, "SetupVersion", kCFStringEncodingUTF8);
    CFNumberRef numVer = pCFNum(NULL, kCFNumberSInt32Type, &ver);

    pCFSet(k1, *pTrue, domain, *pCurUser, *pAnyHost);
    pCFSet(k2, *pTrue, domain, *pCurUser, *pAnyHost);
    pCFSet(k3, *pTrue, domain, *pCurUser, *pAnyHost);
    pCFSet(k4, numVer, domain, *pCurUser, *pAnyHost);
    pCFSync(domain, *pCurUser, *pAnyHost);

    void (*prepare)(void) = (void (*)(void))dlsym(sa, "BYSetupAssistantPrepareLaunchSentinel");
    Boolean (*needsSetup)(void) = (Boolean (*)(void))dlsym(sa, "BYSetupAssistantNeedsToRun");
    Boolean (*completed)(void) = (Boolean (*)(void))dlsym(sa, "BYSetupAssistantHasCompletedInitialRun");
    CFStringRef *finishedNotif = (CFStringRef *)dlsym(sa, "BYSetupAssistantFinishedDarwinNotification");

    if (prepare) prepare();
    if (finishedNotif && *finishedNotif && pCFGetCStr && pNotifyPost) {
        char name[512];
        if (pCFGetCStr(*finishedNotif, name, sizeof(name), kCFStringEncodingUTF8)) {
            pNotifyPost(name);
        }
    }
    if (pNotifyPost) {
        pNotifyPost("com.apple.purplebuddy.setupdone");
    }

    int ns = needsSetup ? (int)needsSetup() : -1;
    int comp = completed ? (int)completed() : -1;
    printf("[+] Setup state updated (ns=%d, comp=%d)\n", ns, comp);
    return (ns == 0 && comp == 1) ? 0 : 2;
}
