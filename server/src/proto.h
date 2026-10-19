/* Wire protocol constants. Keep in sync with client/ether_core/proto.py. */
#ifndef ETHER_PROTO_H
#define ETHER_PROTO_H

#define TCP_PORT        5050
#define UDP_PORT        5051
#define UDP_MAGIC       0x4554
#define UDP_VERSION     1
#define UDP_HEADER_LEN  8                   /* u16 magic, u8 ver, u8 type, u32 session_id */

/* Limits */
#define MAX_FRAME       (64 * 1024)         /* u32 length field, includes the type byte */
#define MAX_NICK        31                  /* bytes, UTF-8 */
#define MAX_PASS        64                  /* bytes, UTF-8 */
#define MAX_TEXT        4000                /* bytes per chat message (GUI input box limit) */
#define MAX_FILENAME    255
#define MAX_FILE        (50 * 1024 * 1024)
#define HISTORY_LEN     50                  /* messages kept per room, sent on JOIN */
#define TOKEN_TTL_MS    30000               /* transfer tokens: single use, expire after 30 s */
#define TYPING_MIN_MS   1000                /* relay at most 1 TYPING per user per second */

/* TCP message types */
enum tcp_type {
    T_LOGIN         = 0x01,                 /* C->S str nickname, str password */
    T_WELCOME       = 0x02,
    T_JOIN          = 0x03,
    T_LEAVE         = 0x04,
    T_SAY           = 0x05,
    T_MSG           = 0x06,
    T_HISTORY       = 0x07,
    T_USERS         = 0x08,
    T_USER_EVT      = 0x09,
    T_FILE_UP_REQ   = 0x10,
    T_FILE_UP_OK    = 0x11,
    T_FILE_EVT      = 0x12,
    T_FILE_DOWN_REQ = 0x13,
    T_FILE_DOWN_OK  = 0x14,
    T_DATA_HELLO    = 0x15,
    T_ERROR         = 0x7E,
    T_BYE           = 0x7F,
};

/* UDP datagram types */
enum udp_type {
    U_DISCOVER       = 0x01,
    U_DISCOVER_REPLY = 0x02,
    U_UDP_HELLO      = 0x03,
    U_TYPING         = 0x04,
    U_TYPING_EVT     = 0x05,
    U_PING           = 0x06,
    U_PONG           = 0x07,
};

/* ERROR codes */
enum err_code {
    E_NICK_ONLINE     = 1,                  /* account already logged in */
    E_BAD_ROOM        = 2,
    E_FILE_TOO_LARGE  = 3,
    E_BAD_TOKEN       = 4,
    E_FRAME_TOO_LARGE = 5,
    E_NOT_LOGGED_IN   = 6,
    E_BAD_PASSWORD    = 7,
    E_TEXT_TOO_LONG   = 8,
    E_BAD_CREDENTIALS = 9,                  /* empty / too long, or nick has ':' or control chars */
    E_NO_SUCH_FILE    = 10,                 /* FILE_DOWN_REQ: unknown file id or upload not finished */
};

#endif
