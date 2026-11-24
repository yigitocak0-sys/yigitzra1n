#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#include <dlfcn.h>

static id (*orig_requestModel)(id, SEL) = NULL;

static id n71m_requestModel(id self, SEL _cmd) {
    if (orig_requestModel) {
        id existing = orig_requestModel(self, _cmd);
        if ([existing isKindOfClass:[NSString class]] && [(NSString *)existing length] > 0) {
            return existing;
        }
    }
    return @"A1905";
}

__attribute__((constructor))
static void n71m_tipsmodel_init(void) {
    @autoreleasepool {
        dlopen("/System/Library/PrivateFrameworks/TipsCore.framework/TipsCore", RTLD_LAZY);
        Class dm = objc_getClass("TPSDefaultsManager");
        if (!dm) return;
        Method m = class_getClassMethod(dm, sel_registerName("requestModel"));
        if (!m) return;
        orig_requestModel = (id (*)(id, SEL))method_setImplementation(m, (IMP)n71m_requestModel);
    }
}
