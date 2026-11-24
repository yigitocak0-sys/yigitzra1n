#include <stdint.h>
#include <stddef.h>
#include "bootlogo_data.h"

struct pongo_boot_video {
    uint64_t v_baseAddr;
    uint64_t v_display;
    uint64_t v_rowBytes;
    uint64_t v_width;
    uint64_t v_height;
    uint64_t v_depth;
};

struct pongo_boot_args {
    uint16_t Revision;
    uint16_t Version;
    uint32_t _pad;
    uint64_t virtBase;
    uint64_t physBase;
    uint64_t memSize;
    uint64_t topOfKernelData;
    struct pongo_boot_video Video;
};

extern uint64_t gIOBase;
extern uint32_t *gFramebuffer;
extern uint32_t gWidth;
extern uint32_t gHeight;
extern uint32_t gRowPixels;
extern struct pongo_boot_args *gBootArgs;
extern void cache_clean_and_invalidate(void *address, size_t size);
extern void spin(uint32_t usec);
extern int printf(const char *fmt, ...);
extern void command_register(const char *name, const char *desc, void (*cb)(const char *cmd, char *args));

struct pongo_exports {
    const char *name;
    void *value;
};

char *module_name = "yigitzra1n_hwinit";
struct pongo_exports exported_symbols[1];

#define I2C0_OFF        0x0a110000ULL
#define PMGR_I2C0_OFF   0x0e080198ULL
#define I2C1_OFF        0x0a111000ULL
#define PMGR_I2C1_OFF   0x0e0801a0ULL

#define REG_MTXFIFO     0x00
#define REG_MRXFIFO     0x04
#define REG_XFSTA       0x0c
#define REG_SMSTA       0x14
#define REG_IMASK       0x18
#define REG_CTL         0x1c
#define REG_REV         0x28

#define MTXFIFO_START   (1u << 8)
#define MTXFIFO_STOP    (1u << 9)
#define MTXFIFO_READ    (1u << 10)
#define MRXFIFO_EMPTY   (1u << 8)

#define SMSTA_XIP       (1u << 28)
#define SMSTA_XEN       (1u << 27)
#define SMSTA_MTO       (1u << 23)
#define SMSTA_MTA       (1u << 22)
#define SMSTA_MTN       (1u << 21)
#define SMSTA_TOM       (1u << 6)

#define CTL_EN          (1u << 11)
#define CTL_MRR         (1u << 10)
#define CTL_MTR         (1u << 9)
#define CTL_UJM         (1u << 8)
#define CTL_CLK_M       0xffu

#define CHESTNUT_ADDR   0x27u
#define CHESTNUT_REG05  0x05u

#define D2255_ADDR                   0x74u
#define D2255_BT_REG                 0x0910u
#define D2255_WLAN_REG               0x0914u
#define D2255_CAM_AVDD_LDO_CTRL_REG  0x0311u
#define D2255_CAM0_LDO_CTRL_REG      0x0306u

#define SN2400_ADDR      0x75u
#define SN2400_MASK1     0x01u
#define SN2400_MASK2     0x02u
#define SN2400_MASK3     0x03u
#define SN2400_IINLIM    0x09u
#define SN2400_ICHG      0x0du
#define SN2400_VREG_RD   0x0fu
#define SN2400_CTL       0x10u
#define SN2400_HDQ_REG   0x1du

static inline uint32_t mmio_read32(uint64_t addr) {
    uint32_t v = *(volatile uint32_t *)addr;
    __asm__ volatile("dmb ld" ::: "memory");
    return v;
}

static inline void mmio_write32(uint64_t addr, uint32_t val) {
    __asm__ volatile("dmb st" ::: "memory");
    *(volatile uint32_t *)addr = val;
    __asm__ volatile("dmb sy" ::: "memory");
}

static int i2c_ensure_clock(uint64_t pmgr_off, const char *tag) {
    uint64_t ps = gIOBase + pmgr_off;
    uint32_t before = mmio_read32(ps);
    if ((before & 0xfu) != 0xfu || ((before >> 4) & 0xfu) != 0xfu) {
        mmio_write32(ps, (before & ~0xfu) | 0xfu);
        for (int i = 0; i < 1000; i++) {
            uint32_t cur = mmio_read32(ps);
            if ((cur & 0xfu) == ((cur >> 4) & 0xfu)) {
                printf("[HWINIT] PMGR %s clock gated ON: 0x%08x -> 0x%08x\n", tag, before, cur);
                return 0;
            }
            spin(100);
        }
        printf("[HWINIT] PMGR %s clock timeout: 0x%08x\n", tag, mmio_read32(ps));
        return -1;
    }
    return 0;
}

static void i2c0_reset(uint64_t base, uint32_t rev, uint32_t clk_div) {
    uint32_t val = CTL_MTR | CTL_MRR | CTL_UJM | (clk_div & CTL_CLK_M);
    if (rev >= 6) val |= CTL_EN;
    mmio_write32(base + REG_CTL, val);
    mmio_write32(base + REG_IMASK, 0);
    mmio_write32(base + REG_SMSTA, 0xffffffffu);
    spin(500);
}

static int i2c0_wait_xen(uint64_t base, uint32_t *out_sta) {
    uint32_t sta = 0;
    for (int i = 0; i < 100; i++) {
        sta = mmio_read32(base + REG_SMSTA);
        if (sta & SMSTA_XEN) {
            *out_sta = sta;
            mmio_write32(base + REG_SMSTA, SMSTA_XEN);
            if (sta & (SMSTA_TOM | SMSTA_MTO | SMSTA_XIP | SMSTA_MTA | SMSTA_MTN)) {
                return -2;
            }
            return 0;
        }
        spin(1000);
    }
    *out_sta = sta;
    return -1;
}

static int i2c0_read_reg8(uint64_t base, uint32_t rev, uint32_t clk_div, uint8_t addr7, uint8_t reg, uint8_t *out_val) {
    uint32_t sta = 0;
    i2c0_reset(base, rev, clk_div);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_START | ((uint32_t)addr7 << 1));
    mmio_write32(base + REG_MTXFIFO, (uint32_t)reg);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_START | ((uint32_t)addr7 << 1) | 1u);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_READ | MTXFIFO_STOP | 1u);

    int rc = i2c0_wait_xen(base, &sta);
    if (rc != 0) {
        printf("[HWINIT] read8(0x%02x, 0x%02x) err=%d smsta=0x%08x\n", addr7, reg, rc, sta);
        i2c0_reset(base, rev, clk_div);
        return rc;
    }
    uint32_t rd = mmio_read32(base + REG_MRXFIFO);
    if (rd & MRXFIFO_EMPTY) {
        printf("[HWINIT] read8 RX empty rd=0x%08x\n", rd);
        return -3;
    }
    *out_val = (uint8_t)(rd & 0xffu);
    return 0;
}

static int i2c0_write_reg8(uint64_t base, uint32_t rev, uint32_t clk_div, uint8_t addr7, uint8_t reg, uint8_t val) {
    uint32_t sta = 0;
    i2c0_reset(base, rev, clk_div);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_START | ((uint32_t)addr7 << 1));
    mmio_write32(base + REG_MTXFIFO, (uint32_t)reg);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_STOP | (uint32_t)val);

    int rc = i2c0_wait_xen(base, &sta);
    if (rc != 0) {
        printf("[HWINIT] write8(0x%02x, 0x%02x, 0x%02x) err=%d smsta=0x%08x\n", addr7, reg, val, rc, sta);
        i2c0_reset(base, rev, clk_div);
        return rc;
    }
    return 0;
}

static int i2c0_read_reg16be(uint64_t base, uint32_t rev, uint32_t clk_div, uint8_t addr7, uint16_t reg16, uint8_t *out_val) {
    uint32_t sta = 0;
    i2c0_reset(base, rev, clk_div);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_START | ((uint32_t)addr7 << 1));
    mmio_write32(base + REG_MTXFIFO, (uint32_t)((reg16 >> 8) & 0xffu));
    mmio_write32(base + REG_MTXFIFO, (uint32_t)(reg16 & 0xffu));
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_START | ((uint32_t)addr7 << 1) | 1u);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_READ | MTXFIFO_STOP | 1u);

    int rc = i2c0_wait_xen(base, &sta);
    if (rc != 0) {
        printf("[HWINIT] read16be(0x%02x, 0x%04x) err=%d smsta=0x%08x\n", addr7, reg16, rc, sta);
        i2c0_reset(base, rev, clk_div);
        return rc;
    }
    uint32_t rd = mmio_read32(base + REG_MRXFIFO);
    if (rd & MRXFIFO_EMPTY) {
        printf("[HWINIT] read16be RX empty rd=0x%08x\n", rd);
        return -3;
    }
    *out_val = (uint8_t)(rd & 0xffu);
    return 0;
}

static int i2c0_write_reg16be(uint64_t base, uint32_t rev, uint32_t clk_div, uint8_t addr7, uint16_t reg16, uint8_t val) {
    uint32_t sta = 0;
    i2c0_reset(base, rev, clk_div);
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_START | ((uint32_t)addr7 << 1));
    mmio_write32(base + REG_MTXFIFO, (uint32_t)((reg16 >> 8) & 0xffu));
    mmio_write32(base + REG_MTXFIFO, (uint32_t)(reg16 & 0xffu));
    mmio_write32(base + REG_MTXFIFO, MTXFIFO_STOP | (uint32_t)val);

    int rc = i2c0_wait_xen(base, &sta);
    if (rc != 0) {
        printf("[HWINIT] write16be(0x%02x, 0x%04x, 0x%02x) err=%d smsta=0x%08x\n", addr7, reg16, val, rc, sta);
        i2c0_reset(base, rev, clk_div);
        return rc;
    }
    return 0;
}

static void hwinit_op(int do_enable) {
    uint64_t base = gIOBase + I2C0_OFF;
    if (i2c_ensure_clock(PMGR_I2C0_OFF, "I2C0") != 0) return;

    uint32_t rev = mmio_read32(base + REG_REV);
    uint32_t ctl = mmio_read32(base + REG_CTL);
    uint32_t clk_div = ctl & CTL_CLK_M;
    if (clk_div == 0) clk_div = 0x04;
    printf("[HWINIT] i2c0=0x%llx REV=0x%x CTL=0x%08x clk_div=0x%02x\n",
           (unsigned long long)base, rev, ctl, clk_div);

    uint8_t ch_old = 0;
    if (i2c0_read_reg8(base, rev, clk_div, CHESTNUT_ADDR, CHESTNUT_REG05, &ch_old) == 0) {
        printf("[HWINIT] CHESTNUT (0x27) reg 0x05 BEFORE = 0x%02x\n", ch_old);
        if (do_enable) {
            uint8_t ch_tgt = (uint8_t)(ch_old | 0x17u);
            if (ch_tgt != ch_old && i2c0_write_reg8(base, rev, clk_div, CHESTNUT_ADDR, CHESTNUT_REG05, ch_tgt) == 0) {
                spin(2000);
                uint8_t ch_new = 0;
                if (i2c0_read_reg8(base, rev, clk_div, CHESTNUT_ADDR, CHESTNUT_REG05, &ch_new) == 0) {
                    printf("[HWINIT] CHESTNUT (0x27) reg 0x05 AFTER  = 0x%02x (wrote 0x%02x)\n", ch_new, ch_tgt);
                }
            }
        }
    }

    uint8_t pm_old = 0;
    if (i2c0_read_reg16be(base, rev, clk_div, D2255_ADDR, D2255_WLAN_REG, &pm_old) == 0) {
        printf("[HWINIT] D2255 (0x74) reg 0x0914 (WLAN_REG_ON) BEFORE = 0x%02x\n", pm_old);
        if (do_enable) {
            uint8_t pm_base = (pm_old >= 0x40u) ? pm_old : 0x80u;
            uint8_t pm_tgt = (uint8_t)((pm_base & ~0x19u) | 0x01u);
            if (i2c0_write_reg16be(base, rev, clk_div, D2255_ADDR, D2255_WLAN_REG, pm_tgt) == 0) {
                spin(50000);
                uint8_t pm_new = 0;
                if (i2c0_read_reg16be(base, rev, clk_div, D2255_ADDR, D2255_WLAN_REG, &pm_new) == 0) {
                    printf("[HWINIT] D2255 (0x74) reg 0x0914 (WLAN_REG_ON) AFTER  = 0x%02x (wrote 0x%02x)\n", pm_new, pm_tgt);
                }
            }
        }
    }

    uint8_t bt_old = 0;
    if (i2c0_read_reg16be(base, rev, clk_div, D2255_ADDR, D2255_BT_REG, &bt_old) == 0) {
        printf("[HWINIT] D2255 (0x74) reg 0x0910 (BT_REG_ON) BEFORE = 0x%02x\n", bt_old);
        if (do_enable) {
            uint8_t bt_base = (bt_old >= 0x40u) ? bt_old : 0x80u;
            uint8_t bt_low  = (uint8_t)(bt_base & ~0x19u);
            uint8_t bt_high = (uint8_t)(bt_low | 0x01u);
            if (i2c0_write_reg16be(base, rev, clk_div, D2255_ADDR, D2255_BT_REG, bt_low) == 0) {
                spin(20000);
                if (i2c0_write_reg16be(base, rev, clk_div, D2255_ADDR, D2255_BT_REG, bt_high) == 0) {
                    spin(50000);
                    uint8_t bt_new = 0;
                    if (i2c0_read_reg16be(base, rev, clk_div, D2255_ADDR, D2255_BT_REG, &bt_new) == 0) {
                        printf("[HWINIT] D2255 (0x74) reg 0x0910 (BT_REG_ON) AFTER  = 0x%02x (0x%02x->0x%02x)\n",
                               bt_new, bt_low, bt_high);
                    }
                }
            }
        }
    }

    const uint16_t cam_regs[2] = { D2255_CAM_AVDD_LDO_CTRL_REG, D2255_CAM0_LDO_CTRL_REG };
    const char *cam_names[2] = { "LDO8(cam_avdd)", "LDO21(cam0)" };
    for (int i = 0; i < 2; i++) {
        uint8_t lv = 0;
        if (i2c0_read_reg16be(base, rev, clk_div, D2255_ADDR, cam_regs[i], &lv) == 0) {
            printf("[HWINIT] D2255 (0x74) reg 0x%04x (%s) BEFORE = 0x%02x\n", cam_regs[i], cam_names[i], lv);
            if (do_enable) {
                uint8_t ltgt = (uint8_t)(lv | 0x01u);
                if (ltgt != lv && i2c0_write_reg16be(base, rev, clk_div, D2255_ADDR, cam_regs[i], ltgt) == 0) {
                    spin(2000);
                    uint8_t lnew = 0;
                    if (i2c0_read_reg16be(base, rev, clk_div, D2255_ADDR, cam_regs[i], &lnew) == 0) {
                        printf("[HWINIT] D2255 (0x74) reg 0x%04x (%s) AFTER  = 0x%02x\n", cam_regs[i], cam_names[i], lnew);
                    }
                }
            }
        }
    }

    if (i2c_ensure_clock(PMGR_I2C1_OFF, "I2C1") == 0) {
        uint64_t base1 = gIOBase + I2C1_OFF;
        uint32_t rev1 = mmio_read32(base1 + REG_REV);
        uint32_t ctl1 = mmio_read32(base1 + REG_CTL);
        uint32_t clk_div1 = ctl1 & CTL_CLK_M;
        if (clk_div1 == 0) clk_div1 = 0x04;
        printf("[HWINIT] i2c1=0x%llx REV=0x%x CTL=0x%08x clk_div=0x%02x\n",
               (unsigned long long)base1, rev1, ctl1, clk_div1);

        if (do_enable) {
            uint8_t vreg = 0, r10 = 0;
            int vreg_ok = 1;
            if (i2c0_read_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_VREG_RD, &vreg) == 0) {
                unsigned vreg_mv = 3000u + (unsigned)vreg * 10u;
                printf("[HWINIT] SN2400 VREG=0x%02x (%umV)\n", vreg, vreg_mv);
                if (vreg_mv > 4380u) vreg_ok = 0;
            }
            if (vreg_ok) {
                i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_MASK1, 0xffu);
                i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_MASK2, 0xffu);
                i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_MASK3, 0xffu);
                if (i2c0_read_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_CTL, &r10) == 0) {
                    i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_CTL, (uint8_t)((r10 & ~0x08u) | 0x04u));
                }
                i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_ICHG, 0x32u);
                i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_IINLIM, 0x29u);
                printf("[HWINIT] SN2400 boot input limit set to 500mA (0x29/0x32)\n");
            }
        }

        uint8_t sn_old = 0;
        if (i2c0_read_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_HDQ_REG, &sn_old) == 0) {
            printf("[HWINIT] SN2400 (0x75) reg 0x1d (HDQ_MUX) BEFORE = 0x%02x\n", sn_old);
            if (do_enable) {
                if (i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_HDQ_REG, 0x04u) == 0) {
                    spin(15000);
                    if (i2c0_write_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_HDQ_REG, 0x06u) == 0) {
                        spin(5000);
                        uint8_t sn_new = 0;
                        if (i2c0_read_reg8(base1, rev1, clk_div1, SN2400_ADDR, SN2400_HDQ_REG, &sn_new) == 0) {
                            printf("[HWINIT] SN2400 (0x75) reg 0x1d (HDQ_MUX) AFTER  = 0x%02x (wrote 0x04->0x06)\n", sn_new);
                        }
                    }
                }
            }
        }
    }
}

static void draw_bootlogo(void) {
    if (!gFramebuffer || !gWidth || !gHeight || !gRowPixels) {
        printf("[LOGO] Framebuffer unavailable.\n");
        return;
    }

    uint64_t depth = gBootArgs ? (gBootArgs->Video.v_depth & 0xFFu) : 32u;
    int is_30bit = (depth == 30u);

    uint32_t *base_fb = *(uint32_t *volatile *)((uintptr_t)&gFramebuffer + 0x10u);
    int have_base_fb = (base_fb != NULL && base_fb != gFramebuffer && ((uintptr_t)base_fb >> 32) != 0);

    size_t fb_pixels = (size_t)gRowPixels * (size_t)gHeight;
    for (size_t i = 0; i < fb_pixels; i++) {
        gFramebuffer[i] = 0u;
    }
    if (have_base_fb) {
        for (size_t i = 0; i < fb_pixels; i++) {
            base_fb[i] = 0u;
        }
    }

#if BOOTLOGO_CORNER_MODE
    uint32_t ox = (gWidth > BOOTLOGO_W + 24u) ? (gWidth - BOOTLOGO_W - 24u) : 0u;
    uint32_t oy = (gHeight > BOOTLOGO_H + 140u) ? (gHeight - BOOTLOGO_H - 140u) : ((gHeight > BOOTLOGO_H) ? (gHeight - BOOTLOGO_H) : 0u);
#else
    uint32_t ox = (gWidth > BOOTLOGO_W) ? ((gWidth - BOOTLOGO_W) / 2u) : 0u;
    uint32_t oy = (gHeight > BOOTLOGO_H) ? ((gHeight - BOOTLOGO_H) / 2u) : 0u;
#endif

    uint32_t px = 0, py = 0;
    for (uint32_t i = 0; i + 1u < BOOTLOGO_RLE_LEN; i += 2u) {
        uint32_t count = g_bootlogo_rle[i];
        uint8_t luma = g_bootlogo_rle[i + 1u];
        uint32_t pix = 0u;
        if (luma) {
            if (is_30bit) {
                uint32_t c10 = ((uint32_t)luma << 2) | ((uint32_t)luma >> 6);
                pix = (c10 << 20) | (c10 << 10) | c10;
            } else {
                pix = 0xFF000000u | ((uint32_t)luma << 16) | ((uint32_t)luma << 8) | (uint32_t)luma;
            }
        }
        while (count--) {
            if (luma && py < BOOTLOGO_H) {
                uint32_t dx = ox + px;
                uint32_t dy = oy + py;
                if (dx < gWidth && dy < gHeight) {
                    size_t idx = (size_t)dy * (size_t)gRowPixels + (size_t)dx;
                    gFramebuffer[idx] = pix;
                    if (have_base_fb) {
                        base_fb[idx] = pix;
                    }
                }
            }
            px++;
            if (px >= BOOTLOGO_W) {
                px = 0;
                py++;
            }
        }
    }

    cache_clean_and_invalidate(gFramebuffer, fb_pixels * 4u);
    printf("[LOGO] yigitzra1n drawn cleanly (%ux%u stride=%u depth=%llu base_fb=%d)\n",
           gWidth, gHeight, gRowPixels, (unsigned long long)depth, have_base_fb);
}

static void cmd_bootlogo(const char *cmd, char *args) {
    (void)cmd;
    (void)args;
    draw_bootlogo();
}

static void cmd_hwinit(const char *cmd, char *args) {
    (void)cmd;
    if (args && args[0] == 'e') {
        hwinit_op(1);
    } else {
        hwinit_op(0);
    }
}

void module_entry(void) {
    command_register("hwinit", "N71m hwinit: Touch + WLAN + BT + CamLDO + SN2400 (usage: hwinit [read|enable])", cmd_hwinit);
    command_register("bootlogo", "Draw custom yigitzra1n framebuffer boot logo", cmd_bootlogo);
    printf("[yigitzra1n] module ready (hwinit + custom bootlogo).\n");
}
