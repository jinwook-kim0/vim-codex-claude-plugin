"""Small RFC 6455 client for Codex's local Unix WebSocket transport."""
import base64
import hashlib
import json
import os
import socket
import struct
import threading


class WebSocket:
    MAX_MESSAGE = 32 * 1024 * 1024

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(15)
        self.sock.connect(str(path))
        self.lock = threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                   f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                   'Sec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode())
        # Do not consume the first frame together with the HTTP headers.
        headers = bytearray()
        while not headers.endswith(b'\r\n\r\n'):
            headers.extend(self._exact(1))
            if len(headers) > 16384:
                raise ValueError('Oversized WebSocket handshake')
        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if not headers.startswith(b'HTTP/1.1 101 ') or expected.lower() not in headers.lower():
            raise ValueError('Codex rejected the WebSocket handshake')
        self.sock.settimeout(None)

    def _exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise EOFError('Codex connection closed')
            data.extend(part)
        return bytes(data)

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += b'\xfe' + struct.pack('!H', length)
        else:
            header += b'\xff' + struct.pack('!Q', length)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self.lock:
            self.sock.sendall(header + mask + masked)

    def send(self, value):
        self._send(1, json.dumps(value, ensure_ascii=False).encode())

    def receive(self):
        chunks = bytearray()
        while True:
            first, second = self._exact(2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self._exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._exact(8))[0]
            if length > self.MAX_MESSAGE or len(chunks) + length > self.MAX_MESSAGE:
                raise ValueError('Oversized Codex message')
            mask = self._exact(4) if second & 128 else None
            payload = self._exact(length)
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            opcode = first & 15
            if opcode == 8:
                raise EOFError('Codex connection closed')
            if opcode == 9:
                self._send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise ValueError('Unexpected binary Codex message')
            chunks.extend(payload)
            if first & 128:
                return json.loads(chunks)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class RPC:
    def __init__(self, path, notification, request):
        self.ws = WebSocket(path)
        self.notification = notification
        self.request_handler = request
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def call(self, method, params, timeout=45):
        with self.lock:
            self.sequence += 1
            request_id = self.sequence
            event, slot = threading.Event(), {}
            self.pending[request_id] = (event, slot)
        try:
            self.ws.send({'id': request_id, 'method': method, 'params': params})
            if not event.wait(timeout):
                raise TimeoutError(f'Codex timed out: {method}')
            if 'error' in slot:
                raise RuntimeError(str(slot['error']))
            return slot.get('result', {})
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def _reader(self):
        try:
            while not self.closed:
                message = self.ws.receive()
                if 'method' in message:
                    if 'id' in message:
                        self.request_handler(message)
                    else:
                        self.notification(message['method'], message.get('params', {}))
                else:
                    with self.lock:
                        pending = self.pending.get(message.get('id'))
                        if pending:
                            pending[1].update(message)
                            pending[0].set()
        except (OSError, EOFError, ValueError) as error:
            if not self.closed:
                with self.lock:
                    for event, slot in self.pending.values():
                        slot['error'] = str(error)
                        event.set()
                self.notification('bridge/disconnected', {'message': str(error)})

    def close(self):
        self.closed = True
        with self.lock:
            for event, slot in self.pending.values():
                slot['error'] = 'Codex connection closed'
                event.set()
        self.ws.close()
