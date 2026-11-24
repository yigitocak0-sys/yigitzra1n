#include <sys/types.h>
#include <sys/socket.h>
#include <netdb.h>
#include <string.h>

#define DYLD_INTERPOSE(_replacement, _replacee) \
   __attribute__((used)) static struct { const void *replacement; const void *replacee; } _interpose_##_replacee \
   __attribute__((section("__DATA,__interpose"))) = { (const void *)(unsigned long)&_replacement, (const void *)(unsigned long)&_replacee };

int my_getaddrinfo(const char *hostname, const char *servname, const struct addrinfo *hints, struct addrinfo **res) {
    if (hostname) {
        if (strcmp(hostname, "updates.cdn-apple.com") == 0) {
            hostname = "17.253.39.134";
        } else if (strcmp(hostname, "api.ipsw.me") == 0) {
            hostname = "104.26.9.156";
        }
    }
    return getaddrinfo(hostname, servname, hints, res);
}
DYLD_INTERPOSE(my_getaddrinfo, getaddrinfo)
