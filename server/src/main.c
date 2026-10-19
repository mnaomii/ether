/* poll() loop, accept, read/write, dispatch. */
#include "server.h"

#include <time.h>
#include <stdio.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <errno.h>
#include <stdlib.h>
#include <poll.h>
#include <sys/stat.h>

static struct server srv;                  // global

uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}

/* Bound, listening, non-blocking TCP socket on port. Returns fd or -1. */
static int listen_tcp(uint16_t port)
{
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if(sock < 0) return -1;

    int one = 1; // size of an option
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)); // skip timeouts

    struct sockaddr_in sv = {0};
    sv.sin_family = AF_INET;
    sv.sin_port = htons(port);
    sv.sin_addr.s_addr = htonl(INADDR_ANY); // or 0.0.0.0

    if (bind(sock,(struct sockaddr *)&sv, sizeof(sv)) < 0 
        || listen(sock, 16) // 16 clients can wait in backlog
        || fcntl(sock, F_SETFL, O_NONBLOCK) < 0){
        close(sock); return -1;
    }

    
    return sock;
}

/* Bound, non-blocking UDP socket on port (DISCOVER, TYPING, PING). Returns fd or -1. */
static int bind_udp(uint16_t port)
{
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if(sock < 0) return -1;

    struct sockaddr_in sv = {0};
    sv.sin_family = AF_INET;
    sv.sin_port = htons(port);
    sv.sin_addr.s_addr = htonl(INADDR_ANY); // or 0.0.0.0

    if (bind(sock,(struct sockaddr *)&sv, sizeof(sv)) < 0 
        || fcntl(sock, F_SETFL, O_NONBLOCK) < 0){
        close(sock); return -1;
    }

    
    return sock;
}

/* tcp_fd readable: accept, make non-blocking, take a free slot in state ST_NEW. */
static void accept_client(struct server *s)
{
    while (1){ 
        int fd = accept(s->tcp_fd, NULL, NULL); 
        if (fd < 0 || fcntl(fd, F_SETFL, O_NONBLOCK) < 0)
           { close(fd); return; }

        size_t index = 0;
        while(index < MAX_CLIENTS && s->clients[index].fd != -1)
            ++index;

        if(index == MAX_CLIENTS) { close(fd); return; }

        struct client*  c = &s->clients[index];
        memset(c,0,sizeof(s->clients[index])); // wipes the prev entry

        c->fd = fd;
        c->st = ST_NEW; // identity not established yet
    }
}

/* c readable. CONTROL/NEW: buffer bytes, call handle_frame() for every complete frame,
 * drop on oversized frame. UPLOAD: pass raw bytes to upload_bytes(). recv() == 0: drop. */
static void read_client(struct server *s, struct client *c)
{

    // purpose ? data arrives in stream -> reconstruct it in 
    //                                          a frame


    if(c->st == ST_UPLOAD){ //if user is transferring files
        ssize_t n = recv(c->fd, c->inbuf, sizeof(c->inbuf), 0);
        if( n == 0) { // client exited mid-upload
            drop_client(s,c); return;
        }

        if(n < 0){
            if(errno == EAGAIN || errno == EINTR) return;
            else{
                drop_client(s,c);
                return;
            }
        }

        upload_bytes(s,c, c->inbuf, n);
        return;
    }

    else if(c->st == ST_DOWNLOAD){
        ssize_t n = recv(c->fd, c->inbuf, sizeof(c->inbuf), 0);
        if( n == 0) {
            drop_client(s,c);
            return;
        }

        if( n < 0 ){
            if(errno == EAGAIN || errno == EINTR) return;
            else{
                drop_client(s,c);
                return;
            }
        }

        return;
    }

    int n = recv(c->fd, c->inbuf + c->inlen, 
                    sizeof(c->inbuf) - c->inlen, 0); // append to current

    if( n == 0 ) { drop_client(s,c); return; } // client closed case
    if( n < 0 ) { 
        if (errno == EAGAIN || errno == EINTR)  return;  // retry
        else {drop_client(s,c); return;} // other error, drop client
    }


    // frame -> u32 len + u8 type + payload, len = 1 + size(payload)
    c->inlen += n;

    while( c->inlen >= 4){

        struct rbuf tmp;
        tmp.p = c->inbuf;
        tmp.left = c->inlen;
        tmp.err = 0;

        uint32_t len = get_u32(&tmp); // returns big endian u32
        if(len == 0 || len > MAX_FRAME) { drop_client(s,c); return; }
        if(c->inlen < 4 + len) break; // incomplete , wait


        // decoding the kind of msg
        handle_frame(s,c,c->inbuf[4], c->inbuf + 5, len - 1);
        if(c->fd < 0) return;

        // memmove - src & dest refer to same ptr, memcpy -> UB
        memmove(c->inbuf,c->inbuf+4+len, c->inlen-4-len);
        c->inlen -= 4 + len;

        if(c->st == ST_UPLOAD){
            size_t n = c->inlen;
            c->inlen = 0;

            if(n > 0) upload_bytes(s,c,c->inbuf, n);
            return;
        }
    }
    
}

/* c writable and outlen > 0: send as much as possible, keep the rest.
 * DOWNLOAD: call download_refill() when the buffer drains. */
static void write_client(struct server *s, struct client *c)
{
    // nosignal - if client disconnected already, dont raise SIGPIPE (kills sv)
    int n = send(c->fd, c->outbuf, c->outlen, MSG_NOSIGNAL);
    if(n < 0) {
        if(errno == EAGAIN || errno == EINTR) return;
        else { drop_client(s,c); return;}
    }

    memmove(c->outbuf,c->outbuf+n, c->outlen-n); // remove sent items
    c->outlen -= n;

    if(c->st == ST_DOWNLOAD && c->outlen == 0){
        download_refill(s,c);
    }
}

void drop_client(struct server *s, struct client *c)
{
    close(c->fd);
    free(c->outbuf);
    
    if(c->st == ST_CONTROL) { // broadcasting user disconnect
        for(int i = 0; i < s->n_rooms; ++i){
            uint64_t bit = 1ull << i; // i is already the room index
            uint64_t was_member = c->rooms_mask & bit;

            if(!was_member) continue;

            struct wbuf payload = {0};
            put_u16(&payload, s->rooms[i].id);
            put_u8(&payload, 0); // quit server
            put_str(&payload, c->nick);

            broadcast_room(s,s->rooms[i].id, c, T_USER_EVT, &payload);

            wbuf_free(&payload);
        }
    }

    if(c->xfer_fp) fclose(c->xfer_fp);

    memset(c,0,sizeof(*c));
    c->fd = -1;
}

void client_send(struct client *c, const void *data, size_t len)
{
    // prepares the outgoing buffer for sending
    
    if( c == NULL || len == 0 ) return;

    if(c->outbuf == NULL) {
        c->outbuf = malloc(64);
        c->outcap = 64;
        if(!c->outbuf) { perror("[ERR:client_send()] Memory alloc failed"); exit(1); }
    }

    if(c->outlen + len > c->outcap){
        while ( c->outlen + len > c->outcap) {
            c->outcap *= 2;
        }

        c->outbuf = realloc(c->outbuf, c->outcap);

        if(!c->outbuf) { perror("[ERR:client_send()] Memory alloc failed"); exit(1);}
    }
    memcpy(c->outbuf + c->outlen, data, len);
    c->outlen += len;

}

/* Usage: ether-server [server_name]. Rooms: general, homework, games.
 * Loads accounts.txt, opens sockets, then runs the poll loop forever. */
int main(int argc, char **argv)
{
    srv.name = argc > 1 ? argv[1] : "local-ether";

    const char *names[] = {"general", "homework", "games"};
    for(int i=0; i<3; ++i){
        srv.rooms[i].id = (uint16_t) (i+1);
        strcpy(srv.rooms[i].name, names[i]);
    }

    srv.n_rooms = 3;

    for(size_t index = 0; index < MAX_CLIENTS; ++index)
        srv.clients[index].fd = -1;

    if( accounts_load(&srv,"accounts.txt") < 0)
        perror("[ERR:main()] no accounts were found");

    srv.tcp_fd = listen_tcp(5050);
    srv.udp_fd = bind_udp(5051);

    if(srv.tcp_fd == -1 || srv.udp_fd == -1)
    {
        perror("[ERR:main()] Cannot bind to intended port");
        return 1;
    }

    mkdir("uploads",0755);

    struct pollfd poll_fd[2 + MAX_CLIENTS];
    // [0] - tcp
    // [1] - udp
    // [2+i] - client no. i

    while(1){
        poll_fd[0].fd     = srv.tcp_fd;
        poll_fd[0].events = POLLIN;

        poll_fd[1].fd     = srv.udp_fd;
        poll_fd[1].events = POLLIN;

        for(size_t i = 0; i < MAX_CLIENTS; ++i){
            struct client* c = &srv.clients[i];
            poll_fd[2+i].fd = c->fd;
            poll_fd[2+i].events = POLLIN | ((c->outlen > 0) ? POLLOUT : 0);
        }

        if( poll(poll_fd, 2 + MAX_CLIENTS, -1) < 0 ){
            if(errno == EINTR) continue;
            perror("[ERR:main()] poll"); return 1;
        }
        
        
        if(poll_fd[0].revents & POLLIN) accept_client(&srv);
        if(poll_fd[1].revents & POLLIN) 
            while(1){
                struct sockaddr_in src = {0};
                socklen_t size = sizeof(src);

                uint8_t buff[2048];
                int n = recvfrom(srv.udp_fd, buff, sizeof(buff), 0, 
                                (struct sockaddr *)&src, &size);

                if (n < 0) break;
                handle_udp(&srv, buff, n, &src);
            }

        for(size_t i = 0; i < MAX_CLIENTS; ++i){
            struct client* c = &srv.clients[i];
            if(c->fd < 0) continue;

            if(poll_fd[2+i].revents & (POLLIN | POLLERR | POLLHUP))
                read_client(&srv,c);

            if(c->fd >= 0 && poll_fd[2+i].revents & POLLOUT)
                write_client(&srv,c);
        }


    }
    
    return 0;
}
