/* UDP datagrams: discovery, typing relay, ping. */
#include "server.h"
#include <sys/socket.h>

/* DISCOVER -> DISCOVER_REPLY (str server_name, u16 tcp_port, u16 users_online) unicast to src. */
static void on_discover(struct server *s, const struct sockaddr_in *src)
{
    size_t count = 0;
    for(int i = 0; i < MAX_CLIENTS; ++i)
        if(s->clients[i].st == ST_CONTROL) ++count;
    
    struct wbuf f = {0};
    put_str(&f, s->name);
    put_u16(&f, TCP_PORT);
    put_u16(&f, (uint16_t) count);

    udp_send(s,src,U_DISCOVER_REPLY, 0, &f);
    wbuf_free(&f);
}

// helper function

 static struct client* find_session(struct server *s, uint32_t sid){
    if(sid == 0) return NULL;

    for(int i = 0 ; i < MAX_CLIENTS; ++i)
        if(s->clients[i].session_id == sid && s->clients[i].st == ST_CONTROL) 
            return &s->clients[i];

    return NULL;
}

/* UDP_HELLO: remember src as the UDP address of the client with this session_id. */
static void on_udp_hello(struct server *s, uint32_t session_id, const struct sockaddr_in *src)
{
    struct client *c = find_session(s,session_id);
    if(c == NULL) return;

    c->has_udp = 1;
    c->udp_addr = *src;

}

/* TYPING u16 room_id -> TYPING_EVT (u16 room_id, str nick) to the other room members.
 * At most one relay per user per TYPING_MIN_MS. */
static void on_typing(struct server *s, uint32_t session_id, struct rbuf *r)
{
    struct client *c = find_session(s, session_id);
    if( c == NULL ) return;

    // is member check 

    uint16_t room_id = get_u16(r);
    if(r->err) return;

    int index = find_room(s, room_id);
    if(index < 0) return;

    uint64_t bit = 1ull << index;
    uint64_t was_member = c->rooms_mask & bit;

    if(!was_member) return;

    // rate limit check

    uint64_t now = now_ms();
    if( now - c->last_typing_ms < TYPING_MIN_MS) return;
    c->last_typing_ms = now;

    // sending packet


    struct wbuf f = {0};
    put_u16(&f, room_id);
    put_str(&f, c->nick);

    for(size_t i = 0; i < MAX_CLIENTS; ++i){
        struct client *c1 = &s->clients[i];
        if(c1 == c || !(c1->rooms_mask & bit) || c1->st != ST_CONTROL || !c1->has_udp)
            continue;
        udp_send(s,&c1->udp_addr, U_TYPING_EVT, c1->session_id, &f);
    }

    wbuf_free(&f);

}

/* PING u64 client_time_ms -> PONG with the same value, back to src. */
static void on_ping(struct server *s, uint32_t session_id, struct rbuf *r, const struct sockaddr_in *src)
{
    uint64_t value = get_u64(r);
    if(r->err) return;

    struct wbuf w = {0};
    
    put_u64(&w, value);
    udp_send(s, src, U_PONG, session_id, &w);

    wbuf_free(&w);
}

void handle_udp(struct server *s, const uint8_t *pkt, size_t len, const struct sockaddr_in *src)
{
    if(pkt == NULL) return;

    struct rbuf f = {pkt, len , 0};

    if(get_u16(&f) != UDP_MAGIC || get_u8(&f) != UDP_VERSION)
        return;

    uint8_t type = get_u8(&f);
    uint32_t sid = get_u32(&f);

    if( f.err ) return;

    switch(type){
        case U_DISCOVER: on_discover(s,src); return;
        case U_PING: on_ping(s,sid,&f,src); return;
        case U_TYPING: on_typing(s,sid,&f); return;
        case U_UDP_HELLO: on_udp_hello(s,sid,src); return;
        default: return;
    }

}

void udp_send(struct server *s, const struct sockaddr_in *dst, uint8_t type,
              uint32_t session_id, const struct wbuf *payload)
{
    struct wbuf f = {0};
     
    // UDP_MAGIC = "ET" - marks ownership of the package to Ether

    put_u16(&f, UDP_MAGIC);
    put_u8(&f, UDP_VERSION);
    put_u8(&f, type);
    put_u32(&f, session_id);

    if(payload){
        put_bytes(&f, payload->data, payload->len);
    }

    if (sendto(s->udp_fd, f.data, f.len, 0, (const struct sockaddr * )dst, sizeof(*dst)) < 0)
         perror("[ERR:udp_send()] sendto failed"); 

    wbuf_free(&f);

}
