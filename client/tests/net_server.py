"""Black-box tests of the C server over raw sockets.

    python tests/net_server.py [--host 192.168.1.40] [--slow] [-k NAME] [-v]

Uses its own byte encoders (netkit.py), not ether_core.proto, so a client codec bug can't hide
or fake a server bug. Skips everything if nothing listens on HOST:5050.

Accounts persist in accounts.txt and the server holds at most MAX_ACCOUNTS (256), so the tests
reuse a fixed pool of nicknames (ts000, ts001, ...) with password "pw-<nick>" instead of adding
~100 random accounts per run. Only the first run registers them.
"""
from __future__ import annotations

import os
import secrets
import socket
import struct
import time

import netkit as nk
from netkit import cfg, check, skip, test

ROOMS = nk.ROOMS


def tok(n=4) -> str:
    return secrets.token_hex(n)


_pool = iter(range(10_000))


def nick() -> str:
    """Next nickname from the fixed pool; its password is pw(nick)."""
    return f"ts{next(_pool):03d}"


def pw(n: str) -> str:
    return "pw-" + n


def user(label="", rooms=(), rcvbuf=0) -> nk.Conn:
    """A logged-in client that is a member of `rooms` (their HISTORY/USERS already read)."""
    n = nick()
    for attempt in (n, "x" + tok(5)):
        c = nk.Conn(rcvbuf=rcvbuf)
        c.nick, c.pw, c.label = attempt, pw(attempt), label
        c.send(nk.login(c.nick, c.pw))
        w = c.expect((nk.WELCOME, nk.ERROR), why="LOGIN with a valid nickname + its password gets WELCOME (on_login)")
        if isinstance(w, nk.Welcome):
            break
        # A pool name registered elsewhere with another password (7), or still online (1): use a
        # fresh random name instead. right_password_works_after_logout checks 7 on its own.
        check(w.code in (1, 7) and attempt == n, f"LOGIN {attempt!r} with password {c.pw!r}: expected WELCOME, got {w}")
        c.close()
    c.sid = w.sid
    for r in rooms:
        enter(c, r)
    return c


def enter(c: nk.Conn, room: int):
    """JOIN room; returns (History, Users). The order is checked by join_sends_history_then_users."""
    c.send(nk.join(room))
    h = c.expect(nk.HISTORY, lambda m: m.room == room, why=f"JOIN {room} is answered with HISTORY, then USERS (on_join)")
    u = c.expect(nk.USERS, lambda m: m.room == room, why=f"JOIN {room} is answered with HISTORY, then USERS (on_join)")
    return h, u


def expect_welcome(c: nk.Conn, why: str):
    """WELCOME for a LOGIN sent in some special way; an ERROR answer is reported as what it is."""
    m = c.expect((nk.WELCOME, nk.ERROR), why=why)
    check(isinstance(m, nk.Welcome), f"LOGIN {c.nick!r} got {m} instead of WELCOME. The frame itself arrived "
                                     "fine (the server answered it): look at the login_* tests first")
    return m


def login_reply(n, pw, retry_online=True):
    """LOGIN on a new connection -> (conn, Welcome | Error). Retries on ERROR 1 for a while,
    because the server may not have processed an earlier close yet."""
    end = time.monotonic() + cfg.t
    while True:
        c = nk.Conn()
        c.nick = n
        c.send(nk.login(n, pw))
        m = c.expect((nk.WELCOME, nk.ERROR), why="LOGIN is answered with WELCOME or ERROR")
        if retry_online and isinstance(m, nk.Error) and m.code == 1 and time.monotonic() < end:
            c.close()
            time.sleep(0.1)
            continue
        return c, m


# Floods use ~1 KB texts: rooms keep their last 50 messages for the server's whole life, and
# 50 x 4000-byte messages would make every later HISTORY ~200 KB, over MAX_FRAME (64 KiB).
FLOOD_TEXT = 1000


def check_ms(m: nk.Msg):
    now = time.time() * 1000
    check(abs(m.ms - now) < 86_400_000,
          f"MSG unix_ms = {m.ms}, but now is about {int(now)} (more than a day apart): now_ms() returns "
          "milliseconds since 1970 (tv_sec * 1000 + tv_nsec / 1000000, as uint64_t) and put_u64 "
          "writes 8 big-endian bytes")


def udp_for(c: nk.Conn) -> nk.Udp:
    """A UDP socket bound to c's session with UDP_HELLO."""
    u = nk.Udp()
    u.send(nk.udp_hello(c.sid))
    return u


def probe():
    try:
        socket.create_connection((cfg.host, nk.TCP_PORT), timeout=min(cfg.t, 3)).close()
    except OSError as e:
        skip(f"no Ether server accepts TCP at {cfg.host}:{nk.TCP_PORT} ({e}). Start it first "
             "(cd server && make && ./ether-server test), or pass --host / set ETHER_HOST.")


# ---------------------------------------------------------------- login

@test
def login_new_nick_gets_welcome():
    c = nk.Conn()
    c.send(nk.login("new_" + tok(5), "pw-" + tok()))
    w = c.expect(nk.WELCOME, why="an unknown nickname auto-registers (account_login -> ACC_CREATED) and gets WELCOME")
    check(w.sid != 0, "WELCOME session_id is 0: use a random non-zero id (getrandom)")
    check(len(w.rooms) == 3 and dict(w.rooms) == ROOMS,
          f"WELCOME rooms: expected {sorted(ROOMS.items())}, got {w.rooms}")


@test
def session_ids_are_unique():
    a, b = user(), user()
    check(a.sid != b.sid, f"two logins got the same session_id 0x{a.sid:08X}: UDP datagrams carry it to "
                          "tell users apart, so make it random (getrandom)")


@test
def wrong_password_error_7():
    a = user()
    a.close()
    _, m = login_reply(a.nick, a.pw + "x")
    check(isinstance(m, nk.Error) and m.code == 7,
          f"known nickname + wrong password: expected ERROR 7, got {m}. account_login: "
          "crypt(pass, stored_hash) must equal stored_hash, otherwise ACC_BAD_PASS")


@test
def right_password_works_after_logout():
    a = user()
    a.close()
    _, m = login_reply(a.nick, a.pw)
    check(isinstance(m, nk.Welcome),
          f"logging in again with the right password after the first session closed: expected WELCOME, "
          f"got {m}. account_login must accept the stored hash, and drop_client must free the nickname")


@test
def nick_already_online_error_1():
    a = user()
    _, m = login_reply(a.nick, a.pw, retry_online=False)
    check(isinstance(m, nk.Error) and m.code == 1,
          f"same nickname while the first session is still online: expected ERROR 1, got {m}. "
          "on_login: another CONTROL client already has this nick -> ERROR 1")


BAD_CREDENTIALS = {
    "nick_with_colon": lambda: ("a:" + tok(), "pw"),
    "empty_nick": lambda: ("", "pw"),
    "nick_32_bytes": lambda: (tok(16), "pw"),
    "nick_32_bytes_utf8": lambda: ("ș" * 15 + tok(1), "pw"),       # 17 characters, 32 bytes
    "nick_with_control_char": lambda: ("a\x01" + tok(), "pw"),
    "empty_password": lambda: (nick(), ""),
    "password_65_bytes": lambda: (nick(), "p" * 65),
}


def _bad_credentials(label, make):
    def t():
        n, pw = make()
        c = nk.Conn()
        c.send(nk.login(n, pw))
        m = c.expect((nk.WELCOME, nk.ERROR), why="a malformed LOGIN gets ERROR 9")
        check(isinstance(m, nk.Error) and m.code == 9,
              f"LOGIN {label} (nick {n!r} = {len(n.encode())} B, password {len(pw.encode())} B): expected "
              f"ERROR 9, got {m}. Rule: 1..31 nick bytes without ':' or control chars, 1..64 password bytes")
    test(t, name=f"login_bad_credentials_error_9[{label}]")


for _label, _make in BAD_CREDENTIALS.items():
    _bad_credentials(_label, _make)


@test
def login_limits_are_inclusive():
    c = nk.Conn()
    n = "ts_limit_" + "x" * 22                          # 31 bytes; same account every run
    c.send(nk.login(n, "p" * 64))
    m = c.expect((nk.WELCOME, nk.ERROR), why="31-byte nick + 64-byte password is the limit, not over it")
    off_by_one = " (off by one? get_str's cap includes the NUL: MAX_NICK + 1)" if getattr(m, "code", 0) == 9 else ""
    check(isinstance(m, nk.Welcome), f"31-byte nickname + 64-byte password must be accepted, got {m}{off_by_one}")


# ---------------------------------------------------------------- framing

@test
def login_sent_one_byte_at_a_time():
    c = nk.Conn()
    c.nick = nick()
    c.send_slowly(nk.login(c.nick, pw(c.nick)))
    expect_welcome(c, "the LOGIN frame arrived one byte per recv(): keep partial frames in inbuf "
                      "until 4 + length bytes are there (read_client)")


@test
def several_frames_in_one_send():
    c = nk.Conn()
    text = "batch " + tok()
    c.nick = nick()
    c.send(nk.login(c.nick, pw(c.nick)) + nk.join(2) + nk.say(2, text))
    why = ("LOGIN + JOIN + SAY arrived in one recv(): handle every complete frame in inbuf, not only "
           "the first (while loop + memmove)")
    expect_welcome(c, why)
    c.expect(nk.HISTORY, lambda m: m.room == 2, why=why)
    c.expect(nk.USERS, lambda m: m.room == 2, why=why)
    c.expect(nk.MSG, lambda m: m.text == text, why=why)


@test
def oversized_frame_drops_client():
    c = user()
    c.send(struct.pack("!IB", nk.MAX_FRAME + 1, nk.SAY) + b"\0" * 64)
    c.wait_closed(why="a length field of 65537 (> MAX_FRAME) must drop the client right away "
                      "(read_client: len == 0 || len > MAX_FRAME -> drop_client). An ERROR 5 first is fine")


@test
def frame_of_exactly_64k_is_accepted():
    c = user(rooms=[3])
    big = nk.say(3, "x" * (nk.MAX_FRAME - 5))         # length field = 65536 = MAX_FRAME
    check(len(big) == 4 + nk.MAX_FRAME, "test bug: frame size")
    c.send(big)
    e = c.expect(nk.ERROR, why="a 64 KiB SAY (length = MAX_FRAME exactly) is a legal frame holding a "
                               "too-long text: answer ERROR 8, don't drop. inbuf must hold MAX_FRAME + 4 bytes")
    check(e.code == 8, f"expected ERROR 8 (text too long), got {e}")
    text = "still here " + tok()
    c.send(nk.say(3, text))
    c.expect(nk.MSG, lambda m: m.text == text, why="the connection must stay usable after the 64 KiB frame")


def _first_frame_must_be_login(label, data):
    def t():
        c = nk.Conn()
        c.send(data)
        c.wait_closed(why=f"a new connection whose first frame is {label} (not LOGIN / DATA_HELLO) "
                          "is dropped (handle_frame in ST_NEW). An ERROR 6 before closing is fine")
    test(t, name=f"first_frame_must_be_login[{label}]")


_first_frame_must_be_login("JOIN", nk.join(1))
_first_frame_must_be_login("SAY", nk.say(1, "hi"))


@test
def unknown_frame_type_is_skipped():
    c = user()
    c.send(nk.frame(0x55, b"junk junk") + nk.join(1))
    c.expect(nk.HISTORY, lambda m: m.room == 1,
             why="an unknown frame type is ignored and the length prefix keeps the stream in sync "
                 "(handle_frame: unknown type -> ignore)")


# ---------------------------------------------------------------- rooms

@test
def join_sends_history_then_users():
    a = user()
    a.send(nk.join(1))
    first = a.expect((nk.HISTORY, nk.USERS), lambda m: m.room == 1, why="JOIN 1 is answered with HISTORY, then USERS")
    check(isinstance(first, nk.History), "got USERS before HISTORY after JOIN: on_join sends HISTORY first, "
                                         "then USERS")
    users = a.expect(nk.USERS, lambda m: m.room == 1, why="after HISTORY comes USERS (on_join)")
    check(a.nick in users.nicks, f"USERS after JOIN must list every member, the joiner included; got {users.nicks}")
    check(len(users.nicks) == len(set(users.nicks)), f"USERS lists a nickname twice: {users.nicks}")
    a.quiet(nk.USER_EVT, lambda m: m.nick == a.nick,
            why="the joiner doesn't get USER_EVT about itself (broadcast_room with except = the joiner)")


@test
def join_notifies_other_members():
    a = user("a", [1])
    b = user("b")
    _, users = enter(b, 1)
    check({a.nick, b.nick} <= set(users.nicks), f"USERS for room 1 should list {a.nick} and {b.nick}, got {users.nicks}")
    ev = a.expect(nk.USER_EVT, lambda m: m.nick == b.nick,
                  why="members already in the room get USER_EVT joined (on_join: broadcast except the joiner)")
    check(ev == (1, 1, b.nick), f"expected USER_EVT(room 1, joined=1, {b.nick!r}), got {ev}")


@test
def join_bad_room_error_2():
    c = user()
    for room in (0, 99, 0xFFFF):
        c.send(nk.join(room))
        e = c.expect(nk.ERROR, why=f"JOIN {room}: no such room -> ERROR 2 (find_room returns -1). "
                                   "Careful with 1ULL << id for big ids")
        check(e.code == 2, f"JOIN {room}: expected ERROR 2, got {e}")
    enter(c, 1)                                         # still usable


@test
def say_reaches_every_member_including_sender():
    a, b, other = user("a", [1]), user("b", [1]), user("c", [2])
    text = "salut " + tok()
    a.send(nk.say(1, text))
    ma = a.expect(nk.MSG, lambda m: m.text == text,
                  why="the sender gets its own MSG back: on_say broadcasts with except = NULL")
    mb = b.expect(nk.MSG, lambda m: m.text == text, why="every member of the room gets the MSG")
    check(ma.room == 1 and ma.frm == a.nick, f"MSG should be (room 1, from {a.nick!r}), got {ma}")
    check(ma == mb, f"sender and member got different MSGs: {ma} vs {mb}")
    check_ms(ma)
    other.quiet(nk.MSG, lambda m: m.text == text, why="a client that isn't in room 1 must not get its messages")


@test
def say_in_room_not_joined_error_2():
    a, b = user("a", [1]), user("b")
    text = "intruder " + tok()
    b.send(nk.say(1, text))
    e = b.expect(nk.ERROR, why="SAY to a room you haven't joined -> ERROR 2 (on_say)")
    check(e.code == 2, f"expected ERROR 2, got {e}")
    a.quiet(nk.MSG, lambda m: m.text == text, why="a non-member's SAY must not be broadcast")


@test
def text_limit_is_4000_bytes():
    c = user(rooms=[3])
    for text, what in (("ă" * 2001, "4002 bytes (2001 characters)"), ("a" * 4001, "4001 bytes")):
        c.send(nk.say(3, text))
        e = c.expect((nk.ERROR, nk.MSG), why=f"a {what} text is over MAX_TEXT -> ERROR 8")
        check(isinstance(e, nk.Error) and e.code == 8,
              f"SAY with {what}: expected ERROR 8, got {nk.short(e)}. The limit is 4000 BYTES. Peek at "
              "the string length before get_str")
    exact = "ă" * 2000                                   # 4000 bytes
    c.send(nk.say(3, exact))
    m = c.expect((nk.ERROR, nk.MSG), why="exactly 4000 bytes is allowed")
    check(isinstance(m, nk.Msg) and m.text == exact,
          f"SAY with exactly 4000 bytes must be accepted (> MAX_TEXT is the error), got {nk.short(m)}")


@test
def utf8_text_round_trips():
    a, b = user("a", [2]), user("b", [2])
    text = "bună ziua, ăîșțâ 🙂 " + tok()
    a.send(nk.say(2, text))
    m = b.expect(nk.MSG, lambda m: m.frm == a.nick, why="SAY with UTF-8 text")
    check(m.text == text, f"text changed on the way: sent {text!r}, got {m.text!r}. Lengths are in bytes "
                          "(strlen, not characters); copy the bytes as they are")


@test
def messages_keep_their_order():
    a, b = user("a", [2]), user("b", [2])
    texts = [f"order {i} {tok(2)}" for i in range(20)]
    a.send(b"".join(nk.say(2, t) for t in texts))
    for who in (a, b):
        got = [who.expect(nk.MSG, lambda m: m.frm == a.nick, why="20 SAYs sent in one send()").text
               for _ in texts]
        check(got == texts, f"{who.who} got the messages in a different order: {got}")


@test
def history_replays_to_later_joiners():
    a = user("a", [2])
    t1, t2 = "first " + tok(), "second " + tok()
    a.send(nk.say(2, t1) + nk.say(2, t2))
    a.expect(nk.MSG, lambda m: m.text == t2)
    hint = ("room_push stores every MSG in the room's ring buffer and on_join sends it as HISTORY "
            "(entries = full MSG payload incl. room_id)")
    for who, how in ((user("c"), "a new client"), (a, "the author after LEAVE + JOIN")):
        if who is a:
            a.send(nk.leave(2))
        h, _ = enter(who, 2)
        check(not h.note, h.note)
        texts = [m.text for m in h.msgs]
        check(texts[-2:] == [t1, t2],
              f"HISTORY for {how} should end with our two messages, got {[nk.short(t, 30) for t in texts[-3:]]}. {hint}")
        check(h.msgs[-1].room == 2 and h.msgs[-1].frm == a.nick,
              f"last HISTORY entry should be (room 2, from {a.nick!r}), got {h.msgs[-1]}")
        check(len(h.msgs) <= 50, f"HISTORY has {len(h.msgs)} entries, the limit is 50")
        check_ms(h.msgs[-1])


@test
def history_keeps_the_last_50():
    a = user("a", [3])
    texts = [f"h{i:02d} {tok(2)}" for i in range(55)]
    a.send(b"".join(nk.say(3, t) for t in texts))
    a.expect(nk.MSG, lambda m: m.text == texts[-1], why="55 SAYs in one send()")
    h, _ = enter(user("c"), 3)
    check(not h.note, h.note)
    got = [m.text for m in h.msgs]
    check(got == texts[-50:],
          f"after 55 messages HISTORY must be exactly the last 50, oldest first. Got {len(got)} entries, "
          f"starting {got[:2]} and ending {got[-2:]}. Ring buffer: head = next slot, oldest = "
          "(head - count + 50) % 50")


@test
def leave_notifies_others_and_stops_messages():
    a, b = user("a", [1]), user("b", [1])
    a.expect(nk.USER_EVT, lambda m: m.nick == b.nick and m.joined)
    b.send(nk.leave(1))
    ev = a.expect(nk.USER_EVT, lambda m: m.nick == b.nick and not m.joined,
                  why="LEAVE: the other members get USER_EVT left (on_leave)")
    check(ev.room == 1, f"USER_EVT left should be for room 1, got {ev}")
    b.quiet(nk.USER_EVT, lambda m: m.nick == b.nick and not m.joined, wait=0.3,
            why="the leaver gets nothing back (USER_EVT left goes to the OTHERS)")
    text = "after leave " + tok()
    a.send(nk.say(1, text))
    a.expect(nk.MSG, lambda m: m.text == text)
    b.quiet(nk.MSG, lambda m: m.text == text, why="after LEAVE, B must not get room 1's messages (clear the room bit)")


def _disconnect(how):
    def t():
        a, b = user("a", [1, 2]), user("b", [1, 2])
        if how == "bye":
            b.send(nk.bye())
            b.wait_closed(why="after BYE the server closes the connection (on_bye -> drop_client)")
        elif how == "reset":
            b.abort()
        else:
            b.close()
        for room in (1, 2):
            a.expect(nk.USER_EVT, lambda m: m.room == room and m.nick == b.nick and not m.joined,
                     why=f"B {'sent BYE' if how == 'bye' else 'closed its socket' if how == 'close' else 'reset (crashed)'}: "
                         "drop_client sends USER_EVT left to every room B was in. Did the server notice "
                         "recv() == 0 (or -1 with ECONNRESET)?")
        text = "still serving " + tok()
        a.send(nk.say(1, text))
        a.expect(nk.MSG, lambda m: m.text == text, why="the server must keep serving the others")
    test(t, name=f"disconnect_notifies_others[{how}]")


for _how in ("close", "reset", "bye"):
    _disconnect(_how)


# ---------------------------------------------------------------- UDP

@test
def discover_gets_reply():
    a, b = user(), user()
    u = nk.Udp()
    u.send(nk.discover())
    d = u.expect(nk.U_DISCOVER_REPLY, why="DISCOVER (here unicast to port 5051) gets DISCOVER_REPLY sent back "
                                         "to recvfrom's source address (on_discover)")
    r = d.body
    check(r.name != "", "DISCOVER_REPLY server_name is empty: send srv.name (argv[1])")
    check(r.port == nk.TCP_PORT, f"DISCOVER_REPLY tcp_port = {r.port}, expected 5050 (put_u16 is big-endian)")
    check(2 <= r.users <= 64, f"DISCOVER_REPLY users_online = {r.users}, but at least 2 users are logged in "
                              "right now (count the ST_CONTROL clients)")


@test
def udp_junk_is_ignored():
    u = nk.Udp()
    junk = [bytes.fromhex("00 00 01 01 00 00 00 00"),           # bad magic (vector udp_bad_magic)
            bytes.fromhex("45 54 02 01 00 00 00 00"),           # version 2
            bytes.fromhex("45 54 01"),                          # truncated header
            b""]
    for j in junk:
        u.send(j)
    stray = u.recv(timeout=0.5)
    check(stray is None, f"the server answered junk with {stray}: drop datagrams with a wrong magic/version "
                         "or a short header (handle_udp)")
    u.send(nk.discover())
    u.expect(nk.U_DISCOVER_REPLY, why="after junk datagrams the server must still answer a good DISCOVER")


@test
def typing_goes_to_other_members_only():
    a, b, c = user("a", [1]), user("b", [1]), user("c", [2])
    ua, ub, uc = udp_for(a), udp_for(b), udp_for(c)
    time.sleep(0.2)
    ua.send(nk.typing(a.sid, 1))
    d = ub.expect(nk.U_TYPING_EVT, why="B shares room 1 and sent UDP_HELLO, so it gets TYPING_EVT: on_udp_hello "
                                       "stores recvfrom's src, on_typing sends to every OTHER member with has_udp")
    check(d.body == (1, a.nick), f"expected TYPING_EVT(room 1, {a.nick!r}), got {d.body}")
    check(ua.recv(timeout=0.4, pred=lambda d: d.type == nk.U_TYPING_EVT) is None,
          "the typer got its own TYPING_EVT: skip the sender")
    check(uc.recv(timeout=0.2, pred=lambda d: d.type == nk.U_TYPING_EVT) is None,
          "a client in another room got TYPING_EVT: only members of the room")


@test
def typing_from_non_member_or_unknown_session_is_dropped():
    a, b = user("a", [1]), user("b", [2])
    ua, ub = udp_for(a), udp_for(b)
    time.sleep(0.2)
    ua.send(nk.typing(a.sid, 2))
    ua.send(nk.typing(a.sid ^ 0x5A5A5A5A or 1, 2))
    stray = ub.recv(timeout=0.5, pred=lambda d: d.type == nk.U_TYPING_EVT)
    check(stray is None, f"got {stray and stray.body}: TYPING must come from a known session that is a "
                         "member of the room (find_session, then check the room bit)")


@test
def typing_is_rate_limited():
    a, b = user("a", [3]), user("b", [3])
    ua, ub = udp_for(a), udp_for(b)
    time.sleep(0.2)
    t0 = time.monotonic()
    ua.send(nk.typing(a.sid, 3))
    ua.send(nk.typing(a.sid, 3))
    ub.expect(nk.U_TYPING_EVT, why="the first TYPING is relayed")
    second = ub.recv(timeout=0.5, pred=lambda d: d.type == nk.U_TYPING_EVT)
    check(second is None, "2 TYPINGs within 1 s were both relayed: at most 1 per user per TYPING_MIN_MS "
                          "(on_typing: now - last_typing_ms < 1000 -> drop)")
    time.sleep(max(0, t0 + 1.2 - time.monotonic()))
    ua.send(nk.typing(a.sid, 3))
    ub.expect(nk.U_TYPING_EVT, why="more than 1 s later the next TYPING is relayed again (compare in ms)")


@test
def ping_gets_pong_with_same_value():
    a = user()
    u = nk.Udp()
    for t in (0x0123456789ABCDEF, 0xFEDCBA9876543210):
        u.send(nk.ping(a.sid, t))
        d = u.expect(nk.U_PONG, why="PING gets PONG back to the sender's address (on_ping)")
        check(d.body.t == t, f"PONG echoed 0x{d.body.t:016X}, PING sent 0x{t:016X}: get_u64 / put_u64 byte "
                             "order (cast to uint64_t before shifting)")


# ---------------------------------------------------------------- files

LEFTOVER = ("If FILE_EVT never comes: the first raw bytes arrived in the same recv() as DATA_HELLO, so "
            "they're still in inbuf after the frame; pass them to upload_bytes. "
            "Also: upload_bytes counts xfer_left down to 0, then broadcasts FILE_EVT to the room, "
            "uploader included")


def upload(owner, room, name, content, glue=1000):
    """FILE_UP_REQ, then a new connection: DATA_HELLO + the first `glue` raw bytes in ONE send()."""
    owner.send(nk.file_up_req(room, name, len(content)))
    ok = owner.expect(nk.FILE_UP_OK, why="FILE_UP_REQ for a valid room and size gets FILE_UP_OK "
                                         "(on_file_up_req: file_new + transfer_new)")
    d = nk.Conn()
    d.send(nk.data_hello(ok.tid, ok.token) + content[:glue])
    if len(content) > glue:
        time.sleep(0.1)
        d.send(content[glue:])
    return ok, d


def download(c, fid, size):
    """FILE_DOWN_REQ -> FILE_DOWN_OK -> new connection with DATA_HELLO -> raw bytes until close."""
    c.send(nk.file_down_req(fid))
    ok = c.expect(nk.FILE_DOWN_OK, why="FILE_DOWN_REQ for a finished upload gets FILE_DOWN_OK "
                                       "(on_file_down_req: file_get + transfer_new(0, id))")
    check(ok.size == size, f"FILE_DOWN_OK size = {ok.size}, but the upload had {size} bytes")
    d = nk.Conn()
    d.send(nk.data_hello(ok.tid, ok.token))
    data, how = d.read_all(limit=size + 65536)
    return ok, data, how


def check_download(data, how, content):
    if data != content:
        i = next((k for k, (x, y) in enumerate(zip(data, content)) if x != y), min(len(data), len(content)))
        raise AssertionError(f"download gave {len(data)} of {len(content)} bytes (then {how}), first "
                             f"difference at offset {i}. download_refill: fread up to 64 KiB -> client_send, "
                             "again each time outbuf drains, until xfer_left == 0")
    check(how in ("eof", "reset"),
          f"all {len(content)} bytes arrived but the data connection stayed open for {cfg.t:g} s: close it "
          "after the last byte (download_refill: xfer_left == 0 -> drop_client)")


@test
def upload_then_download_round_trip():
    a, b = user("up", [2]), user("down", [2])
    content = os.urandom(300_000)
    name = f"notes-{tok()}.bin"
    upload(a, 2, name, content)
    ea = a.expect(nk.FILE_EVT, lambda m: m.name == name, why=f"the uploader is in the room, so it gets FILE_EVT too. {LEFTOVER}")
    eb = b.expect(nk.FILE_EVT, lambda m: m.name == name, why="every member of the room gets FILE_EVT")
    check(ea == eb, f"uploader and member got different FILE_EVTs: {ea} vs {eb}")
    check((ea.room, ea.size, ea.frm) == (2, len(content), a.nick),
          f"FILE_EVT should be (room 2, size {len(content)}, from {a.nick!r}), got {ea}")
    _, data, how = download(b, ea.fid, len(content))
    check_download(data, how, content)


@test
def small_upload_in_the_same_send_as_data_hello():
    a = user("up", [1])
    content = b"tiny file! " * 10
    name = f"tiny-{tok()}.txt"
    upload(a, 1, name, content, glue=len(content))
    ev = a.expect(nk.FILE_EVT, lambda m: m.name == name,
                  why="DATA_HELLO and the whole 110-byte file came in ONE send(), so probably one recv(). After "
                      "DATA_HELLO switches the connection to ST_UPLOAD, the rest of inbuf is file data: "
                      "upload_bytes(inbuf, inlen)")
    _, data, how = download(a, ev.fid, len(content))
    check_download(data, how, content)


@test
def empty_file_upload():
    a = user("up", [1])
    name = f"empty-{tok()}.txt"
    upload(a, 1, name, b"")
    ev = a.expect(nk.FILE_EVT, lambda m: m.name == name,
                  why="a 0-byte upload is complete right after DATA_HELLO (on_data_hello: size 0 -> finish now)")
    check(ev.size == 0, f"FILE_EVT size should be 0, got {ev.size}")
    _, data, how = download(a, ev.fid, 0)
    check_download(data, how, b"")


@test
def tokens_are_single_use():
    a = user("up", [1])
    content = b"once only " * 50
    name = f"once-{tok()}.txt"
    up_ok, _ = upload(a, 1, name, content, glue=len(content))
    ev = a.expect(nk.FILE_EVT, lambda m: m.name == name, why=LEFTOVER)
    down_ok, data, how = download(a, ev.fid, len(content))
    check_download(data, how, content)
    again = nk.Conn()
    again.send(nk.data_hello(down_ok.tid, down_ok.token))
    data, how = again.read_all(limit=len(content) + 65536)
    check(content not in data, "the same download token worked twice: transfer_claim must free the slot (single use)")
    check(how in ("eof", "reset"), "a used download token must get the connection closed")
    again = nk.Conn()
    again.send(nk.data_hello(up_ok.tid, up_ok.token) + content)
    _, how = again.read_all(total=cfg.t)
    check(how in ("eof", "reset"), "a used upload token must get the connection closed")
    a.quiet(nk.FILE_EVT, lambda m: m.name == name, why="a reused upload token produced a second upload")


@test
def bad_token_closes_connection():
    d = nk.Conn()
    d.send(nk.data_hello(secrets.randbits(32), secrets.randbits(32)))
    data, how = d.read_all(total=cfg.t)
    # What the server sends before closing isn't pinned down by the protocol.
    # Accept nothing, or ERROR frames; require only that it closes.
    check(how in ("eof", "reset"), f"DATA_HELLO with an unknown token: the server must close the connection "
                                   f"(transfer_claim -> NULL -> drop_client), it stayed open for {cfg.t:g} s")
    frames = nk.split_frames(data)
    check(all(t == nk.ERROR for t, _ in frames),
          f"before closing, the server sent {[nk.describe(*f) for f in frames]}: only an ERROR makes sense here")


@test
def file_size_limit_is_50_mib():
    a = user("up", [1])
    for size, what in ((nk.MAX_FILE + 1, "50 MiB + 1"), (2**32 + 1, "4 GiB + 1 (would be 1 as a u32)")):
        a.send(nk.file_up_req(1, f"big-{tok()}.bin", size))
        e = a.expect((nk.ERROR, nk.FILE_UP_OK), why=f"FILE_UP_REQ of {what} bytes -> ERROR 3")
        check(isinstance(e, nk.Error) and e.code == 3,
              f"FILE_UP_REQ of {what} bytes: expected ERROR 3, got {e}. size is a u64: get_u64, then "
              "size > MAX_FILE -> ERROR 3")
    name = f"max-{tok()}.bin"
    a.send(nk.file_up_req(1, name, nk.MAX_FILE))
    ok = a.expect((nk.ERROR, nk.FILE_UP_OK), why="exactly 50 MiB is allowed (size > MAX_FILE is the error)")
    check(isinstance(ok, nk.FileUpOk), f"FILE_UP_REQ of exactly 50 MiB must be accepted, got {ok}")
    d = nk.Conn()                                      # claim the token, then give up after 7 bytes
    d.send(nk.data_hello(ok.tid, ok.token) + b"partial")
    time.sleep(0.2)
    d.close()
    a.quiet(nk.FILE_EVT, lambda m: m.name == name, why="an upload abandoned after 7 bytes must not be announced")
    text = "after abandoned upload " + tok()
    a.send(nk.say(1, text))
    a.expect(nk.MSG, lambda m: m.text == text, why="the server must survive an upload connection that dies early")


@test
def upload_to_bad_room_error_2():
    a = user()
    a.send(nk.file_up_req(99, "x.txt", 10))
    e = a.expect((nk.ERROR, nk.FILE_UP_OK), why="FILE_UP_REQ for room 99 -> ERROR 2")
    check(isinstance(e, nk.Error) and e.code == 2, f"FILE_UP_REQ for room 99: expected ERROR 2, got {e}")


@test
def download_unknown_file_is_not_offered():
    a = user()
    a.send(nk.file_down_req(0xFFFFFF00))
    # An ERROR (e.g. code 10) or silence are both accepted.
    # Offering a download of a file that doesn't exist is not.
    f = a.frame(timeout=1.0)
    check(f is None or f[0] != nk.FILE_DOWN_OK,
          "FILE_DOWN_OK for a file_id that was never uploaded: on_file_down_req must check file_get() "
          "(and complete) first")
    enter(a, 1)                                        # still usable


@test
def chat_flows_during_an_upload():
    a, b = user("up", [1]), user("b", [1])
    content = os.urandom(200_000)
    name = f"slow-{tok()}.bin"
    a.send(nk.file_up_req(1, name, len(content)))
    ok = a.expect(nk.FILE_UP_OK)
    d = nk.Conn()
    d.send(nk.data_hello(ok.tid, ok.token) + content[:100_000])
    text = "meanwhile " + tok()
    b.send(nk.say(1, text))
    a.expect(nk.MSG, lambda m: m.text == text,
             why="chat must keep flowing while an upload is half done: one poll() loop serves every socket; "
                 "never block in recv() on a data connection")
    d.send(content[100_000:])
    ev = a.expect(nk.FILE_EVT, lambda m: m.name == name, why=LEFTOVER)
    check(ev.size == len(content), f"FILE_EVT size {ev.size}, expected {len(content)}")


@test
def filename_is_metadata_not_a_path():
    a = user("up", [1])
    name = f"../../etc/passwd-{tok()}"
    content = b"not really a password file"
    upload(a, 1, name, content, glue=len(content))
    ev = a.expect(nk.FILE_EVT, lambda m: m.name == name,
                  why="FILE_EVT carries the client's name unchanged; store the file as uploads/<file_id> "
                      f"and never use the name as a path. {LEFTOVER}")
    _, data, how = download(a, ev.fid, len(content))
    check_download(data, how, content)


@test(slow=True)
def tokens_expire_after_30_s():
    a = user("up", [1])
    name = f"late-{tok()}.txt"
    a.send(nk.file_up_req(1, name, 10))
    ok = a.expect(nk.FILE_UP_OK)
    time.sleep(31)
    d = nk.Conn()
    d.send(nk.data_hello(ok.tid, ok.token) + b"0123456789")
    _, how = d.read_all(total=cfg.t)
    check(how in ("eof", "reset"), "a token used after 31 s must be rejected (TOKEN_TTL_MS: transfer_claim "
                                   "checks now < expires_ms) and the connection closed")
    a.quiet(nk.FILE_EVT, lambda m: m.name == name, why="an expired upload token must not complete an upload")


# ---------------------------------------------------------------- robustness

def _vanish(how):
    def t():
        a, x = user("a", [1]), user("x", [1])
        a.expect(nk.USER_EVT, lambda m: m.nick == x.nick and m.joined)
        x.send(nk.say(1, "this frame never ends")[:9])
        time.sleep(0.05)
        x.abort() if how == "reset" else x.close()
        a.expect(nk.USER_EVT, lambda m: m.nick == x.nick and not m.joined,
                 why="a client that vanishes in the middle of a frame is still dropped (recv() == 0 / -1)")
        text = "still alive " + tok()
        a.send(nk.say(1, text))
        a.expect(nk.MSG, lambda m: m.text == text, why="the server keeps serving the others")
        user()
    test(t, name=f"client_vanishing_mid_frame[{how}]")


for _how in ("close", "reset"):
    _vanish(_how)


@test
def malformed_frames_do_not_crash_server():
    junk = {"JOIN with 1 payload byte": nk.frame(nk.JOIN, b"\x01"),
            "SAY whose string runs past the frame": nk.frame(nk.SAY, struct.pack("!HH", 1, 500) + b"abc"),
            "FILE_UP_REQ cut short": nk.frame(nk.FILE_UP_REQ, b"\x00\x01\xff\xff"),
            "zero-length frame": b"\x00\x00\x00\x00"}
    for what, data in junk.items():
        x = user()
        x.send(data + nk.join(2))
        # Malformed -> drop the client or ignore the frame. Either is fine, as long
        # as an ignored frame leaves the stream in sync and the server keeps running.
        try:
            f = x.frame()
            while f is not None and not (f[0] == nk.HISTORY and nk.parse(*f).room == 2):
                f = x.frame()
            check(f is not None, f"after a {what}, the server neither dropped the client nor answered the "
                                 "JOIN that followed: validate lengths with rbuf (r.err) and skip the frame")
        except nk.Closed:
            pass
    user()                                             # still accepting logins


@test
def slow_reader_does_not_block_others():
    b = user("slow reader", [2], rcvbuf=4096)          # never reads after joining
    a = user("a", [2])
    big = "z" * FLOOD_TEXT
    for batch in range(100):                           # ~2.5 MB queued for b
        a.send(b"".join(nk.say(2, f"{batch}:{i}:{big}") for i in range(25)))
        for i in range(25):
            key = f"{batch}:{i}:"
            a.expect(nk.MSG, lambda m: m.text.startswith(key), timeout=max(cfg.t, 5),
                     why="B never reads, so its socket fills up. If A stops getting answers, the server is "
                         "blocked in send() to B: non-blocking sockets, keep unsent bytes in outbuf, "
                         "POLLOUT only while outlen > 0")


@test
def server_survives_peer_reset_with_data_queued():
    a = user("a", [3])
    b = user("b", [3], rcvbuf=4096)
    filler = "y" * FLOOD_TEXT
    for round_ in range(2):
        a.send(b"".join(nk.say(3, f"{round_}:{i}:{filler}") for i in range(100)))
        for i in range(100):
            key = f"{round_}:{i}:"
            a.expect(nk.MSG, lambda m: m.text.startswith(key),
                     why="B reset its connection while the server had data queued for it. Ignore SIGPIPE "
                         "(signal(SIGPIPE, SIG_IGN) or MSG_NOSIGNAL) and drop B on EPIPE / ECONNRESET")
        if round_ == 0:
            b.abort()
    a.expect(nk.USER_EVT, lambda m: m.nick == b.nick and not m.joined,
             why="B's reset must end in drop_client -> USER_EVT left")
    user()


if __name__ == "__main__":
    nk.main("Ether server tests against {host}:5050 (UDP 5051)", before=probe)
