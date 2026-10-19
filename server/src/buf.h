/* Big-endian encode/decode helpers. Strings are [u16 byte_len][UTF-8 bytes]. */
#ifndef ETHER_BUF_H
#define ETHER_BUF_H

#include <stddef.h>
#include <stdint.h>

/* Growable output buffer. Zero-initialise before first use. */
struct wbuf {
    uint8_t *data;
    size_t len, cap;
};

void wbuf_free(struct wbuf *w);
void put_u8(struct wbuf *w, uint8_t v);
void put_u16(struct wbuf *w, uint16_t v);
void put_u32(struct wbuf *w, uint32_t v);
void put_u64(struct wbuf *w, uint64_t v);
void put_bytes(struct wbuf *w, const void *p, size_t n);
/* Appends [u16 strlen(s)][bytes of s], no NUL. */
void put_str(struct wbuf *w, const char *s);

/* Read cursor over a received payload. Once a read would run past the end,
 * err is set and stays set; later reads return 0. Check err once at the end. */
struct rbuf {
    const uint8_t *p;
    size_t left;
    int err;
};

uint8_t  get_u8(struct rbuf *r);
uint16_t get_u16(struct rbuf *r);
uint32_t get_u32(struct rbuf *r);
uint64_t get_u64(struct rbuf *r);
/* Reads a [u16 len][bytes] string into out (NUL-terminated).
 * Returns the byte length, or -1 (and sets err) if it overruns the buffer or len >= cap. */
int get_str(struct rbuf *r, char *out, size_t cap);

#endif
