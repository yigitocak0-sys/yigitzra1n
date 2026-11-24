#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <errno.h>
#include <mach/mach.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef uint32_t io_object_t;
typedef io_object_t io_service_t;
typedef io_object_t io_connect_t;
typedef int kern_return_t;
typedef uint32_t IOPMAssertionID;
typedef uint32_t IOPMAssertionLevel;

static void *(*p_match)(const char *);
static void *(*p_name)(const char *);
static io_service_t (*p_get)(mach_port_t, void *);
static kern_return_t (*p_open)(io_service_t, task_port_t, uint32_t, io_connect_t *);
static kern_return_t (*p_sc)(io_connect_t, uint32_t, const uint64_t *, uint32_t, uint64_t *, uint32_t *);
static kern_return_t (*p_st)(io_connect_t, uint32_t, const void *, size_t, void *, size_t *);
static kern_return_t (*p_setprops)(io_object_t, CFTypeRef);
static kern_return_t (*p_close)(io_connect_t);
static kern_return_t (*p_rel)(io_object_t);
static kern_return_t (*p_pm_create)(CFStringRef, IOPMAssertionLevel, CFStringRef, IOPMAssertionID *);
static kern_return_t (*p_pm_release)(IOPMAssertionID);

static volatile sig_atomic_t g_stop = 0;
static void on_sig(int s) { (void)s; g_stop = 1; }

static void diag_log(const char *msg) {
    FILE *f = fopen("/var/tmp/sn2400_v6.log", "a");
    if (!f) return;
    fprintf(f, "[%ld] %s\n", (long)time(NULL), msg);
    fclose(f);
}

static void restart_proc_by_name(const char *target) {
    int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0 };
    size_t sz = 0;
    if (sysctl(mib, 4, NULL, &sz, NULL, 0) != 0 || sz == 0) return;
    struct kinfo_proc *procs = (struct kinfo_proc *)malloc(sz);
    if (!procs) return;
    if (sysctl(mib, 4, procs, &sz, NULL, 0) == 0) {
        size_t cnt = sz / sizeof(struct kinfo_proc);
        for (size_t i = 0; i < cnt; i++) {
            pid_t pid = procs[i].kp_proc.p_pid;
            if (pid <= 1) continue;
            if (strncmp(procs[i].kp_proc.p_comm, target, MAXCOMLEN) == 0) {
                kill(pid, SIGKILL);
                char msg[128];
                snprintf(msg, sizeof(msg), "AOP: refreshed %s (pid=%d) after firmware bring-up", target, (int)pid);
                diag_log(msg);
            }
        }
    }
    free(procs);
}

#define CLOCK_FILE     "/var/db/n71m_last_clock"
#define CLOCK_FILE_TMP "/var/db/n71m_last_clock.tmp"
#define CLOCK_MIN_VALID 1700000000L
#define CLOCK_FALLBACK  1795353000L

static time_t g_last_clock_saved = 0;

static void save_clock(time_t now) {
    if (now < CLOCK_MIN_VALID) return;
    FILE *f = fopen(CLOCK_FILE_TMP, "w");
    if (!f) return;
    fprintf(f, "%ld\n", (long)now);
    fflush(f);
    fclose(f);
    if (rename(CLOCK_FILE_TMP, CLOCK_FILE) == 0) {
        g_last_clock_saved = now;
    }
}

static void restore_boot_clock(void) {
    time_t now = time(NULL);
    if (now >= CLOCK_MIN_VALID) {
        save_clock(now);
        char msg[128];
        snprintf(msg, sizeof(msg), "CLOCK: valid (%ld), saved to %s", (long)now, CLOCK_FILE);
        diag_log(msg);
        return;
    }

    long target = CLOCK_FALLBACK;
    FILE *f = fopen(CLOCK_FILE, "r");
    if (f) {
        long saved = 0;
        if (fscanf(f, "%ld", &saved) == 1 && saved >= CLOCK_MIN_VALID) {
            target = saved + 30;
        }
        fclose(f);
    }

    struct timeval tv;
    tv.tv_sec = (time_t)target;
    tv.tv_usec = 0;
    errno = 0;
    int rc = settimeofday(&tv, NULL);
    int err = errno;
    char msg[160];
    snprintf(msg, sizeof(msg), "CLOCK: boot time %ld < min -> settimeofday(%ld) rc=%d errno=%d",
             (long)now, target, rc, err);
    diag_log(msg);
    if (rc == 0) {
        g_last_clock_saved = (time_t)target;
    }
}

static int ensure_aop_firmware(void) {
    const char *fw_path = "/usr/local/share/firmware/s8000aop.bin";

    io_service_t active = p_get(0, p_match("AppleSPUHIDDevice"));
    if (active) {
        p_rel(active);
        diag_log("AOP: already active (AppleSPUHIDDevice present)");
        return 1;
    }

    char bootargs[512];
    size_t blen = sizeof(bootargs);
    memset(bootargs, 0, sizeof(bootargs));
    if (sysctlbyname("kern.bootargs", bootargs, &blen, NULL, 0) != 0 ||
        strstr(bootargs, "skipFWValidation=1") == NULL) {
        diag_log("AOP: skipFWValidation=1 not in kern.bootargs, skipping auto-load");
        return 0;
    }

    io_service_t svc = 0;
    for (int i = 0; i < 40 && !g_stop; i++) {
        svc = p_get(0, p_match("AppleFirmwareUpdateKext"));
        if (svc) break;
        active = p_get(0, p_match("AppleSPUHIDDevice"));
        if (active) {
            p_rel(active);
            diag_log("AOP: AppleSPUHIDDevice became active during wait");
            return 1;
        }
        usleep(250000);
    }
    if (!svc) {
        diag_log("AOP: AppleFirmwareUpdateKext not found after 10s, will retry");
        return 0;
    }

    FILE *f = fopen(fw_path, "rb");
    if (!f) {
        diag_log("AOP: firmware file missing (/usr/local/share/firmware/s8000aop.bin)");
        p_rel(svc);
        return 0;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > (8 * 1024 * 1024)) {
        diag_log("AOP: invalid firmware size");
        fclose(f);
        p_rel(svc);
        return 0;
    }
    void *buf = malloc((size_t)sz);
    if (!buf || (long)fread(buf, 1, (size_t)sz, f) != sz) {
        diag_log("AOP: firmware read failed");
        free(buf);
        fclose(f);
        p_rel(svc);
        return 0;
    }
    fclose(f);

    io_connect_t conn = 0;
    kern_return_t rc = p_open(svc, mach_task_self(), 0, &conn);
    p_rel(svc);
    if (rc != 0) {
        char msg[96];
        snprintf(msg, sizeof(msg), "AOP: IOServiceOpen failed rc=0x%x", rc);
        diag_log(msg);
        free(buf);
        return 0;
    }

    uint64_t args[4] = { (uint64_t)(uintptr_t)buf, (uint64_t)sz, 1ULL, 0ULL };
    rc = p_sc(conn, 0, args, 4, NULL, NULL);
    p_close(conn);
    free(buf);

    char msg[128];
    snprintf(msg, sizeof(msg), "AOP: loadFirmware(%ld B, mode=1) rc=0x%x", sz, rc);
    diag_log(msg);
    if (rc == 0) {
        for (int i = 0; i < 20; i++) {
            active = p_get(0, p_match("AppleSPUHIDDevice"));
            if (active) {
                p_rel(active);
                break;
            }
            usleep(100000);
        }
        restart_proc_by_name("mediaserverd");
        return 1;
    }
    return 0;
}

static int r8(io_connect_t tc, uint8_t reg, uint8_t *out) {
    if (p_st(tc, 0, &reg, 1, NULL, NULL) != 0) return -1;
    size_t sz = 1; uint8_t v = 0;
    if (p_st(tc, 2, NULL, 0, &v, &sz) != 0) return -1;
    *out = v; return 0;
}

static int w8(io_connect_t tc, uint8_t reg, uint8_t val) {
    uint8_t b[2] = {reg, val};
    return p_st(tc, 0, b, 2, NULL, NULL);
}

static int r16(io_connect_t c, uint16_t reg, uint8_t *out) {
    uint8_t a[2] = {(uint8_t)(reg >> 8), (uint8_t)(reg & 0xff)};
    if (p_st(c, 0, a, 2, NULL, NULL) != 0) return -1;
    size_t sz = 1; uint8_t v = 0;
    if (p_st(c, 2, NULL, 0, &v, &sz) != 0) return -1;
    *out = v; return 0;
}

static int w16(io_connect_t c, uint16_t reg, uint8_t val) {
    uint8_t b[3] = {(uint8_t)(reg >> 8), (uint8_t)(reg & 0xff), val};
    return p_st(c, 0, b, 3, NULL, NULL);
}

static void ensure_d2255_rails(void) {
    io_service_t pmu = p_get(0, p_name("pmu"));
    if (!pmu) {
        diag_log("D2255: pmu service not found");
        return;
    }
    io_connect_t c = 0;
    if (p_open(pmu, mach_task_self(), 0, &c) == 0) {
        uint8_t v = 0, v_wlan = 0xff, v_bt = 0xff, v_codec_pd = 0xff, v_ldo8 = 0xff, v_ldo21 = 0xff;
        if (r16(c, 0x0910, &v) == 0 && (v & 0x01u) == 0) {
            uint8_t base = (v >= 0x40u) ? v : 0x80u;
            uint8_t low = (uint8_t)(base & ~0x19u);
            w16(c, 0x0910, low);
            usleep(20000);
            w16(c, 0x0910, (uint8_t)(low | 0x01u));
            usleep(50000);
        }
        if (r16(c, 0x0311, &v) == 0 && (v & 0x01u) == 0) w16(c, 0x0311, (uint8_t)(v | 0x01u));
        if (r16(c, 0x0306, &v) == 0 && (v & 0x01u) == 0) w16(c, 0x0306, (uint8_t)(v | 0x01u));

        r16(c, 0x0914, &v_wlan);
        r16(c, 0x0910, &v_bt);
        r16(c, 0x0918, &v_codec_pd);
        r16(c, 0x0311, &v_ldo8);
        r16(c, 0x0306, &v_ldo21);
        char buf[160];
        snprintf(buf, sizeof(buf),
                 "D2255: wlan(0914)=0x%02x bt(0910)=0x%02x codec_pd(0918)=0x%02x ldo8(0311)=0x%02x ldo21(0306)=0x%02x",
                 v_wlan, v_bt, v_codec_pd, v_ldo8, v_ldo21);
        diag_log(buf);
        p_close(c);
    }
    p_rel(pmu);
}

static void ensure_sleep_assertions(IOPMAssertionID *id_idle, IOPMAssertionID *id_sys) {
    if (!p_pm_create) return;
    if (*id_idle == 0) {
        kern_return_t rc = p_pm_create(
            CFSTR("NoIdleSleepAssertion"), 255,
            CFSTR("N71m Prevent Deep AP Sleep (Idle)"), id_idle);
        if (rc == 0) {
            char buf[96];
            snprintf(buf, sizeof(buf), "IOPMAssertion NoIdleSleepAssertion OK id=%u", *id_idle);
            diag_log(buf);
        } else {
            *id_idle = 0;
        }
    }
    if (*id_sys == 0) {
        kern_return_t rc = p_pm_create(
            CFSTR("PreventSystemSleep"), 255,
            CFSTR("N71m Prevent Deep AP Sleep (Sys)"), id_sys);
        if (rc == 0) {
            char buf[96];
            snprintf(buf, sizeof(buf), "IOPMAssertion PreventSystemSleep OK id=%u", *id_sys);
            diag_log(buf);
        } else {
            *id_sys = 0;
        }
    }
}

static void hdq_to_bmu(io_connect_t tc) {
    w8(tc, 0x1d, 0x04);
    for (int i = 0; i < 25; i++) {
        usleep(10000);
        uint8_t v = 0;
        if (r8(tc, 0x1d, &v) == 0 && (v & 0x20)) break;
    }
    w8(tc, 0x1d, 0x06);
    usleep(80000);
}

typedef struct {
    int ok;
    int temp_01k;
    int volt_mv;
    int soc;
    int rem_mah;
    int fcc_mah;
    int16_t avg_ma;
} bq_t;

static bq_t read_bq(void) {
    bq_t b;
    memset(&b, 0, sizeof(b));
    io_service_t hdq = p_get(0, p_match("AppleHDQGasGaugeControl"));
    if (!hdq) return b;
    io_connect_t hc = 0;
    if (p_open(hdq, mach_task_self(), 0, &hc) != 0) {
        p_rel(hdq);
        return b;
    }
    p_rel(hdq);

    uint64_t v = 0, c = 0;
    uint32_t n = 1;
    c = 0x06; n = 1; if (p_sc(hc, 8, &c, 1, &v, &n) == 0) b.temp_01k = (int)(v & 0xffff);
    c = 0x08; n = 1; if (p_sc(hc, 8, &c, 1, &v, &n) == 0) b.volt_mv  = (int)(v & 0xffff);
    c = 0x10; n = 1; if (p_sc(hc, 8, &c, 1, &v, &n) == 0) b.rem_mah  = (int)(v & 0xffff);
    c = 0x12; n = 1; if (p_sc(hc, 8, &c, 1, &v, &n) == 0) b.fcc_mah  = (int)(v & 0xffff);
    c = 0x14; n = 1; if (p_sc(hc, 8, &c, 1, &v, &n) == 0) b.avg_ma   = (int16_t)(v & 0xffff);
    c = 0x2c; n = 1; if (p_sc(hc, 8, &c, 1, &v, &n) == 0) b.soc      = (int)(v & 0xffff);
    if (b.volt_mv > 2000 && b.volt_mv < 5000 && b.soc >= 0 && b.soc <= 100) b.ok = 1;
    p_close(hc);
    return b;
}

static void add_i32(CFMutableDictionaryRef d, CFStringRef k, int val) {
    CFNumberRef n = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &val);
    if (n) {
        CFDictionarySetValue(d, k, n);
        CFRelease(n);
    }
}

static void push_pmu_props(int ext_connected, int inst_ma, const bq_t *bq) {
    if (!p_setprops) return;
    io_service_t chg = p_get(0, p_match("AppleARMPMUCharger"));
    if (!chg) return;

    CFMutableDictionaryRef dict = CFDictionaryCreateMutable(
        kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (dict) {
        CFDictionarySetValue(dict, CFSTR("ExternalConnected"),
                             ext_connected ? kCFBooleanTrue : kCFBooleanFalse);
        add_i32(dict, CFSTR("InstantAmperage"), inst_ma);
        if (bq && bq->ok) {
            add_i32(dict, CFSTR("CurrentCapacity"), bq->soc);
            add_i32(dict, CFSTR("MaxCapacity"), 100);
            add_i32(dict, CFSTR("Voltage"), bq->volt_mv);
            add_i32(dict, CFSTR("Temperature"), bq->temp_01k);
            add_i32(dict, CFSTR("AppleRawCurrentCapacity"), bq->rem_mah);
            add_i32(dict, CFSTR("AppleRawMaxCapacity"), bq->fcc_mah > 0 ? bq->fcc_mah : 1715);
            add_i32(dict, CFSTR("BatteryInstalled"), 1);
            CFDictionarySetValue(dict, CFSTR("FullyCharged"),
                                 (bq->soc >= 100) ? kCFBooleanTrue : kCFBooleanFalse);
        }
        p_setprops(chg, dict);
        CFRelease(dict);
    }
    p_rel(chg);
}

static void apply_charge_cycle(io_connect_t tc, int max_ma, bq_t *last_bq, int do_bq_sample) {
    if (do_bq_sample) {
        hdq_to_bmu(tc);
        bq_t cur = read_bq();
        if (cur.ok) *last_bq = cur;
    }

    uint8_t r0f = 0;
    int vreg_ok = 1;
    if (r8(tc, 0x0f, &r0f) == 0) {
        unsigned vreg_mv = 3000u + (unsigned)r0f * 10u;
        if (vreg_mv > 4380u) vreg_ok = 0;
    }

    int target_ma = max_ma;
    int allow_charge = vreg_ok;
    if (last_bq->ok) {
        double temp_c = (last_bq->temp_01k - 2731.5) / 10.0;
        if (temp_c < 2.0 || temp_c >= 47.0 || last_bq->volt_mv >= 4370) {
            allow_charge = 0;
        } else if (temp_c >= 43.0) {
            target_ma = 500;
        }
    }

    if (allow_charge) {
        uint8_t ichg = (uint8_t)(target_ma / 10);
        uint8_t iin  = (uint8_t)((target_ma - 90) / 10);
        w8(tc, 0x01, 0xff);
        w8(tc, 0x02, 0xff);
        w8(tc, 0x03, 0xff);
        w8(tc, 0x10, 0x04);
        w8(tc, 0x0d, ichg);
        w8(tc, 0x09, iin);
        w8(tc, 0x1d, 0x00);

        int rep_ma = (last_bq->ok && last_bq->avg_ma > 0) ? last_bq->avg_ma : target_ma;
        push_pmu_props(1, rep_ma, last_bq);
    } else {
        w8(tc, 0x09, 0x29);
        hdq_to_bmu(tc);
        push_pmu_props(1, 0, last_bq);
    }
}

int main(int argc, char **argv) {
    int max_ma = 1000;
    if (argc >= 2) {
        int req = atoi(argv[1]);
        if (req == 500 || req == 1000) max_ma = req;
    }

    signal(SIGTERM, on_sig);
    signal(SIGINT, on_sig);

    restore_boot_clock();
    diag_log("sn2400_charger_daemon v6 starting");

    void *ik = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW);
    if (!ik) return 1;
    p_match      = dlsym(ik, "IOServiceMatching");
    p_name       = dlsym(ik, "IOServiceNameMatching");
    p_get        = dlsym(ik, "IOServiceGetMatchingService");
    p_open       = dlsym(ik, "IOServiceOpen");
    p_sc         = dlsym(ik, "IOConnectCallScalarMethod");
    p_st         = dlsym(ik, "IOConnectCallStructMethod");
    p_setprops   = dlsym(ik, "IORegistryEntrySetCFProperties");
    p_close      = dlsym(ik, "IOServiceClose");
    p_rel        = dlsym(ik, "IOObjectRelease");
    p_pm_create  = dlsym(ik, "IOPMAssertionCreateWithName");
    p_pm_release = dlsym(ik, "IOPMAssertionRelease");

    ensure_d2255_rails();
    int aop_ready = ensure_aop_firmware();

    IOPMAssertionID id_idle = 0, id_sys = 0;
    ensure_sleep_assertions(&id_idle, &id_sys);

    io_connect_t tc = 0;
    while (!g_stop && !tc) {
        io_service_t tig = p_get(0, p_name("tigris"));
        if (tig) {
            if (p_open(tig, mach_task_self(), 0, &tc) != 0) tc = 0;
            p_rel(tig);
        }
        if (!tc) {
            ensure_sleep_assertions(&id_idle, &id_sys);
            if (!aop_ready) aop_ready = ensure_aop_firmware();
            sleep(1);
        }
    }
    if (!tc) return 0;

    int sec = 0;
    int last_ext = -1;
    int last_bq_sec = -100;
    bq_t last_bq;
    memset(&last_bq, 0, sizeof(last_bq));

    while (!g_stop) {
        if ((id_idle == 0 || id_sys == 0) && (sec % 2) == 0) {
            ensure_sleep_assertions(&id_idle, &id_sys);
        }
        if (!aop_ready && (sec % 5) == 0) {
            ensure_d2255_rails();
            aop_ready = ensure_aop_firmware();
        }

        uint8_t r06 = 0, r07 = 0;
        int ok6 = (r8(tc, 0x06, &r06) == 0);
        int ok7 = (r8(tc, 0x07, &r07) == 0);
        int vbus = (ok6 && ((r06 & 0x04u) != 0)) || (ok7 && ((r07 & 0x20u) != 0));
        int edge = (vbus != last_ext);

        if (!vbus) {
            if (edge || (sec - last_bq_sec) >= 10 || !last_bq.ok) {
                uint8_t r1d = 0;
                if (r8(tc, 0x1d, &r1d) == 0 && (r1d & 0x06u) != 0x06u) {
                    hdq_to_bmu(tc);
                }
                bq_t cur = read_bq();
                if (cur.ok) {
                    last_bq = cur;
                    last_bq_sec = sec;
                }
                push_pmu_props(0, last_bq.ok ? last_bq.avg_ma : -150, &last_bq);
                last_ext = 0;
            }
        } else {
            int need_bq = edge || (!last_bq.ok && (sec - last_bq_sec) >= 2) || ((sec - last_bq_sec) >= 20);
            if (edge || need_bq) {
                apply_charge_cycle(tc, max_ma, &last_bq, need_bq);
                if (need_bq) last_bq_sec = sec;
                last_ext = 1;
            }
        }

        time_t now = time(NULL);
        if (now >= CLOCK_MIN_VALID) {
            long diff = (long)(now - g_last_clock_saved);
            if (g_last_clock_saved == 0 || diff >= 60 || diff <= -120) {
                save_clock(now);
            }
        }

        sec++;
        sleep(1);
    }

    save_clock(time(NULL));
    hdq_to_bmu(tc);
    p_close(tc);
    if (p_pm_release) {
        if (id_idle) p_pm_release(id_idle);
        if (id_sys) p_pm_release(id_sys);
    }
    return 0;
}
