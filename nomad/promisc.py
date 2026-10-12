"""Promiscuous mode for a capture: an adapter on a SPAN destination port receives copies of other machines' traffic,
addressed to their MAC addresses, which the adapter normally throws away before pktmon sees them. Windows switches an
adapter to promiscuous mode for a raw socket set to receive everything (SIO_RCVALL), so holding one open on the
adapter's address while pktmon captures lets the copies through. Built into Windows: no driver to install. Needs
administrator rights, and the adapter needs an IPv4 address (on a SPAN port it usually has a 169.254 one).
"""
import logging
import socket

log = logging.getLogger(__name__)

# Fallback constants for POSIX where socket module lacks Windows SIO_RCVALL definitions
SIO_RCVALL = getattr(socket, "SIO_RCVALL", 0x98000001)
RCVALL_ON = getattr(socket, "RCVALL_ON", 1)
RCVALL_OFF = getattr(socket, "RCVALL_OFF", 0)
if not hasattr(socket, "SIO_RCVALL"):
    socket.SIO_RCVALL = SIO_RCVALL
    socket.RCVALL_ON = RCVALL_ON
    socket.RCVALL_OFF = RCVALL_OFF

RECEIVE_BUFFER = 4096  # The socket is never read: keep what piles up in it small


class PromiscuousError(OSError):
    pass


class Promiscuous:
    """Holds an adapter in promiscuous mode from start() to stop()."""

    def __init__(self, address):
        self.address = address
        self.sock = None

    @property
    def on(self):
        return self.sock is not None

    def start(self):
        """Raises PromiscuousError (e.g. without administrator rights, or an address this PC doesn't have)."""
        if self.sock is not None:
            return
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER)
            sock.bind((self.address, 0))
            if hasattr(sock, "ioctl"):
                sock.ioctl(SIO_RCVALL, RCVALL_ON)
            else:
                log.info("Socket ioctl is not available on this platform (%s)", self.address)
        except (OSError, AttributeError, ValueError) as error:
            if sock is not None:
                sock.close()
            raise PromiscuousError(f"Couldn't switch the adapter with {self.address} to promiscuous mode: "
                                   f"{error}") from None
        self.sock = sock
        log.info("Promiscuous mode on for %s", self.address)

    def stop(self):
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            if hasattr(sock, "ioctl"):
                sock.ioctl(SIO_RCVALL, RCVALL_OFF)
        except OSError as error:
            log.warning("Couldn't switch promiscuous mode off for %s: %s", self.address, error)
        finally:
            sock.close()
        log.info("Promiscuous mode off for %s", self.address)
