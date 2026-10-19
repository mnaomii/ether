/* Checks buf.c against shared/vectors.txt.
 * Encodes the server->client vectors, decodes the client->server ones.
 * Usage: test_vectors [path/to/vectors.txt] */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "buf.h"
#include "proto.h"

#define SID  0x9A1F0001u
#define T_MS 1700000000000ull

struct vec {
    char name[32];
    uint8_t b[256];
    size_t len;
};

static struct vec vecs[64];
static int n_vecs, failures;

static void load(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        perror(path);
        exit(2);
    }
    char line[1024];
    while (fgets(line, sizeof line, f) && n_vecs < 64) {
        char *bar = strchr(line, '|');
        if (line[0] == '#' || !bar)
            continue;
        struct vec *v = &vecs[n_vecs++];
        sscanf(line, "%31s", v->name);
        char *p = bar + 1;
        unsigned x;
        int used;
        while (v->len < sizeof v->b && sscanf(p, "%2x%n", &x, &used) == 1) {
            v->b[v->len++] = (uint8_t)x;
            p += used;
        }
    }
    fclose(f);
}

static const struct vec *get(const char *name)
{
    for (int i = 0; i < n_vecs; i++)
        if (strcmp(vecs[i].name, name) == 0)
            return &vecs[i];
    fprintf(stderr, "missing vector %s\n", name);
    exit(2);
}

static void check(const char *name, int ok)
{
    printf("%s %s\n", ok ? "PASS" : "FAIL", name);
    failures += !ok;
}

static void check_bytes(const char *name, struct wbuf *w)
{
    const struct vec *v = get(name);
    check(name, w->len == v->len && memcmp(w->data, v->b, v->len) == 0);
    wbuf_free(w);
}

static struct rbuf reader(const char *name)
{
    const struct vec *v = get(name);
    return (struct rbuf){ v->b, v->len, 0 };
}

static void tcp_frame(struct wbuf *out, uint8_t type, struct wbuf *payload)
{
    put_u32(out, (uint32_t)(1 + payload->len));
    put_u8(out, type);
    put_bytes(out, payload->data, payload->len);
    wbuf_free(payload);
}

static void udp_header(struct wbuf *out, uint8_t type, uint32_t sid)
{
    put_u16(out, UDP_MAGIC);
    put_u8(out, UDP_VERSION);
    put_u8(out, type);
    put_u32(out, sid);
}

static int read_udp_header(struct rbuf *r, uint8_t type, uint32_t sid)
{
    return get_u16(r) == UDP_MAGIC && get_u8(r) == UDP_VERSION && get_u8(r) == type && get_u32(r) == sid;
}

static void test_encode(void)
{
    struct wbuf p = {0}, f = {0};

    put_u32(&p, SID);
    put_u16(&p, 2);
    put_u16(&p, 1);
    put_str(&p, "general");
    put_u16(&p, 2);
    put_str(&p, "misc");
    tcp_frame(&f, T_WELCOME, &p);
    check_bytes("tcp_welcome", &f);

    put_u16(&p, 1);
    put_u64(&p, T_MS);
    put_str(&p, "bob");
    put_str(&p, "salut");
    tcp_frame(&f, T_MSG, &p);
    check_bytes("tcp_msg", &f);

    put_u16(&p, E_BAD_PASSWORD);
    put_str(&p, "wrong password");
    tcp_frame(&f, T_ERROR, &p);
    check_bytes("tcp_error_bad_pass", &f);

    udp_header(&f, U_DISCOVER_REPLY, 0);
    put_str(&f, "ether-1");
    put_u16(&f, TCP_PORT);
    put_u16(&f, 3);
    check_bytes("udp_discover_reply", &f);

    udp_header(&f, U_PONG, SID);
    put_u64(&f, T_MS);
    check_bytes("udp_pong", &f);
}

static void test_decode(void)
{
    char a[MAX_NICK + 1], b[MAX_TEXT + 1];
    struct rbuf r;
    int ok;

    r = reader("tcp_login_ana");
    ok = get_u32(&r) == 14 && get_u8(&r) == T_LOGIN
        && get_str(&r, a, sizeof a) == 3 && strcmp(a, "ana") == 0
        && get_str(&r, b, sizeof b) == 6 && strcmp(b, "parola") == 0;
    check("tcp_login_ana", ok && r.left == 0 && !r.err);

    r = reader("tcp_join_1");
    ok = get_u32(&r) == 3 && get_u8(&r) == T_JOIN && get_u16(&r) == 1;
    check("tcp_join_1", ok && r.left == 0 && !r.err);

    r = reader("tcp_say_1_salut");
    ok = get_u32(&r) == 10 && get_u8(&r) == T_SAY && get_u16(&r) == 1
        && get_str(&r, b, sizeof b) == 5 && strcmp(b, "salut") == 0;
    check("tcp_say_1_salut", ok && r.left == 0 && !r.err);

    r = reader("tcp_say_2_buna");
    ok = get_u32(&r) == 10 && get_u8(&r) == T_SAY && get_u16(&r) == 2
        && get_str(&r, b, sizeof b) == 5 && strcmp(b, "bun\xC4\x83") == 0;
    check("tcp_say_2_buna", ok && r.left == 0 && !r.err);

    r = reader("tcp_bye");
    ok = get_u32(&r) == 1 && get_u8(&r) == T_BYE;
    check("tcp_bye", ok && r.left == 0 && !r.err);

    r = reader("udp_discover");
    ok = read_udp_header(&r, U_DISCOVER, 0);
    check("udp_discover", ok && r.left == 0 && !r.err);

    r = reader("udp_typing");
    ok = read_udp_header(&r, U_TYPING, SID) && get_u16(&r) == 1;
    check("udp_typing", ok && r.left == 0 && !r.err);

    r = reader("udp_ping");
    ok = read_udp_header(&r, U_PING, SID) && get_u64(&r) == T_MS;
    check("udp_ping", ok && r.left == 0 && !r.err);

    /* Hardening: reads past the end must set err, never read garbage. */
    uint8_t one = 0x42;
    r = (struct rbuf){ &one, 1, 0 };
    get_u16(&r);
    check("overrun sets err", r.err != 0);

    r = reader("tcp_say_1_salut");
    get_u32(&r);
    get_u8(&r);
    get_u16(&r);
    check("get_str rejects small out buffer", get_str(&r, a, 4) == -1 && r.err);
}

int main(int argc, char **argv)
{
    load(argc > 1 ? argv[1] : "../shared/vectors.txt");
    test_encode();
    test_decode();
    printf("%d failed\n", failures);
    return failures != 0;
}
