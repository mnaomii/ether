/* TCP control messages. */
#include "server.h"
#include <sys/random.h>
#include <string.h>


// ----- helpers

 int find_room(struct server *s, uint16_t id){
    for(size_t i = 0; i < s->n_rooms; ++i)
        if(s->rooms[i].id == id) return i; // index = bit number in rooms_mask

    return -1;
}

// MSG payload layout in one place: used by MSG (on_say) and every HISTORY entry (on_join)
static void put_msg(struct wbuf *w, uint16_t room_id, const struct msg *m){
    put_u16(w, room_id);
    put_u64(w, m->unix_ms);
    put_str(w, m->from);
    put_str(w, m->text);

}

// ------ helpers end

/* Append m to the room's ring buffer, overwriting the oldest after HISTORY_LEN. */
static void room_push(struct room *r, const struct msg *m)
{
    r->history[r->head] = *m; // saves the message into the buffer;
    r->head = (r->head + 1) % HISTORY_LEN;

    if(r->count < HISTORY_LEN) r->count++;
}

/* LOGIN str nickname, str password -> WELCOME, or ERROR 1/7/9. NEW -> CONTROL. */
static void on_login(struct server *s, struct client *c, struct rbuf *r)
{
    char nick[MAX_NICK+1], pass[MAX_PASS+1];
    
    get_str(r, nick, MAX_NICK+1);
    get_str(r, pass, MAX_PASS+1);

    if(r->err) {send_error(c, E_BAD_CREDENTIALS, "incorrect credentials"); return;}

    enum acc_result code = account_login(s,nick,pass);
    switch(code){
        case ACC_BAD_FORMAT: send_error(c, E_BAD_CREDENTIALS, "bad credentials"); return;
        case ACC_BAD_PASS: send_error(c, E_BAD_PASSWORD, "wrong password"); return;
        default: break;
    }

    // after any error above the conn stays ST_NEW: the client may send LOGIN again
    for(int i = 0; i < MAX_CLIENTS; ++i )
        if(s->clients[i].st == ST_CONTROL && strcmp(nick, s->clients[i].nick) == 0)
            {send_error(c,E_NICK_ONLINE, "user already online"); return;}

    uint32_t sid = 0;
    while(sid == 0 ) getrandom(&sid, sizeof(sid), 0); // never 0: 0 = "no session" on UDP
    c->session_id = sid;

    strcpy(c->nick,nick);
    c->st = ST_CONTROL;

    struct wbuf f = {0};
    put_u32(&f,sid);
    put_u16(&f,(uint16_t)s->n_rooms);

    for(int i = 0; i < s->n_rooms; ++i){
        put_u16(&f, s->rooms[i].id);
        put_str(&f, s->rooms[i].name);
    }

    send_frame(c,T_WELCOME, &f);
    wbuf_free(&f);
}

/* JOIN u16 room_id -> HISTORY + USERS to c, USER_EVT joined to the others. ERROR 2 if no such room. */
static void on_join(struct server *s, struct client *c, struct rbuf *r)
{
    uint16_t id = get_u16(r);

    if(r->err) return;

    int idx = find_room(s, id);
    if( idx < 0 ) { send_error(c,E_BAD_ROOM,"no such room"); return;}

    uint64_t bit = 1ull << idx;
    uint64_t was_member = c->rooms_mask & bit;

    c->rooms_mask |= bit;

    // msg history, room members & user join broadcast
    struct wbuf history = {0}, users = {0}, user_evt = {0};

    // user evt build
    put_u16(&user_evt, id);
    put_u8(&user_evt,1);
    put_str(&user_evt, c->nick);


    // history build
    struct room *rm = &s->rooms[idx];
    put_u16(&history,id);
    put_u16(&history,(uint16_t)rm->count);

    // slot of the oldest msg; +HISTORY_LEN keeps it positive before the %
    int msg_start = (rm->head - rm->count + HISTORY_LEN) % HISTORY_LEN;
    for(int i = 0; i < rm->count; ++i){
        put_msg(&history, id, &rm->history[(msg_start+i)%HISTORY_LEN]);
    }


    // users build

    size_t no_users = 0;
    for(int i=0; i< MAX_CLIENTS; ++i){
        struct client *c = &s->clients[i];
        if(c->st == ST_CONTROL && (c->rooms_mask & bit)) ++no_users;
    }

    put_u16(&users, id);
    put_u16(&users,(uint16_t)no_users);

    for(size_t i = 0; i < MAX_CLIENTS; ++i){
        struct client *c = &s->clients[i];
        if (c->st == ST_CONTROL && (c->rooms_mask & bit)) 
            put_str(&users, c->nick);
    }
    
    send_frame(c, T_HISTORY, &history); // order matters: HISTORY first, then USERS
    send_frame(c, T_USERS, &users);

    if( !was_member ) // broadcast join
        broadcast_room(s, id, c, T_USER_EVT, &user_evt);

    wbuf_free(&history);
    wbuf_free(&users);
    wbuf_free(&user_evt);

}

/* LEAVE u16 room_id -> USER_EVT left to the others. */
static void on_leave(struct server *s, struct client *c, struct rbuf *r)
{
     uint16_t id = get_u16(r);

    if(r->err) return;

    int idx = find_room(s, id);
    if( idx < 0 ) { send_error(c,E_BAD_ROOM,"no such room"); return;}

    uint64_t bit = 1ull << idx;
    uint64_t was_member = c->rooms_mask & bit;

    if(!was_member) return;

    c->rooms_mask &= ~bit;


    struct wbuf user_evt = {0};
    
    // user evt build
    put_u16(&user_evt, id);
    put_u8(&user_evt,0);
    put_str(&user_evt, c->nick);

    broadcast_room(s, id, c, T_USER_EVT, &user_evt);

    wbuf_free(&user_evt);
}

/* SAY u16 room_id, str text -> MSG to every member, sender included. ERROR 2 / 8. */
static void on_say(struct server *s, struct client *c, struct rbuf *r)
{
    uint16_t id = get_u16(r);
    if(r->err) return;

    int idx = find_room(s,id);

    if(idx < 0) { send_error(c, E_BAD_ROOM, "not in room"); return;}
    if (!(c->rooms_mask & (1ULL <<idx))) {
        send_error(c, E_BAD_ROOM, "not in room"); return;
    }

    // due to the design of the frame, if rbuf's data exceeds the cap we cannot
    // retrieve the length, hence we rebuild the uin16_t length using bit operations
    if( r->left >= 2 && ((r->p[0] << 8) | r->p[1]) > MAX_TEXT){
            send_error(c, E_TEXT_TOO_LONG, "text too long");
            return;
    }

    struct msg m = {0};
    get_str(r, m.text, sizeof(m.text));

    if(r->err) return;

    m.unix_ms = now_ms(); // time of sending the msg (server time = one order for everyone)
    strcpy(m.from, c->nick); // user who sent the msg

    room_push(&s->rooms[idx], &m);

    struct wbuf p = {0};
    put_msg(&p, id, &m);
    broadcast_room(s, id, NULL, T_MSG, &p); // NULL: the sender gets its own msg back too

    wbuf_free(&p);

}

/* FILE_UP_REQ u16 room_id, str filename, u64 size -> FILE_UP_OK, or ERROR 2 / 3. */
static void on_file_up_req(struct server *s, struct client *c, struct rbuf *r)
{
    uint16_t room = get_u16(r); 
    char filename[MAX_FILENAME + 1] ;
    get_str(r,filename, MAX_FILENAME + 1);
    uint64_t size = get_u64(r);

    if(r->err) return;

    // making a new file + transfer

    int idx = find_room(s, room);

    if(idx < 0){
        send_error(c,E_BAD_ROOM, "Wrong room ID");
        return;
    }
    uint64_t bit = 1llu << idx;
    uint64_t was_member = c->rooms_mask & bit;

    if(!was_member) {
        send_error(c,E_BAD_ROOM,"Requested transfer in wrong room");
        return;
    }

    if(size > MAX_FILE) {
        send_error(c, E_FILE_TOO_LARGE, "File too large");
        return;
    }

    struct file_meta *f = file_new(s,room,filename,size, c->nick);
    if(!f) {
        send_error(c,E_BAD_TOKEN, "Cannot create file");
        return;
    }
    struct transfer *t = transfer_new(s,1,f->id);
    if(!t){
        send_error(c,E_BAD_TOKEN, "Cannot initiate transfer");
        return;
    }

    // sending status back to client
    struct wbuf w = {0};

    put_u32(&w, t->id);
    put_u32(&w, t->token);

    send_frame(c, T_FILE_UP_OK, &w);

    wbuf_free(&w);
}

/* FILE_DOWN_REQ u32 file_id -> FILE_DOWN_OK. */
static void on_file_down_req(struct server *s, struct client *c, struct rbuf *r)
{
    if(!r || !s || !c) return;

    uint32_t file_id  = get_u32(r);

    if(r->err) return;


    struct file_meta *f = file_get(s,file_id);
    // always answer: the client matches replies to requests by order
    if(!f || !f->complete) {
        send_error(c, E_NO_SUCH_FILE, "No such file");
        return;
    }



    struct transfer *t = transfer_new(s,0,f->id);
    if(!t){
        send_error(c,E_BAD_TOKEN, "Cannot initiate transfer");
        return;
    }

    // sending status back to client
    struct wbuf w = {0};

    put_u32(&w, t->id);
    put_u32(&w, t->token);
    put_u64(&w, f->size);

    send_frame(c, T_FILE_DOWN_OK, &w);

    wbuf_free(&w);
}

/* BYE: client is leaving. */
static void on_bye(struct server *s, struct client *c)
{
    drop_client(s,c);
}

void handle_frame(struct server *s, struct client *c, uint8_t type, const uint8_t *payload, size_t len)
{
    struct rbuf f = { payload, len, 0 };

    // first frame decides: LOGIN -> ST_CONTROL, DATA_HELLO -> ST_UPLOAD / ST_DOWNLOAD
    if(c->st == ST_NEW){ // unknown user, just connected
        switch(type){
            case T_LOGIN: on_login(s,c,&f); break;
            case T_DATA_HELLO: on_data_hello(s,c,&f); break;
            default: drop_client(s,c); break;
        }
    }
    
    else if(c->st == ST_CONTROL){ // logged-in user
        switch(type){
            case T_JOIN: on_join(s,c,&f); break;
            case T_LEAVE: on_leave(s,c,&f); break;
            case T_SAY: on_say(s,c,&f); break;
            case T_FILE_UP_REQ: on_file_up_req(s,c,&f); break;
            case T_FILE_DOWN_REQ: on_file_down_req(s,c,&f); break;
            case T_BYE: on_bye(s,c); break;
            default: break; // unknown type: ignore (a newer client may send more)
        }
    }
}
void send_frame(struct client *c, uint8_t type, const struct wbuf *payload)
{
    struct wbuf f = {0};
   
    if(payload){
        put_u32(&f, 1 + payload->len); // length counts the type byte
        put_u8(&f, type);
        put_bytes(&f, payload->data, payload->len);

    } else {
        put_u32(&f, 1);
        put_u8(&f, type);
    }

    client_send(c,f.data, f.len);
    wbuf_free(&f);

}

void send_error(struct client *c, uint16_t code, const char *msg)
{
    struct wbuf f = {0};
    put_u16(&f, code);
    put_str(&f, msg);
    send_frame(c, T_ERROR, &f);

    wbuf_free(&f);
}

void broadcast_room(struct server *s, uint16_t room_id, const struct client *except,
                    uint8_t type, const struct wbuf *payload)
{
    int idx = find_room(s,room_id);
    if(idx < 0) return;

    uint64_t bit = 1ull << idx;

    for(size_t i = 0; i < MAX_CLIENTS; ++i){
        struct client *c = &s->clients[i];
        if(c == except || !(c->rooms_mask & bit) || c->st != ST_CONTROL)
            continue; // skip: excluded one, non-members, data conns, free slots
        send_frame(c,type,payload);
    }

}
