from __future__ import print_function

from minecraft import SUPPORTED_MINECRAFT_VERSIONS
from minecraft.networking import connection
from minecraft.networking import types
from minecraft.networking import packets
from minecraft.networking.packets import clientbound
from minecraft.networking.packets import serverbound

from future.utils import raise_

import unittest
import threading
import logging
import socket
import json
import sys
import zlib
import hashlib
import uuid


VERSIONS = sorted(SUPPORTED_MINECRAFT_VERSIONS.items(), key=lambda i: i[1])
VERSIONS = [v for (v, p) in VERSIONS]

THREAD_TIMEOUT_S = 2


class FakeClientDisconnect(Exception):
    """ Raised by 'FakeClientHandler.read_packet' if the client has cleanly
        disconnected prior to the call.
    """


class FakeServerDisconnect(Exception):
    """ May be raised within 'FakeClientHandler.handle_*' in order to terminate
        the client's connection. 'message' is provided as an argument to
        'handle_play_server_disconnect'.
    """
    def __init__(self, message=None):
        self.message = message


class FakeServerTestSuccess(Exception):
    """ May be raised from within 'FakeClientHandler.handle_*' or from a
        'Connection' packet listener in order to terminate a 'FakeServerTest'
        successfully.
    """


class FakeClientHandler(object):
    """ Represents a single client connection being handled by a 'FakeServer'.
        The methods of the form 'handle_*' may be overridden by subclasses to
        customise the behaviour of the server.
    """

    __slots__ = 'server', 'socket', 'socket_file', 'packets', \
                'compression_enabled', 'user_uuid', 'user_name'

    def __init__(self, server, socket):
        self.server = server
        self.socket = socket
        self.socket_file = socket.makefile('rb', 0)
        self.compression_enabled = False
        self.user_uuid = None
        self.user_name = None

    def run(self):
        # Communicate with the client until disconnected.
        try:
            self._run_handshake()
            self.socket.shutdown(socket.SHUT_RDWR)
        finally:
            self.socket.close()
            self.socket_file.close()

    def handle_play_start(self):
        # Called upon entering the play state.
        self.write_packet(clientbound.play.JoinGamePacket(
            entity_id=0, game_mode=0, dimension=0, difficulty=2, max_players=1,
            level_type='default', reduced_debug_info=False))

    def handle_play_packet(self, packet):
        # Called upon each packet received after handle_play_start() returns.
        if isinstance(packet, serverbound.play.ChatPacket):
            assert len(packet.message) <= packet.max_length
            self.write_packet(clientbound.play.ChatMessagePacket(json.dumps({
                'translate': 'chat.type.text',
                'with': [self.username, packet.message],
            })))

    def handle_play_client_disconnect(self):
        # Called when the client cleanly terminates the connection during play.
        pass

    def handle_play_server_disconnect(self, message=None):
        # Called when the server cleanly terminates the connection during play,
        # i.e. by raising FakeServerDisconnect from a handler.
        message = 'Disconnected.' if message is None else message
        self.write_packet(clientbound.play.DisconnectPacket(
            json_data=json.dumps({'text': message})))

    def write_packet(self, packet):
        # Send and log a clientbound packet.
        packet.context = self.server.context
        logging.debug('[S-> ] %s' % packet)
        packet.write(self.socket, **(
            {'compression_threshold': self.server.compression_threshold}
            if self.compression_enabled else {}))

    def read_packet(self):
        # Read and log a serverbound packet from the client, or raises
        # FakeClientDisconnect if the client has cleanly disconnected.
        buffer = self._read_packet_buffer()
        packet_id = types.VarInt.read(buffer)
        if packet_id in self.packets:
            packet = self.packets[packet_id](self.server.context)
            packet.read(buffer)
        else:
            packet = packets.Packet(self.server.context, id=packet_id)
        logging.debug('[ ->S] %s' % packet)
        return packet

    def _run_handshake(self):
        # Enter the initial (i.e. handshaking) state of the connection.
        self.packets = self.server.packets_handshake
        packet = self.read_packet()
        assert isinstance(packet, serverbound.handshake.HandShakePacket)
        if packet.next_state == 1:
            self._run_status()
        elif packet.next_state == 2:
            self._run_handshake_play(packet)
        else:
            raise AssertionError('Unknown state: %s' % packet.next_state)

    def _run_handshake_play(self, packet):
        # Prepare to transition from handshaking to play state (via login),
        # using the given serverbound HandShakePacket to perform play-specific
        # processing.
        if packet.protocol_version == self.server.context.protocol_version:
            self._run_login()
        else:
            if packet.protocol_version < self.server.context.protocol_version:
                msg = 'Outdated client! Please use %s' \
                      % self.server.minecraft_version
            else:
                msg = "Outdated server! I'm still on %s" \
                      % self.server.minecraft_version
            self.write_packet(clientbound.login.DisconnectPacket(
                json_data=json.dumps({'text': msg})))

    def _run_login(self):
        # Enter the login state of the connection.
        self.packets = self.server.packets_login
        packet = self.read_packet()
        assert isinstance(packet, serverbound.login.LoginStartPacket)

        if self.server.compression_threshold is not None:
            self.write_packet(clientbound.login.SetCompressionPacket(
                threshold=self.server.compression_threshold))
            self.compression_enabled = True

        self.user_name = packet.name
        self.user_uuid = uuid.UUID(bytes=hashlib.md5(
            ('OfflinePlayer:%s' % self.user_name).encode('utf8')).digest())

        self.write_packet(clientbound.login.LoginSuccessPacket(
            UUID=str(self.user_uuid), Username=self.user_name))

        self._run_playing()

    def _run_playing(self):
        # Enter the playing state of the connection.
        self.packets = self.server.packets_playing
        client_disconnected = False
        try:
            self.handle_play_start()
            try:
                while True:
                    self.handle_play_packet(self.read_packet())
            except FakeClientDisconnect:
                client_disconnected = True
                self.handle_play_client_disconnect()
        except FakeServerDisconnect as e:
            if not client_disconnected:
                self.handle_play_server_disconnect(message=e.message)

    def _run_status(self):
        # Enter the status state of the connection.
        self.packets = self.server.packets_status

        packet = self.read_packet()
        assert isinstance(packet, serverbound.status.RequestPacket)

        packet = clientbound.status.ResponsePacket()
        packet.json_response = json.dumps({
            'version': {
                'name':     self.server.minecraft_version,
                'protocol': self.server.context.protocol_version},
            'players': {
                'max':      1,
                'online':   0,
                'sample':   []},
            'description': {
                'text':     'FakeServer'}})
        self.write_packet(packet)

        try:
            packet = self.read_packet()
        except FakeClientDisconnect:
            return

        assert isinstance(packet, serverbound.status.PingPacket)
        self.write_packet(clientbound.status.PingResponsePacket(
            time=packet.time))

    def _read_packet_buffer(self):
        # Read a serverbound packet in the form of a raw buffer, or raises
        # FakeClientDisconnect if the client has cleanly disconnected.
        try:
            length = types.VarInt.read(self.socket_file)
        except EOFError:
            raise FakeClientDisconnect
        buffer = packets.PacketBuffer()
        while len(buffer.get_writable()) < length:
            data = self.socket_file.read(length - len(buffer.get_writable()))
            buffer.send(data)
        buffer.reset_cursor()
        if self.compression_enabled:
            data_length = types.VarInt.read(buffer)
            if data_length > 0:
                data = zlib.decompress(buffer.read())
                assert len(data) == data_length, \
                    '%s != %s' % (len(data), data_length)
                buffer.reset()
                buffer.send(data)
                buffer.reset_cursor()
        return buffer


class FakeServer(object):
    """
        A rudimentary implementation of a Minecraft server, suitable for
        testing features of minecraft.networking.connection.Connection that
        require a full connection to be established.

        The server listens on a local TCP socket and accepts client connections
        in serial, in a single-threaded manner. It responds to status queries,
        performs handshake and login, and, by default, echoes any chat messages
        back to the client until it disconnects.1~

        The behaviour of the server can be customised by writing subclasses of
        FakeClientHandler, overriding its public methods of the form
        'handle_*', and providing the class to the FakeServer as its
        'client_handler_type'.
    """

    __slots__ = 'listen_socket', 'compression_threshold', 'context', \
                'minecraft_version', 'client_handler_type', \
                'packets_handshake', 'packets_login', 'packets_playing', \
                'packets_status', 'lock', 'stopping'

    def __init__(self, minecraft_version=None, compression_threshold=None,
                 client_handler_type=FakeClientHandler):
        if minecraft_version is None:
            minecraft_version = VERSIONS[-1][0]

        self.minecraft_version = minecraft_version
        self.compression_threshold = compression_threshold
        self.client_handler_type = client_handler_type

        protocol_version = SUPPORTED_MINECRAFT_VERSIONS[minecraft_version]
        self.context = connection.ConnectionContext(
            protocol_version=protocol_version)

        self.packets_handshake = {
            p.get_id(self.context): p for p in
            serverbound.handshake.get_packets(self.context)}

        self.packets_login = {
            p.get_id(self.context): p for p in
            serverbound.login.get_packets(self.context)}

        self.packets_playing = {
            p.get_id(self.context): p for p in
            serverbound.play.get_packets(self.context)}

        self.packets_status = {
            p.get_id(self.context): p for p in
            serverbound.status.get_packets(self.context)}

        self.listen_socket = socket.socket()
        self.listen_socket.settimeout(0.1)
        self.listen_socket.bind(('localhost', 0))
        self.listen_socket.listen(0)

        self.lock = threading.Lock()
        self.stopping = False

        super(FakeServer, self).__init__()

    def run(self):
        try:
            while True:
                try:
                    client_socket, addr = self.listen_socket.accept()
                    logging.debug('[ ++ ] Client %s connected.' % (addr,))
                    self.client_handler_type(self, client_socket).run()
                    logging.debug('[ -- ] Client %s disconnected.' % (addr,))
                except socket.timeout:
                    pass
                with self.lock:
                    if self.stopping:
                        logging.debug('[ ** ] Server stopped normally.')
                        break
        finally:
            self.listen_socket.close()

    def stop(self):
        with self.lock:
            self.stopping = True


class _FakeServerTest(unittest.TestCase):
    """
        A template for test cases involving a single client connecting to a
        single 'FakeServer'. The default behaviour causes the client to connect
        to the server, join the game, then disconnect, considering it a success
        if a 'JoinGamePacket' is received before a 'DisconnectPacket'.

        Customise by making subclasses that:
         1. Overrides the attributes present in this class, where desired, so
            that they will apply to all tests; and/or
         2. Define tests (or override 'runTest') to call '_test_connect' with
            the arguments specified as necessary to override class attributes.
         3. Overrides '_start_client' in order to set event listeners and
            change the connection mode, if necessary.
        To terminate the test and indicate that it finished successfully, a
        client packet handler or a handler method of the 'FakeClientHandler'
        must raise a 'FakeServerTestSuccess' exception.
    """

    server_version = VERSIONS[-1]
    # The Minecraft version name that the server will support.

    client_versions = None
    # The set of Minecraft version names or protocol version numbers that the
    # client will support. If None, the client supports all possible versions.

    client_handler_type = FakeClientHandler
    # A subclass of FakeClientHandler to be used in tests.

    compression_threshold = None
    # The compression threshold that the server will dictate.
    # If None, compression is disabled.

    def _start_client(self, client):
        game_joined = [False]

        def handle_join_game(packet):
            game_joined[0] = True
        client.register_packet_listener(
            handle_join_game, clientbound.play.JoinGamePacket)

        def handle_disconnect(packet):
            assert game_joined[0], 'JoinGamePacket not received.'
            raise FakeServerTestSuccess
        client.register_packet_listener(
            handle_disconnect, clientbound.play.DisconnectPacket)

        client.connect()

    def _test_connect(self, client_versions=None, server_version=None,
                      client_handler_type=None, compression_threshold=None):
        if client_versions is None:
            client_versions = self.client_versions
        if server_version is None:
            server_version = self.server_version
        if compression_threshold is None:
            compression_threshold = self.compression_threshold
        if client_handler_type is None:
            client_handler_type = self.client_handler_type

        server = FakeServer(minecraft_version=server_version,
                            compression_threshold=compression_threshold,
                            client_handler_type=client_handler_type)
        addr = "localhost"
        port = server.listen_socket.getsockname()[1]

        cond = threading.Condition()
        server_lock = threading.Lock()
        server_exc_info = [None]
        client_lock = threading.Lock()
        client_exc_info = [None]

        def handle_client_exception(exc, exc_info):
            with client_lock:
                client_exc_info[0] = exc_info
            with cond:
                cond.notify_all()

        client = connection.Connection(
            addr, port, username='TestUser', allowed_versions=client_versions,
            handle_exception=handle_client_exception)
        client.register_packet_listener(
            lambda packet: logging.debug('[ ->C] %s' % packet),
            packets.Packet, early=True)
        client.register_packet_listener(
            lambda packet: logging.debug('[C-> ] %s' % packet),
            packets.Packet, early=True, outgoing=True)

        server_thread = threading.Thread(
            name='FakeServer',
            target=self._test_connect_server,
            args=(server, cond, server_lock, server_exc_info))
        server_thread.daemon = True

        errors = []
        try:
            try:
                with cond:
                    server_thread.start()
                    self._start_client(client)
                    cond.wait(THREAD_TIMEOUT_S)
            finally:
                # Wait for all threads to exit.
                server.stop()
                for thread in server_thread, client.networking_thread:
                    if thread is not None and thread.is_alive():
                        thread.join(THREAD_TIMEOUT_S)
                    if thread is not None and thread.is_alive():
                        errors.append({
                            'msg': 'Thread "%s" timed out.' % thread.name})
        except Exception:
            errors.insert(0, {
                'msg': 'Exception in main thread',
                'exc_info': sys.exc_info()})
        else:
            timeout = True
            for lock, [exc_info], thread_name in (
                (client_lock, client_exc_info, 'client thread'),
                (server_lock, server_exc_info, 'server thread')
            ):
                with lock:
                    if exc_info is None:
                        continue
                    if not issubclass(exc_info[0], FakeServerTestSuccess):
                        errors.insert(0, {
                            'msg': 'Exception in %s:' % thread_name,
                            'exc_info': exc_info})
                    timeout = False
            if timeout:
                errors.insert(0, {'msg': 'Test timed out.'})

        if len(errors) > 1:
            for error in errors:
                logging.error(**error)
            self.fail('Multiple errors: see logging output.')
        elif errors and 'exc_info' in errors[0]:
            raise_(*errors[0]['exc_info'])
        elif errors:
            self.fail(errors[0]['msg'])

    def _test_connect_server(self, server, cond, server_lock, server_exc_info):
        exc_info = None
        try:
            server.run()
        except Exception:
            exc_info = sys.exc_info()
        with server_lock:
            server_exc_info[0] = exc_info
        with cond:
            cond.notify_all()
