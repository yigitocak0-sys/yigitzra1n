#include <stdint.h>

#define MAGIC_LO 0x54464f53u
#define MAGIC_HI 0x31534b41u

int soft_aks_wrap_key(const uint8_t *in_key, int in_len, uint32_t key_class,
                      int32_t handle, uint8_t *out_key, int *out_len, uint32_t *out_class) {
    (void)handle;
    if (!in_key || !out_key || !out_len || in_len <= 0) return (int)0xe00002c2u;
    int need = in_len + 8;
    if (need > *out_len) return (int)0xe00002c2u;
    for (int i = 0; i < in_len; i++) {
        out_key[i] = (uint8_t)(in_key[i] ^ 0x5Au);
    }
    uint32_t m0 = MAGIC_LO;
    uint32_t m1 = MAGIC_HI;
    out_key[in_len + 0] = (uint8_t)(m0 & 0xffu);
    out_key[in_len + 1] = (uint8_t)((m0 >> 8) & 0xffu);
    out_key[in_len + 2] = (uint8_t)((m0 >> 16) & 0xffu);
    out_key[in_len + 3] = (uint8_t)((m0 >> 24) & 0xffu);
    out_key[in_len + 4] = (uint8_t)(m1 & 0xffu);
    out_key[in_len + 5] = (uint8_t)((m1 >> 8) & 0xffu);
    out_key[in_len + 6] = (uint8_t)((m1 >> 16) & 0xffu);
    out_key[in_len + 7] = (uint8_t)((m1 >> 24) & 0xffu);
    *out_len = need;
    if (out_class) *out_class = key_class;
    return 0;
}

int soft_aks_unwrap_key(const uint8_t *in_key, int in_len, uint32_t key_class,
                        int32_t handle, uint8_t *out_key, int *out_len) {
    (void)key_class;
    (void)handle;
    if (!in_key || !out_key || !out_len || in_len <= 8) return (int)0xe00002c2u;
    int plain = in_len - 8;
    if (plain > *out_len) return (int)0xe00002c2u;
    uint32_t m0 = (uint32_t)in_key[plain + 0] | ((uint32_t)in_key[plain + 1] << 8) |
                  ((uint32_t)in_key[plain + 2] << 16) | ((uint32_t)in_key[plain + 3] << 24);
    uint32_t m1 = (uint32_t)in_key[plain + 4] | ((uint32_t)in_key[plain + 5] << 8) |
                  ((uint32_t)in_key[plain + 6] << 16) | ((uint32_t)in_key[plain + 7] << 24);
    if (m0 != MAGIC_LO || m1 != MAGIC_HI) return (int)0xe00002bcu;
    for (int i = 0; i < plain; i++) {
        out_key[i] = (uint8_t)(in_key[i] ^ 0x5Au);
    }
    *out_len = plain;
    return 0;
}
