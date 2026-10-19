/* Shared server state and cross-file functions. */
#ifndef ETHER_SERVER_H
#define ETHER_SERVER_H

#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>

#include "buf.h"
#include "proto.h"

#define MAX_CLIENTS   64                    /* control + data connections */
#define MAX_ROOMS     16                    /* <= 64: rooms_mask is a u64 */
#define MAX_FILES     256
#define MAX_TRANSFERS 32
#define MAX_ACCOUNTS  256
#define HASH_LEN      128                   /* crypt() "$6$..." output fits */

enum conn_state { ST_NEW, ST_CONTROL, ST_UPLOAD, ST_DOWNLOAD };

struct msg {
    uint64_t unix_ms;
    char from[MAX_NICK + 1];
    char text[MAX_TEXT + 1];
};

struct room {
    uint16_t id;
    char name[32];
    struct msg history[HISTORY_LEN];        /* ring buffer */
    int head, count;
};

struct client {
    int fd;                                 /* -1 = free slot */
    enum conn_state st;
    uint32_t session_id;
    char nick[MAX_NICK + 1];
    struct sockaddr_in udp_addr;
    int has_udp;
    uint64_t last_typing_ms;
    uint8_t inbuf[MAX_FRAME + 4];
    size_t inlen;
    uint8_t *outbuf;                        /* growable */
    size_t outlen, outcap;
    uint64_t rooms_mask;                    /* bit i = member of rooms[i] */
    FILE *xfer_fp;                          /* data connections only */
    uint64_t xfer_left;
    uint32_t xfer_file_id;
};

struct file_meta {                          /* stored on disk as uploads/<id> */
    uint32_t id;
    uint16_t room_id;
    char name[MAX_FILENAME + 1];            /* metadata only, never a path */
    uint64_t size;
    char from[MAX_NICK + 1];
    int complete;
};

struct transfer {
    int in_use;
    int upload;                             /* 1 = upload, 0 = download */
    uint32_t id, token;
    uint32_t file_id;
    uint64_t expires_ms;
};

struct account {
    char nick[MAX_NICK + 1];
    char hash[HASH_LEN];
};

struct server {
    const char *name;                       /* sent in DISCOVER_REPLY */
    int tcp_fd, udp_fd;
    struct client clients[MAX_CLIENTS];
    struct room rooms[MAX_ROOMS];
    int n_rooms;
    struct file_meta files[MAX_FILES];
    int n_files;
    struct transfer xfers[MAX_TRANSFERS];
    struct account accounts[MAX_ACCOUNTS];
    int n_accounts;
    const char *accounts_path;
};

/* main.c */
uint64_t now_ms(void);                      /* wall clock, unix ms */
/* Close fd, broadcast USER_EVT left to its rooms, free buffers, mark slot free. */
void drop_client(struct server *s, struct client *c);
/* Queue bytes on c's outbuf; the poll loop sends them when the socket is writable. */
void client_send(struct client *c, const void *data, size_t len);

/* tcp.c */
/* One complete frame from c: type byte + payload without the length prefix. */
void handle_frame(struct server *s, struct client *c, uint8_t type, const uint8_t *payload, size_t len);
/* Queue [u32 length][u8 type][payload] on c. payload may be NULL for an empty frame. */
void send_frame(struct client *c, uint8_t type, const struct wbuf *payload);
void send_error(struct client *c, uint16_t code, const char *msg);
/* Send a frame to every CONTROL client in room_id except `except` (may be NULL). */
void broadcast_room(struct server *s, uint16_t room_id, const struct client *except,
                    uint8_t type, const struct wbuf *payload);
/* Index of the room with this id in s->rooms, or -1. */
int find_room(struct server *s, uint16_t id);

/* udp.c */
/* One datagram received on the UDP socket from src. */
void handle_udp(struct server *s, const uint8_t *pkt, size_t len, const struct sockaddr_in *src);
/* Send [magic][ver][type][session_id][payload] to dst. */
void udp_send(struct server *s, const struct sockaddr_in *dst, uint8_t type,
              uint32_t session_id, const struct wbuf *payload);

/* files.c */
/* New metadata entry for an upload that has not arrived yet. NULL if the table is full. */
struct file_meta *file_new(struct server *s, uint16_t room_id, const char *name,
                           uint64_t size, const char *from);
struct file_meta *file_get(struct server *s, uint32_t file_id);
/* New transfer with a random id + token, expiring after TOKEN_TTL_MS. NULL if the table is full. */
struct transfer *transfer_new(struct server *s, int upload, uint32_t file_id);
/* Return the matching unexpired transfer and free its slot (single use), else NULL. */
struct transfer *transfer_claim(struct server *s, uint32_t id, uint32_t token);
/* First frame on a NEW connection was DATA_HELLO: switch c to UPLOAD or DOWNLOAD, or drop it. */
void on_data_hello(struct server *s, struct client *c, struct rbuf *r);
/* Raw bytes arrived on an UPLOAD connection. When the file is complete, broadcast FILE_EVT. */
void upload_bytes(struct server *s, struct client *c, const uint8_t *data, size_t len);
/* DOWNLOAD connection's outbuf drained: queue the next chunk, or close when done. */
void download_refill(struct server *s, struct client *c);

/* accounts.c */
enum acc_result { ACC_OK, ACC_CREATED, ACC_BAD_PASS, ACC_BAD_FORMAT };
/* Read "nick:hash" lines from path. Missing file = no accounts yet. Returns 0 or -1. */
int accounts_load(struct server *s, const char *path);
/* Known nick: check pass. Unknown nick: hash pass, store, append to the file.
 * ACC_BAD_FORMAT = empty or too long, or nick contains ':' or control chars (-> E_BAD_CREDENTIALS). */
enum acc_result account_login(struct server *s, const char *nick, const char *pass);

#endif
