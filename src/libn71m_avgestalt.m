#import <Foundation/Foundation.h>
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>

static uintptr_t decode_adrp(uintptr_t pc, uint32_t w) {
    int64_t imm = (((w >> 5) & 0x7ffff) << 2) | ((w >> 29) & 3);
    if (imm & (1 << 20)) imm -= (1 << 21);
    return (pc & ~0xfffULL) + (imm << 12);
}

static void **decode_adrp_str64(uintptr_t pc) {
    uint32_t w0 = *(uint32_t *)pc;
    uint32_t w1 = *(uint32_t *)(pc + 4);
    if ((w0 & 0x9f000000) != 0x90000000) return NULL;
    if ((w1 & 0xffc00000) != 0xf9000000) return NULL;
    int rd0 = w0 & 31;
    int rn1 = (w1 >> 5) & 31;
    if (rd0 != rn1) return NULL;
    uintptr_t page = decode_adrp(pc, w0);
    uint32_t imm = ((w1 >> 10) & 0xfff) * 8;
    return (void **)(page + imm);
}

__attribute__((constructor))
static void n71m_avgestalt_init(void) {
    @autoreleasepool {
        void *avfc = dlopen("/System/Library/PrivateFrameworks/AVFCapture.framework/AVFCapture", RTLD_LAZY);
        if (!avfc) return;
        BOOL (*getBool)(CFStringRef) = (BOOL (*)(CFStringRef))dlsym(avfc, "AVGestaltGetBoolAnswer");
        if (!getBool) return;

        if (getBool(CFSTR("AVGQCameraCapability"))) {
            return;
        }

        uintptr_t fn = (uintptr_t)getBool;
        uint32_t w44 = *(uint32_t *)(fn + 0x44);
        uint32_t w48 = *(uint32_t *)(fn + 0x48);
        if ((w44 & 0x9f000000) != 0x90000000 || (w48 & 0xff800000) != 0x91000000) return;

        uintptr_t bpage = decode_adrp(fn + 0x44, w44);
        uintptr_t blk_addr = bpage + ((w48 >> 10) & 0xfff);
        struct blk { void *isa; int flags; int res; void *invoke; } *b = (struct blk *)blk_addr;
        if (!b || !b->invoke) return;

        uintptr_t inv = (uintptr_t)b->invoke;
        uint32_t blw = *(uint32_t *)(inv + 0x68);
        if ((blw & 0xfc000000) != 0x94000000) return;
        int64_t blim = blw & 0x3ffffff;
        if (blim & (1 << 25)) blim -= (1 << 26);
        uintptr_t helper = (inv + 0x68) + blim * 4;

        void **pBool = decode_adrp_str64(inv + 0x1c);
        void **pInt  = decode_adrp_str64(inv + 0x2c);
        void **pFlt  = decode_adrp_str64(inv + 0x3c);
        void **pStr  = decode_adrp_str64(inv + 0x4c);
        if (!pBool || !pInt || !pFlt || !pStr) return;
        if (!*pBool || !*pInt || !*pFlt || !*pStr) return;

        NSMutableDictionary *dBool = (__bridge NSMutableDictionary *)(*pBool);
        NSMutableDictionary *dInt  = (__bridge NSMutableDictionary *)(*pInt);
        NSMutableDictionary *dFlt  = (__bridge NSMutableDictionary *)(*pFlt);
        NSMutableDictionary *dStr  = (__bridge NSMutableDictionary *)(*pStr);

        void (*call_helper)(NSString *, NSMutableDictionary *, NSMutableDictionary *, NSMutableDictionary *, NSMutableDictionary *) =
            (void (*)(NSString *, NSMutableDictionary *, NSMutableDictionary *, NSMutableDictionary *, NSMutableDictionary *))helper;

        call_helper(@"D20", dBool, dInt, dFlt, dStr);

        dBool[@"AVGQDeviceSupportsP3ColorspaceVideoRecording"] = @NO;
        dBool[@"AVGQHEVCEncodingCapability"] = @NO;
        dBool[@"AVGQSphereCapability"] = @NO;

        dInt[@"AVGQRearFacingCameraVideoCapture4kMaxFPS"] = @30;
        dInt[@"AVGQRearFacingCameraHFRVideoCapture1080pMaxFPS"] = @120;
        dInt[@"AVGQRearFacingCameraHFRVideoCapture720pMaxFPS"] = @240;
        dInt[@"AVGQFrontFacingCameraVideoCapture1080pMaxFPS"] = @0;
        dInt[@"AVGQFrontFacingCameraVideoCapture720pMaxFPS"] = @30;
        dInt[@"AVGQRearFacingWideCameraFocalLengthIn35mm"] = @29;
    }
}
