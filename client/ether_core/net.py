"""asyncio network client: TCP control connection, UDP endpoint, file data connections.

Everything runs on the event loop that ether_gui shares with Qt (qasync), so never block.
on_event is called on that loop with events.* dataclasses.
"""
from __future__ import annotations

from typing import Callable

import asyncio
import os
import socket
import struct
import time


from . import proto
from .events import Event, ServerInfo, Welcome, Error, LoginError, Disconnected, TypingSeen, Pong, EtherError

Progress = Callable[[int, int], None]      # progress(done_bytes, total_bytes)


def now_ms() -> int:  # unused leftover (returns ns); _now_ms below is the one PING uses
    return time.monotonic_ns()

async def discover(timeout: float = 1.0) -> list[ServerInfo]:
    """Broadcast one DISCOVER on UDP_PORT and collect replies for `timeout` seconds.
    The GUI calls this again every ~1 s, so one broadcast per call is enough."""

    found = {}
    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):

            info = proto.decode_udp(data)
            if(isinstance(info, ServerInfo)):
                info.host = addr[0]
                found[(info.host,info.tcp_port)] = info
                
    # Windows sends 255.255.255.255 out of ONE adapter only (here a virtual one),
    # so broadcast once from a socket bound to each local IPv4 address
    loop = asyncio.get_running_loop()
    ips = {"0.0.0.0"}                       # the default adapter
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass                                # can't list adapters, default only
    transports = []
    try:
        for ip in ips:
            if ip.startswith("127."):
                continue
            try:
                tr, _ = await loop.create_datagram_endpoint(P, local_addr=(ip, 0), allow_broadcast=True)
                transports.append(tr)
                tr.sendto(proto.encode_discover(), ('255.255.255.255', proto.UDP_PORT))
            except OSError:
                pass                        # this adapter can't broadcast, skip it
        await asyncio.sleep(timeout)
    finally:
        for tr in transports:
            tr.close()

    return list(found.values())


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000     # ms clock for PING/PONG round trips


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, client):
        self.c = client

    def datagram_received(self, data, addr):
        if addr[0] != self.c._server_ip:  # only trust datagrams from our own server
            return
        m = proto.decode_udp(data)
        if isinstance(m, TypingSeen):
            self.c._emit(m)

        elif isinstance(m, proto.PongEcho):
            self.c._emit(Pong(_now_ms() - m.client_time_ms))  # rtt = now - the time we put in the PING

    def error_received(self, exc):
        pass  # windows reports ICMP "port unreachable" here: ignore, UDP is best-effort


class EtherClient:
    def __init__(self, on_event: Callable[[Event], None]):
        self.on_event = on_event
        self.session_id = 0                 # set by connect(); shown in the GUI status bar
        self.udp_port = 0                   # local UDP port (bound to 0 = OS picks); status bar
        self._r = None
        self._w = None
        self._reader_task = None
        self._closing = False
        self._udp = None # socket
        self._udp_task = None
        self._server_ip = None
        self._pending = None
        self._pending_codes = ()
        self._file_lock = asyncio.Lock()

    async def _read_frame(self):
        # readexactly waits for exactly n bytes = the C inbuf/inlen framing, done by asyncio
        (n,) = struct.unpack('!I', await self._r.readexactly(4))
        if not 1 <= n <= proto.MAX_FRAME:
            raise ConnectionError("bad frame length")
        body = await self._r.readexactly(n)
        return body[0], body[1:] # type, payload
    
    async def _send(self, frame: bytes):
        self._w.write(frame)    #queue bytes
        await self._w.drain() # wait if kernel buf is 

    def _fail_pending(self, exc):
        if self._pending and not self._pending.done():
            self._pending.set_exception(exc)
    async def _request(self, frame: bytes, codes):
        if not self._w:
            raise ConnectionError("not connected")    
        async with self._file_lock:  # replies carry no request id: one request at a time keeps them in order
            self._pending = asyncio.get_running_loop().create_future()  # the "box" _reader fills
            self._pending_codes = codes

            try:
                await self._send(frame)
                return await asyncio.wait_for(self._pending, 10)
            finally:
                self._pending = None

        
    def _udp_send(self, data: bytes):
        try:
            if self._udp:
                self._udp.sendto(data, (self._server_ip, proto.UDP_PORT))
        except Exception:
            pass

    async def _udp_loop(self):
        n = 0
        while True:
            await asyncio.sleep(2)
            self._udp_send(proto.encode_ping(self.session_id, _now_ms()))

            n += 1
            if n % 5 == 0:  # every 10 s: refresh our UDP address on the server (NAT mappings expire)
                self._udp_send(proto.encode_udp_hello(self.session_id))

    def _emit(self, ev):
        try:
            self.on_event(ev)   # hand the event to the GUI/CLI
        except Exception:
            pass                # their bug must not kill our network code

    async def _abort(self):
        if self._w:
            self._w.close()     # failed connect: close what we opened
        self._r = self._w = None

        if self._udp:
            self._udp.close()
            self._udp = None


    async def connect(self, host: str, port: int, nickname: str, password: str) -> Welcome:
        """Log in over TCP and set up the UDP endpoint (UDP_HELLO, resent every 10 s).
        Emits the Welcome event and returns it. Raises LoginError if the server answers ERROR,
        OSError if unreachable. From then on: incoming frames -> on_event, a periodic
        PING whose PONG becomes Pong(rtt_ms), and Disconnected(reason) when the connection drops."""

        self._host, self._port = host, port
        try:
            self._r, self._w = await asyncio.wait_for(asyncio.open_connection(host,port),5)
            await self._send(proto.encode_login(nickname, password))

            t, p = await asyncio.wait_for(self._read_frame(),5)
            msg = proto.decode_tcp(t,p)

            if isinstance(msg, Error):
                raise LoginError(msg.code, msg.message)

            if not isinstance(msg, Welcome):
                raise ConnectionError("expected WELCOME")
            
            self.session_id = msg.session_id

            self._server_ip = self._w.get_extra_info('peername')[0]  # server ip, taken from the TCP socket
            loop = asyncio.get_running_loop()
            self._udp, _ = await loop.create_datagram_endpoint(lambda: _Udp(self), local_addr=('0.0.0.0',0))
            self.udp_port = self._udp.get_extra_info('sockname')[1]  # port 0 -> the OS picked a free one

            self._udp_send(proto.encode_udp_hello(self.session_id))  # tell the server where our UDP lives
            self._udp_task = asyncio.create_task(self._udp_loop())

            self._reader_task = asyncio.create_task(self._reader())
        except asyncio.IncompleteReadError:
            await self._abort()
            raise ConnectionError("Server closed the connection")
        
        except BaseException:  # the GUI never calls close() after a failed connect: clean up ourselves
            await self._abort()
            raise

        self._emit(msg)  # Welcome last: the reader already runs, so the GUI's instant join() gets replies
        return msg
    
    async def _reader(self):
        reason = "conn lost"
        try:
            while True:
                t, payload = await self._read_frame()
                try:
                    msg = proto.decode_tcp(t,payload)
                except ValueError:
                    continue
                if isinstance(msg, proto.Bye):
                    reason = "server closed the session"
                    break

                if isinstance(msg, (proto.FileUpOk, proto.FileDownOk)):
                    if self._pending and not self._pending.done():
                        self._pending.set_result(msg)  # reply to the waiting upload/download
                elif isinstance(msg, Error) and self._pending and msg.code in self._pending_codes:
                    self._fail_pending(EtherError(msg.code,msg.message))  # an ERROR that answers that request
                else:
                    self._emit(msg)  # everything else -> GUI / CLI

        except (asyncio.IncompleteReadError, OSError) as e:
            reason = str(e) or reason
        finally:

            self._fail_pending(ConnectionError(reason))  # nobody waits forever on a dead connection
            if not self._closing:  # exactly once, and not after our own close()
                self._emit(Disconnected(reason))

    async def close(self) -> None:
        """Send BYE, close every socket, stop background tasks. Safe to call twice."""
        
        if self._closing: #already closed?
            return

        self._closing = True  # tells _reader: don't emit Disconnected
        if self._w:
            try:
                self._w.write(proto.encode_bye())
                await asyncio.wait_for(self._w.drain(), 0.5)
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        
        if self._w:
            self._w.close()

        if self._udp_task:
            self._udp_task.cancel()

        self._fail_pending(ConnectionError("client closed"))

        if self._udp:
            self._udp.close()

        self._udp = None

        self._r = self._w = None

    async def join(self, room_id: int) -> None:
        """Server answers with History then Users events."""
        await self._send(proto.encode_join(room_id))

    async def leave(self, room_id: int) -> None:
        await self._send(proto.encode_leave(room_id))

    async def say(self, room_id: int, text: str) -> None:
        """Our own message comes back as a MsgReceived event (server orders all messages)."""
        
        if len(text.encode()) > proto.MAX_TEXT:
            raise ValueError("message too long")
        
        await self._send(proto.encode_say(room_id, text))  # not shown locally: the server's echo shows it

    async def typing(self, room_id: int) -> None:
        """UDP TYPING. The GUI already throttles to 1/s."""
        self._udp_send(proto.encode_typing(self.session_id, room_id))


    async def upload(self, room_id: int, path: str, progress: Progress | None = None) -> None:
        """FILE_UP_REQ -> FILE_UP_OK, then stream the file on a new data connection.
        Returns when every byte is sent. Raises EtherError if the server answers ERROR
        (FILE_UP_OK carries no request id: replies arrive in request order)."""
        
        size, name = os.path.getsize(path), os.path.basename(path)
        ok = await self._request(proto.encode_file_up_req(room_id, name, size), (2,3,4))
        # a NEW TCP conn = the data conn; the transfer runs outside the lock, so uploads can overlap
        r, w = await asyncio.open_connection(self._host, self._port)
        try:
            w.write(proto.encode_data_hello(ok.transfer_id, ok.token))
            done = 0
            with open(path, 'rb') as f:
                while chunk := f.read(65536):
                    w.write(chunk)
                    await w.drain()
                    done += len(chunk)
                    if progress:
                        progress(done, size)
        finally:
            w.close()

    async def download(self, file_id: int, dest: str, progress: Progress | None = None) -> None:
        """FILE_DOWN_REQ -> FILE_DOWN_OK, then receive `size` bytes into dest on a new data connection.
        Raises EtherError if the server answers ERROR."""
        ok = await self._request(proto.encode_file_down_req(file_id),(4,10))
        r, w = await asyncio.open_connection(self._host, self._port)
        try:
            w.write(proto.encode_data_hello(ok.transfer_id, ok.token))
            await w.drain()
            done = 0
            with open(dest, 'wb') as f:
                while done < ok.size:
                    chunk = await r.read(min(65536, ok.size - done))
                    if not chunk:
                        raise ConnectionError(f"download cut short at {done} of {ok.size} bytes")
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, ok.size)
        except BaseException:  # never leave half a file behind
            try:
                os.remove(dest)
            except OSError:
                pass
            raise

        finally:
            w.close()
