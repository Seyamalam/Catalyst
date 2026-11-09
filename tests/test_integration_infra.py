"""Socket-level checks for the optional Docker database integration harness.

These tests validate only that the pinned services accept TCP connections and speak
an initial plaintext wire-protocol exchange. They do not exercise Catalyst drivers,
SQL execution, TLS, authentication success, or SQLAlchemy dialect parity.
"""

from __future__ import annotations

import socket
import struct

import pytest

pytestmark = pytest.mark.integration


POSTGRES_HOST = "127.0.0.1"
POSTGRES_PORT = 5432
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 3306
SOCKET_TIMEOUT_SECONDS = 2.0


def _connect(host: str, port: int) -> socket.socket:
    connection = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT_SECONDS)
    connection.settimeout(SOCKET_TIMEOUT_SECONDS)
    return connection


def _read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise AssertionError("database closed the connection before completing its packet")
        data.extend(chunk)
    return bytes(data)


def _postgres_startup_packet() -> bytes:
    parameters = b"user\0catalyst\0database\0catalyst\0client_encoding\0UTF8\0\0"
    length = 4 + 4 + len(parameters)
    return struct.pack("!II", length, 196608) + parameters


def test_postgres_plaintext_startup_response() -> None:
    """Validate PostgreSQL protocol framing, not driver behavior or parity.

    PostgreSQL does not send a greeting on connect: the client asks whether TLS is
    available with an SSLRequest, expects `N` from the TLS-disabled service, then
    sends a protocol startup packet and reads an authentication or error response.
    This validates wire framing only; it is not driver behavior or parity.
    """
    try:
        connection = _connect(POSTGRES_HOST, POSTGRES_PORT)
    except OSError as error:
        pytest.skip(f"PostgreSQL integration service is not reachable: {error}")

    with connection:
        # PostgreSQL SSLRequest: a TLS-disabled server answers with one-byte N.
        connection.sendall(struct.pack("!II", 8, 80877103))
        response = _read_exact(connection, 1)
        if response == b"S":
            pytest.skip(
                "A TLS-enabled PostgreSQL answered SSLRequest; the harness only "
                "validates the TLS-disabled compose service"
            )
        assert response == b"N"
        connection.sendall(_postgres_startup_packet())
        message_type = _read_exact(connection, 1)
        message_length = struct.unpack("!I", _read_exact(connection, 4))[0]
        assert message_type in (b"R", b"E")
        assert message_length >= 4
        _read_exact(connection, message_length - 4)


def test_mysql_plaintext_protocol_greeting() -> None:
    """Validate MySQL's initial plaintext greeting, not driver behavior or parity.

    No TLS upgrade or authentication response is sent. This proves only that the
    server emits a minimally well-formed protocol-v10 greeting over plaintext TCP.
    """
    try:
        connection = _connect(MYSQL_HOST, MYSQL_PORT)
    except OSError as error:
        pytest.skip(f"MySQL integration service is not reachable: {error}")

    with connection:
        packet_header = _read_exact(connection, 4)
        payload_length = int.from_bytes(packet_header[:3], byteorder="little")
        sequence_id = packet_header[3]
        greeting = _read_exact(connection, payload_length)
        assert sequence_id == 0
        assert payload_length >= 1
        assert greeting[0] == 10
        assert b"\0" in greeting[1:]
