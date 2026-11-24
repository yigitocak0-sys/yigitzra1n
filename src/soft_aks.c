#include <stdint.h>

#define MAGIC64 0x31534b4154464f53ULL
typedef uint64_t __attribute__((aligned(1))) u64_ua;

int soft_aks_wrap_key(const uint8_t *in_key, int in_len, uint32_t key_class,
                      int32_t handle, uint8_t *out_key, int *out_len, uint32_t *out_class) {
    (void)handle;
    if (!in_key || !out_key || !out_len || in_len <= 0)
        return (int)0xe00002c2u;
    int need = in_len + 8;
    if (need > *out_len)
        return (int)0xe00002c2u;
    const uint8_t *end = in_key + in_len;
    uint8_t *dst = out_key;
    while (in_key < end) *dst++ = *in_key++ ^ 0x5Au;
    *(u64_ua *)dst = MAGIC64;
    *out_len = need;
    if (out_class) *out_class = key_class;
    return 0;
}

int soft_aks_unwrap_key(const uint8_t *in_key, int in_len, uint32_t key_class,
                        int32_t handle, uint8_t *out_key, int *out_len) {
    (void)key_class;
    (void)handle;
    if (!in_key || !out_key || !out_len || in_len <= 8)
        return (int)0xe00002c2u;
    int plain = in_len - 8;
    if (plain > *out_len)
        return (int)0xe00002c2u;
    const uint8_t *end = in_key + plain;
    if (*(const u64_ua *)end != MAGIC64)
        return (int)0xe00002bcu;
    while (in_key < end) *out_key++ = *in_key++ ^ 0x5Au;
    *out_len = plain;
    return 0;
}
