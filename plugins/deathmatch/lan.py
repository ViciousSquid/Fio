# SPDX-License-Identifier: GPL-2.0-or-later
"""LAN discovery and bounded JSON transport. No code or assets cross the wire."""
import json
import queue
import socket
import struct
import threading
import time
import uuid

PROTOCOL = "fio-arena-1"
DISCOVERY_PORT = 27960
GAME_PORT = 27961
MAX_PACKET = 1024 * 1024
FRAME_TIMEOUT = 10
# A connection with nothing to say sends this often, so the frame deadline
# only ever catches a peer that has stalled or gone: a guest loading the map
# or sitting in a dialog sends no input, and was dropped after ten seconds.
KEEPALIVE = 2.5
PING = {"type": "ping"}
# A client sends 20 inputs a second; a peer far beyond that is not a client.
MAX_MESSAGE_RATE = 200
MAX_OFFERS = 64


def encode(message):
    data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_PACKET:
        raise ValueError("LAN message exceeds 1 MiB")
    return struct.pack("!I", len(data)) + data


class Channel:
    def __init__(self, sock, received, disconnected, rate=None):
        self.sock = sock
        self.sock.settimeout(1)
        self.received, self.disconnected = received, disconnected
        self.rate, self.opened = rate, time.monotonic()
        self.closed = threading.Event()
        self.outgoing = queue.Queue(32)
        self._close_lock = threading.Lock()
        for target in (self._read, self._write):
            threading.Thread(target=target, daemon=True).start()

    def send(self, message):
        if self.closed.is_set():
            return False
        try:
            self.outgoing.put_nowait(encode(message))
            return True
        except queue.Full:
            # A stalled link: the frame being written has its own deadline,
            # and state this stale is worthless to the peer once it recovers.
            return False
        except (ValueError, TypeError):
            self.close()
            return False

    def _exact(self, count):
        result = bytearray()
        deadline = time.monotonic() + FRAME_TIMEOUT
        while len(result) < count and not self.closed.is_set():
            if time.monotonic() > deadline:
                raise OSError("LAN frame timed out")
            try:
                part = self.sock.recv(count - len(result))
            except socket.timeout:
                continue
            if not part:
                raise OSError("Peer disconnected")
            result.extend(part)
        if len(result) != count:
            raise OSError("Closed")
        return result

    def _read(self):
        window, count = time.monotonic(), 0
        try:
            while not self.closed.is_set():
                size, = struct.unpack("!I", self._exact(4))
                if not 0 < size <= MAX_PACKET:
                    break
                message = json.loads(self._exact(size))
                if not isinstance(message, dict):
                    break
                if message == PING:
                    continue        # the transport's own; never the game's
                if self.rate is not None:
                    now = time.monotonic()
                    if now - window >= 1:
                        window, count = now, 0
                    count += 1
                    if count > self.rate:
                        break
                self.received(message)
        except (OSError, ValueError, RecursionError, queue.Full):
            pass
        finally:
            self.close()

    def _write(self):
        sent = time.monotonic()
        try:
            while not self.closed.is_set():
                try:
                    data = self.outgoing.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - sent < KEEPALIVE:
                        continue
                    data = encode(PING)
                sent = time.monotonic()
                # Not sendall(): its socket timeout would be the budget for
                # the whole frame, one second for a map of up to 1 MiB.
                view, deadline = memoryview(data), time.monotonic() + FRAME_TIMEOUT
                while view and not self.closed.is_set():
                    if time.monotonic() > deadline:
                        raise OSError("LAN frame timed out")
                    try:
                        view = view[self.sock.send(view):]
                    except socket.timeout:
                        continue
        except OSError:
            pass
        finally:
            self.close()

    def close(self):
        with self._close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
        self.disconnected()


class LanHost:
    def __init__(self, name, mode, *, address="0.0.0.0", port=GAME_PORT,
                 discovery_port=DISCOVERY_PORT, capacity=8):
        self.name, self.mode, self.capacity = name[:48], mode, capacity
        self.events = queue.Queue(256)
        self.peers = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((address, port)); self.listener.listen(capacity)
            self.listener.settimeout(0.2)
            self.port = self.listener.getsockname()[1]
            self.discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.discovery.bind((address, discovery_port))
            self.discovery.settimeout(0.2)
            self.discovery_port = self.discovery.getsockname()[1]
        except OSError:
            self.listener.close(); self.discovery.close()
            raise
        self._threads = [threading.Thread(target=target, daemon=True)
                         for target in (self._accept, self._discover)]
        for thread in self._threads:
            thread.start()

    def _event(self, *event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def _accept(self):
        while not self.closed.is_set():
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                # close() may have collected the peers while accept() returned.
                if self.closed.is_set() or len(self.peers) >= self.capacity - 1:
                    sock.close(); continue
                key = "lan_" + uuid.uuid4().hex[:12]
                channel = Channel(sock, lambda msg, k=key: self._event("message", k, msg),
                                  lambda k=key: self._lost(k), rate=MAX_MESSAGE_RATE)
                self.peers[key] = channel
            self._event("connected", key)

    def _lost(self, key):
        with self.lock:
            self.peers.pop(key, None)
        self._event("disconnected", key)

    def _discover(self):
        while not self.closed.is_set():
            try:
                packet, address = self.discovery.recvfrom(1024)
                query = json.loads(packet)
                if query != {"protocol": PROTOCOL, "type": "discover"}:
                    continue
                with self.lock:
                    count = len(self.peers) + 1
                reply = dict(protocol=PROTOCOL, type="offer", name=self.name, mode=self.mode,
                             port=self.port, players=count, capacity=self.capacity)
                self.discovery.sendto(json.dumps(reply).encode(), address)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                if self.closed.is_set():
                    break

    def send(self, key, message):
        with self.lock:
            peer = self.peers.get(key)
        return peer.send(message) if peer else False

    def connections(self):
        """``{key: seconds connected}`` of the live peers.

        The event queue is bounded and may drop a ``disconnected`` event, so
        this, not the events, is the authority on who is still here.
        """
        now = time.monotonic()
        with self.lock:
            return {key: now - channel.opened for key, channel in self.peers.items()}

    def drop(self, key):
        with self.lock:
            peer = self.peers.get(key)
        if peer is not None:
            peer.close()

    def drain(self):
        while True:
            try:
                yield self.events.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.closed.set()
        # A thread blocked in accept()/recvfrom() keeps its port bound until
        # it wakes, so wait for both: hosting again at once must succeed.
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(1)
        self.listener.close(); self.discovery.close()
        with self.lock:
            peers = list(self.peers.values())
        for channel in peers:
            channel.close()


class LanClient:
    def __init__(self, address, port=GAME_PORT, name="Player", team="auto"):
        self.events = queue.Queue(128)
        self.channel = None
        self.closed = threading.Event()
        self.lost, self._lost_reported = threading.Event(), False
        self.address, self.port = address, port
        self.name, self.team = name[:32], team
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        try:
            sock = socket.create_connection((self.address, self.port), timeout=3)
            if self.closed.is_set():
                sock.close(); return
            self.channel = Channel(sock, lambda msg: self.events.put_nowait(msg), self.lost.set)
            if self.closed.is_set():
                # close() ran between the check above and the assignment.
                self.channel.close(); return
            self.send(dict(type="hello", protocol=PROTOCOL, name=self.name, team=self.team))
        except (OSError, ValueError, OverflowError) as error:
            # ValueError: the resolver's IDNA codec rejects names such as "a..b".
            self._event(dict(type="error", message=str(error)))

    def _event(self, message):
        try:
            self.events.put_nowait(message)
        except queue.Full:
            pass

    def send(self, message):
        return self.channel.send(message) if self.channel else False

    def drain(self):
        while True:
            try:
                yield self.events.get_nowait()
            except queue.Empty:
                break
        # Never queued: a full queue is one of the reasons a channel closes,
        # and the game must still learn that it did.
        if self.lost.is_set() and not self._lost_reported:
            self._lost_reported = True
            yield {"type": "disconnected"}

    def close(self):
        self.closed.set()
        if self.channel:
            self.channel.close()


class Discovery:
    """One short asynchronous UDP query; the GUI polls results without blocking."""
    def __init__(self, *, destinations=("255.255.255.255", "127.0.0.1"), port=DISCOVERY_PORT):
        self.results = queue.Queue()
        self.error = ""
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(destinations, port), daemon=True).start()

    def _run(self, destinations, port):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.15)
            query = json.dumps(dict(protocol=PROTOCOL, type="discover")).encode()
            for destination in destinations:
                try:
                    sock.sendto(query, (destination, port))
                except OSError as error:
                    self.error = str(error)
            end = time.monotonic() + 1.2
            found = 0
            while time.monotonic() < end and found < MAX_OFFERS:
                try:
                    data, address = sock.recvfrom(2048)
                    offer = json.loads(data)
                    if (isinstance(offer, dict) and offer.get("protocol") == PROTOCOL
                            and offer.get("type") == "offer" and type(offer.get("port")) is int
                            and 1 <= offer["port"] <= 65535):
                        offer["address"] = address[0]
                        self.results.put(offer)
                        found += 1
                except socket.timeout:
                    continue
                except (ValueError, OSError):
                    continue
        except OSError as error:
            self.error = str(error)
        finally:
            if sock is not None:
                sock.close()
            self.done.set()
