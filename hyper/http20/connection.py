# -*- coding: utf-8 -*-
"""
hyper/http20/connection
~~~~~~~~~~~~~~~~~~~~~~~

Objects that build hyper's connection-level HTTP/2.0 abstraction.
"""
from .hpack import Encoder, Decoder
from .stream import Stream
from .tls import wrap_socket
from .frame import DataFrame, HeadersFrame, SettingsFrame, Frame

import socket


class HTTP20Connection(object):
    """
    An object representing a single HTTP/2.0 connection to a server.

    This object behaves similarly to the Python standard library's
    HTTPConnection object, with a few critical differences.
    """
    def __init__(self, host, port=None, **kwargs):
        """
        Creates an HTTP/2.0 connection to a specific server.

        Most of the standard library's arguments to the constructor are
        irrelevant for HTTP/2.0 or not supported by hyper.
        """
        if port is None:
            try:
                self.host, self.port = host.split(':')
                self.port = int(self.port)
            except ValueError:
                self.host, self.port = host, 443
        else:
            self.host, self.port = host, port

        # Streams are stored in a dictionary keyed off their stream IDs. We
        # also save the most recent one for easy access without having to walk
        # the dictionary.
        self.streams = {}
        self.recent_stream = None
        self.next_stream_id = 1

        # Header encoding/decoding is at the connection scope, so we embed a
        # header encoder and a decoder. These get passed to child stream
        # objects.
        self.encoder = Encoder()
        self.decoder = Decoder()

        # The socket used to send data.
        self._sock = None

        # The inbound and outbound flow control windows.
        self._out_flow_control_window = 65535
        self._in_flow_control_window = 65535

        return

    def request(self, method, url, body=None, headers={}):
        """
        This will send a request to the server using the HTTP request method
        ``method`` and the selector ``url``. If the ``body`` argument is
        present, it should be string or bytes object of data to send after the
        headers are finished. Strings are encoded as UTF-8. To use other
        encodings, pass a bytes object. The Content-Length header is set to the
        length of the body field.

        Returns a stream ID for the request.
        """
        pass

    def getresponse(self, stream_id=None):
        """
        Should be called after a request is sent to get a response from the
        server. If sending multiple parallel requests, pass the stream ID of
        the request whose response you want. Returns a HTTPResponse instance.
        If you pass no stream_id, you will receive the oldest HTTPResponse
        still outstanding.
        """
        pass

    def connect(self):
        """
        Connect to the server specified when the object was created. This is a
        no-op if we're already connected.
        """
        if self._sock is None:
            sock = socket.create_connection((self.host, self.port), 5)
            sock = wrap_socket(sock, self.host)
            self._sock = sock

            # We need to send a Settings frame immediately on this connection.
            f = SettingsFrame(0)
            f.settings[SettingsFrame.ENABLE_PUSH] = 0
            self._send_cb(f)

        return

    def close(self):
        """
        Close the connection to the server.
        """
        pass

    def putrequest(self, method, selector, **kwargs):
        """
        This should be the first call for sending a given HTTP request to a
        server. It returns a stream ID for the given connection that should be
        passed to all subsequent request building calls.
        """
        # Create a new stream.
        s = self._new_stream()

        # To this stream we need to immediately add a few headers that are
        # HTTP/2.0 specific. These are: ":method", ":scheme", ":authority" and
        # ":path". We can set all of these now.
        s.add_header(":method", method)
        s.add_header(":scheme", "https")  # We only support HTTPS at this time.
        s.add_header(":authority", self.host)
        s.add_header(":path", selector)

        # Save the stream.
        self.streams[s.stream_id] = s
        self.recent_stream = s

        return s.stream_id

    def putheader(self, header, argument, stream_id=None):
        """
        Sends an HTTP header to the server, with name ``header`` and value
        ``argument``.

        Unlike the httplib version of this function, this version does not
        actually send anything when called. Instead, it queues the headers up
        to be sent when you call ``endheaders``.
        """
        stream = (self.streams[stream_id] if stream_id is not None
                  else self.recent_stream)

        stream.add_header(header, argument)

        return

    def endheaders(self, message_body=None, final=False, stream_id=None):
        """
        Sends the prepared headers to the server. If the ``message_body``
        argument is provided it will also be sent to the server as the body of
        the request, and the stream will immediately be closed. If the
        ``final`` argument is set to True, the stream will also immediately
        be closed: otherwise, the stream will be left open and subsequent calls
        to ``send()`` will be required.
        """
        stream = (self.streams[stream_id] if stream_id is not None
                  else self.recent_stream)

        # Close this if we've been told no more data is coming and we don't
        # have any to send.
        stream.open(final and message_body is None)

        # Send whatever data we have.
        if message_body is not None:
            stream.send_data(message_body, final)

        return

    def send(self, data, final=False, stream_id=None):
        """
        Sends some data to the server. This data will be sent immediately
        (excluding the normal HTTP/2.0 flow control rules). If this is the last
        data that will be sent as part of this request, the ``final`` argument
        should be set to ``True``. This will cause the stream to be closed.
        """
        stream = (self.streams[stream_id] if stream_id is not None
                  else self.recent_stream)

        stream.send_data(data, final)

        return

    def _new_stream(self):
        """
        Returns a new stream object for this connection.
        """
        s = Stream(
            self.next_stream_id, self._send_cb, self._recv_cb, self.encoder,
            self.decoder
        )
        self.next_stream_id += 2

        return s

    def _send_cb(self, frame):
        """
        This is the callback used by streams to send data on the connection.

        It expects to receive a single frame, and then to serialize that frame
        and send it on the connection. It does so obeying the connection-level
        flow-control principles of HTTP/2.0.
        """
        # Maintain our outgoing flow-control window.
        if (isinstance(frame, DataFrame) and
            not isinstance(frame, HeadersFrame)):
            if self._out_flow_control_window < len(frame.data):
                raise RuntimeError("Flow control not yet implemented.")

            self._out_flow_control_window -= len(frame.data)

        data = frame.serialize()

        self._sock.send(data)

    def _recv_cb(self):
        """
        This is the callback used by streams to read data from the connection.

        It expects to read a single frame, and then to deserialize that frame
        and pass it to the relevant stream. This is generally called by a
        stream, not by the connection itself, and it's likely that streams will
        read a frame that doesn't belong to them. That's ok: streams need to
        make a decision to spin around again.
        """
        # Begin by reading 8 bytes from the socket.
        header = self._sock.recv(8)

        # Parse the header.
        frame, length = Frame.parse_frame_header(header)

        # Read the remaining data from the socket.
        data = self._sock.recv(length)
        frame.parse_body(data)

        # Work out to whom this frame should go.
        if frame.stream_id != 0:
            self.streams[frame.stream_id].receive_frame(frame)
        else:
            self.receive_frame(frame)
