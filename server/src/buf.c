#include "buf.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

void wbuf_free(struct wbuf *w)
{

    if( w == NULL ) return;

    free(w->data);

    w->data = NULL;
    w->len  = 0;
    w->cap  = 0;
}


void put_bytes(struct wbuf *w, const void *p, size_t n)
{
    if( w == NULL || n == 0 ) return;

    if(w->data == NULL) {
        w->data = malloc(64);
        w->cap = 64;
        if(!w->data) { perror("[ERR:put_bytes()] Memory alloc failed"); exit(1); }
    }

    while ( w->len + n > w->cap) {
        w->cap *= 2;
        w->data = realloc(w->data, w->cap);

        if(!w->data) { perror("[ERR:put_bytes()] Memory alloc failed"); exit(1);}
    }

    memcpy(w->data + w->len, p, n);
    w->len += n;
}

void put_u8(struct wbuf *w, uint8_t v)
{
    if( w == NULL ) return;

    put_bytes(w, &v, 1);
}

void put_u16(struct wbuf *w, uint16_t v)
{
    if( w == NULL ) return;

    int size = 2;
    uint8_t buff[2];

    // big-endian: fill from the last byte, most significant ends up first (0x13BA -> 13 BA)
    for(int idx = size - 1; idx > -1; --idx){
        buff[idx] = (uint8_t)(v & 0xFF);
        v >>= 8;
    }

    put_bytes(w,buff,(size_t)size);

}

void put_u32(struct wbuf *w, uint32_t v)
{
    if( w == NULL ) return;

    int size = 4;
    uint8_t buff[4];

    for(int idx = size - 1; idx > -1; --idx){
        buff[idx] = (uint8_t)(v & 0xFF);
        v >>= 8;
    }

    put_bytes(w,buff,(size_t)size);
}

void put_u64(struct wbuf *w, uint64_t v)
{
    if( w == NULL ) return;
    int size = 8;
    uint8_t buff[8];

    for(int idx = size - 1; idx > -1; --idx){
        buff[idx] = (uint8_t)(v & 0xFF);
        v >>= 8;
    }

    put_bytes(w,buff,(size_t)size);
}


void put_str(struct wbuf *w, const char *s)
{
    if( s == NULL || w == NULL) return;

    size_t n = strlen(s);

    put_u16(w, (uint16_t)n);
    put_bytes(w, s, n);
}


// rbuf below

uint8_t get_u8(struct rbuf *r)
{

    size_t size = 1;
    uint8_t payload = 0;

    // err sticks: once set, every later read returns 0 -> check r->err once at the end
    if(r == NULL || r->err || r->p == NULL) return 0;
    if(r->left < size) { r->err = 1; return 0;}

    payload = *(r->p);

    r->p++;
    r->left -= size;

    return payload;
}

uint16_t get_u16(struct rbuf *r)
{
    size_t size = 2;
    uint16_t payload = 0;

    if(r == NULL || r->err || r->p == NULL) return 0;
    if(r->left < size) { r->err = 1; return 0;}

    for(size_t tmp = size; tmp > 0; --tmp){
        payload = (payload << 8) | *(r->p); // make room, OR in the next byte: 13 BA -> 0x13BA
        r->p++;
    }

    r->left -= size;

    return payload;
}

uint32_t get_u32(struct rbuf *r)
{
    size_t size = 4;
    uint32_t payload = 0;

    if(r == NULL || r->err || r->p == NULL) return 0;
    if(r->left < size) { r->err = 1; return 0;}

    for(size_t tmp = size; tmp > 0; --tmp){
        payload = (payload << 8) | *(r->p);
        r->p++;
    }

    r->left -= size;

    return payload;
}

uint64_t get_u64(struct rbuf *r)
{
    size_t size = 8;
    uint64_t payload = 0;

    if(r == NULL || r->err || r->p == NULL) return 0;
    if(r->left < size) { r->err = 1; return 0;}

    for(size_t tmp = size; tmp > 0; --tmp){
        payload = (payload << 8) | *(r->p);
        r->p++;
    }

    r->left -= size;

    return payload;
}

int get_str(struct rbuf *r, char *out, size_t cap)
{
    if(r == NULL || r->err || r->p == NULL) return -1;

    uint16_t size = get_u16(r);
    if(r->err) return -1;

    if(r->left < size || cap <= size) { r->err = 1; return -1;} // cut off, or no room left for '\0'
    

    memcpy(out,r->p,size);
    out[size] = '\0';

    r->left -= size;
    r->p += size;

    return size;
}
