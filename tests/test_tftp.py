import os
import socket
import threading

import pytest

from nomad import tftp
from nomad.tftp import ACK, DATA, ERROR, OACK, RRQ, WRQ, TftpError, TftpServer, ack_packet, data_packet, \
    error_packet, negotiate, oack_packet, parse_packet, request_packet, safe_path


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "served"
    root.mkdir()
    events = []
    instance = TftpServer(str(root), "127.0.0.1", 0, on_event=events.append)
    instance.start()
    instance.port = instance.sock.getsockname()[1]
    instance.events = events
    yield instance
    instance.stop()


def test_packets_round_trip():
    request = request_packet(RRQ, "firmware.bin", options={"blksize": 1468, "tsize": 0})
    assert parse_packet(request) == (RRQ, ("firmware.bin", "octet", {"blksize": "1468", "tsize": "0"}))
    assert parse_packet(request_packet(WRQ, "cfg.txt", "NETASCII")) == (WRQ, ("cfg.txt", "netascii", {}))
    assert parse_packet(data_packet(65535, b"abc")) == (DATA, (65535, b"abc"))
    assert parse_packet(ack_packet(7)) == (ACK, (7,))
    assert parse_packet(error_packet(1)) == (ERROR, (1, "File not found"))
    assert parse_packet(oack_packet({"blksize": 1024})) == (OACK, ({"blksize": "1024"},))
    for bad in (b"", b"\x00", b"\x00\x09\x00\x00", b"\x00\x01onlyname"):
        with pytest.raises(TftpError):
            parse_packet(bad)


def test_negotiate():
    accepted, size, timeout = negotiate({"blksize": "99999", "tsize": "0", "timeout": "5", "windowsize": "8"}, 1234)
    assert accepted == {"blksize": 65464, "tsize": 1234, "timeout": 5} and (size, timeout) == (65464, 5)
    assert negotiate({"blksize": "junk", "timeout": "999"}) == ({}, 512, 3)
    assert negotiate({"tsize": "777"})[0] == {"tsize": "777"}  # An upload's size is echoed back


def test_safe_path(tmp_path):
    root = str(tmp_path)
    assert safe_path(root, "/configs/sw1.cfg") == os.path.join(os.path.realpath(root), "configs", "sw1.cfg")
    assert safe_path(root, "a\\b.bin") == os.path.join(os.path.realpath(root), "a", "b.bin")
    for bad in ("../x", "a/../../x", "C:/Windows/win.ini", "", "/"):
        with pytest.raises(TftpError):
            safe_path(root, bad)


@pytest.mark.parametrize("size", [0, 511, 512, 1468, 5000, 200000])
@pytest.mark.parametrize("block_size", [512, 1468])
def test_upload_then_download(server, tmp_path, size, block_size):
    data = os.urandom(size)
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    progress = []
    assert tftp.upload("127.0.0.1", str(source), "dir/copy.bin", server.port, block_size,
                       progress=lambda done, total: progress.append((done, total))) == size
    assert (tmp_path / "served" / "dir" / "copy.bin").read_bytes() == data  # Saved before the last ACK
    assert progress[-1] == (size, size)
    target = tmp_path / "back.bin"
    assert tftp.download("127.0.0.1", "dir/copy.bin", str(target), server.port, block_size) == size
    assert target.read_bytes() == data
    assert not [name for name in os.listdir(tmp_path / "served" / "dir") if name.startswith(".nomad")]


def test_refusals(server, tmp_path):
    (tmp_path / "served" / "exists.bin").write_bytes(b"old")
    source = tmp_path / "new.bin"
    source.write_bytes(b"new")
    with pytest.raises(TftpError, match="already exists"):
        tftp.upload("127.0.0.1", str(source), "exists.bin", server.port)
    assert (tmp_path / "served" / "exists.bin").read_bytes() == b"old"
    server.allow_overwrite = True
    tftp.upload("127.0.0.1", str(source), "exists.bin", server.port)
    assert (tmp_path / "served" / "exists.bin").read_bytes() == b"new"
    server.allow_upload = False
    with pytest.raises(TftpError, match="Uploads are turned off"):
        tftp.upload("127.0.0.1", str(source), "other.bin", server.port)
    with pytest.raises(TftpError, match="File not found"):
        tftp.download("127.0.0.1", "missing.bin", str(tmp_path / "x"), server.port)
    with pytest.raises(TftpError, match="Access violation"):
        tftp.download("127.0.0.1", "../secret.txt", str(tmp_path / "x"), server.port)
    assert not (tmp_path / "x").exists()
    finished = [event for event in server.events if event.finished]
    assert finished and not finished[-1].ok


def plain_server(root, stop):
    """A minimal RFC 1350 server that ignores options (sends DATA 1 straight away), like old devices."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                packet, peer = sock.recvfrom(1024)
            except socket.timeout:
                continue
            _, (name, _, _) = parse_packet(packet)
            data = (root / name).read_bytes()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as transfer:
                transfer.settimeout(2)
                for block in range(1, len(data) // 512 + 2):
                    transfer.sendto(data_packet(block, data[(block - 1) * 512:block * 512]), peer)
                    transfer.recvfrom(1024)
        sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def test_download_from_a_server_without_options(tmp_path):
    data = os.urandom(1300)
    (tmp_path / "old.bin").write_bytes(data)
    stop = threading.Event()
    port = plain_server(tmp_path, stop)
    try:
        assert tftp.download("127.0.0.1", "old.bin", str(tmp_path / "got.bin"), port) == 1300
    finally:
        stop.set()
    assert (tmp_path / "got.bin").read_bytes() == data


def test_no_server(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(TftpError, match="isn't running a TFTP server|No answer"):
        tftp.download("127.0.0.1", "x", str(tmp_path / "x"), port, timeout=1)
