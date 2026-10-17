"""AISStream over a TLS socket. The stdlib is enough; the clock stays small.

Derived from piclock3-ais-radar (MIT). The server sends UTF-8 JSON in
binary frames, and sometimes text frames. Client frames are masked, which
is the rule, and a ping is answered so a quiet box does not get dropped.
"""
import base64
import json
import os
import socket
import ssl
import struct
import zlib

HOST = 'stream.aisstream.io'
PATH = '/v0/stream'
PORT = 443

MESSAGE_TYPES = (
    'PositionReport',
    'StandardClassBPositionReport',
    'ExtendedClassBPositionReport',
    'ShipStaticData',
    'StaticDataReport',
)


def subscription(api_key, box):
    """box is (latmin, latmax, lonmin, lonmax). Corners are [lat, lon]."""
    latmin, latmax, lonmin, lonmax = box
    return {
        'APIKey': api_key,
        'BoundingBoxes': [[[latmax, lonmin], [latmin, lonmax]]],
        'FilterMessageTypes': list(MESSAGE_TYPES),
    }


def open_stream(timeout=20):
    """(ssl socket, bytes already read past the handshake)."""
    raw = socket.create_connection((HOST, PORT), timeout)
    try:
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=HOST)
    except Exception:
        raw.close()
        raise
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    request = (
        'GET %s HTTP/1.1\r\n'
        'Host: %s\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n' % (PATH, HOST, key)
    )
    sock.sendall(request.encode('ascii'))
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise OSError('handshake closed')
        buf += chunk
        if len(buf) > 65536:
            sock.close()
            raise OSError('handshake too long')
    head, rest = buf.split(b'\r\n\r\n', 1)
    status = head.split(b'\r\n', 1)[0]
    if b' 101 ' not in status:
        sock.close()
        raise OSError('handshake refused')
    sock.settimeout(timeout)
    return sock, rest


def send_text(sock, text):
    _send(sock, 0x1, text.encode('utf-8'))


def _send(sock, opcode, payload):
    frame = bytearray()
    frame.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        frame.append(0x80 | n)
    elif n < 65536:
        frame.append(0x80 | 126)
        frame += struct.pack('!H', n)
    else:
        frame.append(0x80 | 127)
        frame += struct.pack('!Q', n)
    mask = os.urandom(4)
    frame += mask
    frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(frame)


class Reader:
    """yields text messages. Pings are answered. Close ends the iterator."""

    def __init__(self, sock, leftover, should_stop):
        self.sock = sock
        self.buf = leftover or b''
        self.should_stop = should_stop

    def messages(self):
        acc = bytearray()
        while not self.should_stop():
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read_exact(8))[0]
            mask = self._read_exact(4) if b1 & 0x80 else b''
            payload = self._read_exact(n) if n else b''
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                return
            if opcode == 0x9:
                _send(self.sock, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if b0 & 0x40:
                try:
                    payload = zlib.decompress(payload + b'\x00\x00\xff\xff', -15)
                except zlib.error:
                    acc = bytearray()
                    continue
            if opcode in (0x1, 0x2):
                acc = bytearray()
            if opcode in (0x1, 0x2, 0x0):
                acc += payload
                if fin:
                    yield acc.decode('utf-8', 'replace')
                    acc = bytearray()

    def _read_exact(self, n):
        while len(self.buf) < n:
            if self.should_stop():
                raise OSError('stopped')
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                raise OSError('stream closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def dumps(api_key, box):
    return json.dumps(subscription(api_key, box))
