import socket
import ssl
import threading
from urllib.parse import urlsplit

from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    RejectConnection,
    CloseConnection,
    Message,
    Request,
    Ping,
    TextMessage,
    BytesMessage,
)
from wsproto.frame_protocol import CloseReason
from wsproto.utilities import LocalProtocolError


class ConnectionError(RuntimeError):
    """Connection error exception class."""
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f'Connection error {status_code}')


class ConnectionClosed(RuntimeError):
    """Connection closed exception class."""
    pass


class Base:
    def __init__(self, sock=None, connection_type=None, receive_bytes=4096,
                 thread_class=threading.Thread, event_class=threading.Event):
        self.sock = sock
        self.receive_bytes = receive_bytes
        self.input_buffer = []
        self.event = event_class()
        self.connected = False
        self.is_server = (connection_type == ConnectionType.SERVER)

        self.ws = WSConnection(connection_type)
        self.handshake()

        self.thread = thread_class(target=self._thread)
        self.thread.start()
        self.event.wait()
        self.event.clear()

    def handshake(self):
        # to be implemented by subclasses
        pass

    def send(self, data):
        """Send data over the WebSocket connection.

        :param data: The data to send. If ``data`` is of type ``bytes``, then
                     a binary message is sent. Else, the message is sent in
                     text format.
        """
        if not self.connected:
            raise ConnectionClosed()
        if isinstance(data, bytes):
            out_data = self.ws.send(Message(data=data))
        else:
            out_data = self.ws.send(TextMessage(data=str(data)))
        self.sock.send(out_data)

    def receive(self, timeout=None):
        """Receive data over the WebSocket connection.

        :param timeout: Amount of time to wait for the data, in seconds. Set
                        to ``None`` (the default) to wait undefinitely. Set
                        to 0 to read without blocking.

        The data received is returned, as ``bytes`` or ``str``, depending on
        the type of the incoming message.
        """
        while self.connected and not self.input_buffer:
            if not self.event.wait(timeout=timeout):
                return None
            self.event.clear()
        if not self.connected:
            raise ConnectionClosed()
        return self.input_buffer.pop(0)

    def close(self, reason=None, message=None):
        """Close the WebSocket connection.

        :param reason: A numeric status code indicating the reason of the
                       closure, as defined by the WebSocket specifiation. The
                       default is 1000 (normal closure).
        :param message: A text message to be sent to the other side.
        """
        out_data = self.ws.send(CloseConnection(
            reason or CloseReason.NORMAL_CLOSURE, message))
        try:
            self.sock.send(out_data)
        except BrokenPipeError:
            pass

    def _thread(self):
        self.connected = self._handle_events()
        self.event.set()
        while self.connected:
            try:
                in_data = self.sock.recv(self.receive_bytes)
            except (OSError, ConnectionResetError):
                self.connected = False
                self.event.set()
                break
            self.ws.receive_data(in_data)
            self.connected = self._handle_events()

    def _handle_events(self):
        keep_going = True
        out_data = b''
        for event in self.ws.events():
            try:
                if isinstance(event, Request):
                    out_data += self.ws.send(AcceptConnection())
                elif isinstance(event, CloseConnection):
                    if self.is_server:
                        out_data += self.ws.send(event.response())
                    self.event.set()
                    keep_going = False
                elif isinstance(event, Ping):
                    out_data += self.ws.send(event.response())
                elif isinstance(event, TextMessage):
                    self.input_buffer.append(event.data)
                    self.event.set()
                elif isinstance(event, BytesMessage):
                    self.input_buffer.append(event.data)
                    self.event.set()
            except LocalProtocolError:
                out_data = b''
                self.event.set()
                keep_going = False
        if out_data:
            self.sock.send(out_data)
        return keep_going


class Server(Base):
    """This class implements a WebSocket server.

    :param environ: A WSGI ``environ`` dictionary with the request details.
                    Among other things, this class expects to find the
                    low-level network socket for the connection somewhere in
                    this dictionary. Since the WSGI specification does not
                    cover where or how to store this socket, each web server
                    does this in its own different way. Werkzeug, Gunicorn,
                    Eventlet and Gevent are the only web servers that are
                    currently supported.
    :param receive_bytes: The size of the receive buffer, in bytes. The
                          default is 4096.
    :param thread_class: The ``Thread`` class to use when creating background
                         threads. The default is the ``threading.Thread``
                         class from the Python standard library.
    :param event_class: The ``Event`` class to use when creating event
                        objects. The default is the `threading.Event`` class
                        from the Python standard library.
    """
    def __init__(self, environ, receive_bytes=4096,
                 thread_class=threading.Thread, event_class=threading.Event):
        self.environ = environ
        if 'werkzeug.socket' in environ:
            # extract socket from Werkzeug's WSGI environment
            sock = environ.get('werkzeug.socket')
        elif 'gunicorn.socket' in environ:
            # extract socket from Gunicorn WSGI environment
            sock = environ.get('gunicorn.socket')
        elif 'eventlet.input' in environ:
            # extract socket from Eventlet's WSGI environment
            sock = environ.get('eventlet.input').get_socket()
        elif environ.get('SERVER_SOFTWARE', '').startswith('gevent'):
            # extract socket from Gevent's WSGI environment
            sock = environ['wsgi.input'].raw._sock
        else:
            raise RuntimeError('Cannot obtain socket from WSGI environment.')
        super().__init__(sock, connection_type=ConnectionType.SERVER,
                         receive_bytes=receive_bytes,
                         thread_class=thread_class, event_class=event_class)

    def handshake(self):
        in_data = b'GET / HTTP/1.1\r\n'
        for key, value in self.environ.items():
            if key.startswith('HTTP_'):
                header = '-'.join([p.capitalize() for p in key[5:].split('_')])
                in_data += f'{header}: {value}\r\n'.encode()
        in_data += b'\r\n'
        self.ws.receive_data(in_data)


class Client(Base):
    """This class implements a WebSocket client.

    :param url: The connection URL. Both ``ws://`` and ``wss://`` URLs are
                accepted.
    :param receive_bytes: The size of the receive buffer, in bytes. The
                          default is 4096.
    :param thread_class: The ``Thread`` class to use when creating background
                         threads. The default is the ``threading.Thread``
                         class from the Python standard library.
    :param event_class: The ``Event`` class to use when creating event
                        objects. The default is the `threading.Event`` class
                        from the Python standard library.
    :param ssl_context: An ``SSLContext`` instance, if a default SSL context
                        isn't sufficient.
    """
    def __init__(self, url, receive_bytes=4096, thread_class=threading.Thread,
                 event_class=threading.Event, ssl_context=None):
        parsed_url = urlsplit(url)
        is_secure = parsed_url.scheme in ['https', 'wss']
        self.host = parsed_url.hostname
        self.port = parsed_url.port or (443 if is_secure else 80)
        self.path = parsed_url.path
        if parsed_url.query:
            self.path += '?' + parsed_url.query

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if is_secure:
            if ssl_context is None:
                ssl_context = ssl.create_default_context(
                    purpose=ssl.Purpose.SERVER_AUTH)
            sock = ssl_context.wrap_socket(sock, server_hostname=self.host)
        sock.connect((self.host, self.port))
        super().__init__(sock, connection_type=ConnectionType.CLIENT,
                         receive_bytes=receive_bytes,
                         thread_class=thread_class, event_class=event_class)

    def handshake(self):
        out_data = self.ws.send(Request(host=self.host, target=self.path))
        self.sock.send(out_data)

        in_data = self.sock.recv(self.receive_bytes)
        self.ws.receive_data(in_data)
        for event in self.ws.events():
            if isinstance(event, AcceptConnection):
                break
            elif isinstance(event, RejectConnection):
                raise ConnectionError(event.status_code)

    def close(self, reason=None, message=None):
        super().close(reason=reason, message=message)
        self.sock.close()
