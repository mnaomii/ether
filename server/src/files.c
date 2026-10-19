/* File transfers over separate data connections. Files live in uploads/<file_id>. */
#include "server.h"
#include <string.h>
#include <sys/random.h>


struct file_meta *file_new(struct server *s, uint16_t room_id, const char *name,
                           uint64_t size, const char *from)
{

    if(!s || !from || !name ||  s->n_files == MAX_FILES ) return NULL; 

    struct file_meta *f = &s->files[s->n_files];
    memset(f, 0,sizeof(*f));

    f->room_id = room_id;
    strcpy(f->name, name);
    strcpy(f->from, from);
    f->size = size;
    f->id = ++s->n_files;

    return f;
}

struct file_meta *file_get(struct server *s, uint32_t file_id)
{
    if(file_id == 0 || file_id > s->n_files) return NULL;
    
    return &s->files[file_id-1];
}

struct transfer *transfer_new(struct server *s, int upload, uint32_t file_id)
{
    
    size_t free_slot = 0;
    for(; free_slot < MAX_TRANSFERS; ++free_slot)
        if(s->xfers[free_slot].in_use == 0) break;

    if(free_slot == MAX_TRANSFERS) return NULL;
    struct transfer *t = &s->xfers[free_slot];
    t->file_id = file_id;
    t->in_use = 1;
    t->expires_ms = now_ms() + TOKEN_TTL_MS; // 30s availability of request
    t->upload = upload;
    t->id = free_slot+1;
    getrandom(&t->token, sizeof(t->token),0);

    return t;
}

struct transfer *transfer_claim(struct server *s, uint32_t id, uint32_t token)
{
    if(id - 1 >= MAX_TRANSFERS ) return NULL;

    struct transfer *f = &s->xfers[id-1];
    if(now_ms() >= f->expires_ms ) { f->in_use = 0; return NULL;}
    if(f->token == token && f->in_use == 1) {
        f->in_use = 0;
        return f;
    }

    return NULL;
}


// ---- helper 

static void finish_upload(struct server *s, struct client *c){
    struct file_meta *f = file_get(s, c->xfer_file_id);
    if(!f) {
        fclose(c->xfer_fp);
        drop_client(s,c);
        return;
    }

    fclose(c->xfer_fp);
    c->xfer_fp = NULL; // close file - transfer finished

    f->complete = 1; // mark it as finished

    struct wbuf w = {0};
    put_u16(&w,f->room_id);
    put_u32(&w, f->id);
    put_str(&w, f->name);
    put_u64(&w, f->size);
    put_str(&w, f->from);


    // letting everyone know there has been a file uploaded
    broadcast_room(s, f->room_id, NULL, T_FILE_EVT, &w);
    wbuf_free(&w);

    drop_client(s,c);

}

/// ---- end helper

void on_data_hello(struct server *s, struct client *c, struct rbuf *r)
{
    // rbuf -> [DATA_HELLO][xfer-id][token]

    uint32_t xfer_id = get_u32(r);
    uint32_t token = get_u32(r);
    if(r->err) { drop_client(s,c); return; } // malformed hello, ending xfer tcp conn

    struct transfer *t = transfer_claim(s,xfer_id,token);
    if(!t) { // invalid transfer, ending xfer tcp conn
        drop_client(s,c);
        return;
    }

    struct file_meta *f = file_get(s, t->file_id);
    if(!f){ // invalid file, dropping connection
        drop_client(s,c);
        return;
    }

    char path[32];
    snprintf(path,sizeof(path),"uploads/%u",f->id);

    if(t->upload){
        FILE* fd = fopen(path,"wb");
        if(!fd){ // cannot create file on disk, dropping connection
            drop_client(s,c);
            return;
        }
        c->xfer_file_id = f->id;
        c->xfer_left = f->size;
        c->xfer_fp = fd;
        c->st = ST_UPLOAD;

        if(f->size == 0) finish_upload(s,c); // nothing to receive, complete now

        // file bytes arrive later: read_client -> upload_bytes
    } else { // download
        FILE* fd = fopen(path,"rb");
        if(!fd){ // cannot create file on disk, dropping connection
            drop_client(s,c);
            return;
        }
        c->xfer_file_id = f->id;
        c->xfer_left = f->size;
        c->xfer_fp = fd;
        c->st = ST_DOWNLOAD;

        download_refill(s,c);

    }

}

void upload_bytes(struct server *s, struct client *c, const uint8_t *data, size_t len)
{

    // never writing more than announced
    uint64_t n = len < c->xfer_left ? len : c->xfer_left;


    if( fwrite(data, 1, n, c->xfer_fp) != n ) { // disk err occured, not enough bytes written
        drop_client(s,c); return; 
    }

    c->xfer_left -= n;
    if(c->xfer_left == 0) finish_upload(s,c);
}

void download_refill(struct server *s, struct client *c)
{
    if(c->xfer_left == 0 ){ // download complete
        drop_client(s,c);
        return;
    }

    uint8_t buf[65536]; // 64kib
    size_t want = c->xfer_left < sizeof(buf) ? c->xfer_left : sizeof(buf); // min(left, 64 KiB)

    size_t n = fread(buf,1,want,c->xfer_fp); // filling the buffer with max available data
    if( n == 0){
        drop_client(s,c);
        return;
    }

    client_send(c,buf,n);
    c->xfer_left -= n;
}
