"""Network reset tools: Winsock and TCP/IP resets, DHCP renewal, and viewing or turning off proxy settings."""
import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

from .system import CommandError, run_command

log = logging.getLogger(__name__)

# InternetQueryOption / InternetSetOption
INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_PER_CONNECTION_OPTION = 75
PER_CONN_FLAGS, PER_CONN_PROXY_SERVER, PER_CONN_PROXY_BYPASS, PER_CONN_AUTOCONFIG_URL = 1, 2, 3, 4
PROXY_TYPE_DIRECT, PROXY_TYPE_PROXY, PROXY_TYPE_AUTO_PROXY_URL, PROXY_TYPE_AUTO_DETECT = 1, 2, 4, 8
WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_ACCESS_TYPE_NAMED_PROXY = 1, 3
RESTART_NOTE = "Restart the computer to finish."


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _OptionValue(ctypes.Union):
    # pszValue is a raw pointer, not LPWSTR: ctypes would hand back a copy, and Windows' own string must be freed
    _fields_ = [("dwValue", wintypes.DWORD), ("pszValue", ctypes.c_void_p), ("ftValue", FILETIME)]


class INTERNET_PER_CONN_OPTIONW(ctypes.Structure):
    _fields_ = [("dwOption", wintypes.DWORD), ("Value", _OptionValue)]


class INTERNET_PER_CONN_OPTION_LISTW(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("pszConnection", wintypes.LPWSTR), ("dwOptionCount", wintypes.DWORD),
                ("dwOptionError", wintypes.DWORD), ("pOptions", ctypes.POINTER(INTERNET_PER_CONN_OPTIONW))]


class WINHTTP_PROXY_INFO(ctypes.Structure):
    _fields_ = [("dwAccessType", wintypes.DWORD), ("lpszProxy", ctypes.c_void_p), ("lpszProxyBypass", ctypes.c_void_p)]


@dataclass
class ProxySettings:
    """This user's proxy settings (Windows Settings > Network > Proxy), used by browsers and most programs."""
    flags: int = PROXY_TYPE_DIRECT
    server: str = ""
    bypass: str = ""
    script_url: str = ""

    @property
    def uses_proxy(self):
        return bool(self.flags & PROXY_TYPE_PROXY and self.server)

    @property
    def uses_script(self):
        return bool(self.flags & PROXY_TYPE_AUTO_PROXY_URL and self.script_url)

    @property
    def auto_detect(self):
        return bool(self.flags & PROXY_TYPE_AUTO_DETECT)

    @property
    def active(self):
        """A proxy or setup script is in use (automatic detection alone usually finds nothing)."""
        return self.uses_proxy or self.uses_script

    def describe(self):
        lines = []
        if self.uses_proxy:
            lines.append(f"Proxy server: {self.server}" + (f" (not for: {self.bypass})" if self.bypass else ""))
        if self.uses_script:
            lines.append(f"Setup script: {self.script_url}")
        lines.append(f"Automatically detect settings: {'on' if self.auto_detect else 'off'}")
        if not self.active:
            lines.insert(0, "No proxy: programs connect directly.")
        return lines


def _wininet():
    if not hasattr(ctypes, "WinDLL"):
        return None
    dll = ctypes.WinDLL("wininet", use_last_error=True)
    dll.InternetQueryOptionW.argtypes = [wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                         ctypes.POINTER(wintypes.DWORD)]
    dll.InternetQueryOptionW.restype = wintypes.BOOL
    dll.InternetSetOptionW.argtypes = [wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    dll.InternetSetOptionW.restype = wintypes.BOOL
    return dll


def _option_list(options):
    """Build an option list. Returns (list, array, buffers); keep all three alive until Windows is done with them."""
    array = (INTERNET_PER_CONN_OPTIONW * len(options))()
    buffers = []
    for item, (option, value) in zip(array, options):
        item.dwOption = option
        if isinstance(value, int):
            item.Value.dwValue = value
        elif value:
            buffer = ctypes.create_unicode_buffer(value)
            buffers.append(buffer)
            item.Value.pszValue = ctypes.addressof(buffer)
        else:
            item.Value.pszValue = None
    option_list = INTERNET_PER_CONN_OPTION_LISTW(dwSize=ctypes.sizeof(INTERNET_PER_CONN_OPTION_LISTW),
                                                 pszConnection=None, dwOptionCount=len(options), dwOptionError=0,
                                                 pOptions=array)
    return option_list, array, buffers


def _take_string(address, kernel32):
    """Read a string Windows allocated for us, then free it."""
    if not address:
        return ""
    try:
        return ctypes.wstring_at(address)
    finally:
        kernel32.GlobalFree(address)


def _kernel32():
    if not hasattr(ctypes, "WinDLL"):
        return None
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    return kernel32


def read_proxy():
    """The current user's proxy settings. Raises OSError if Windows won't say."""
    dll = _wininet()
    if dll is None:
        return ProxySettings()
    option_list, array, _buffers = _option_list([(PER_CONN_FLAGS, 0), (PER_CONN_PROXY_SERVER, None),
                                                 (PER_CONN_PROXY_BYPASS, None), (PER_CONN_AUTOCONFIG_URL, None)])
    size = wintypes.DWORD(ctypes.sizeof(option_list))
    if not dll.InternetQueryOptionW(None, INTERNET_OPTION_PER_CONNECTION_OPTION, ctypes.byref(option_list),
                                    ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32 = _kernel32()
    strings = [_take_string(item.Value.pszValue, kernel32) for item in array[1:]]
    return ProxySettings(array[0].Value.dwValue, *strings)


def write_proxy(settings):
    """Apply proxy settings for the current user and tell running programs. Doesn't need administrator rights."""
    dll = _wininet()
    option_list, _array, _buffers = _option_list([(PER_CONN_FLAGS, settings.flags),
                                        (PER_CONN_PROXY_SERVER, settings.server or None),
                                        (PER_CONN_PROXY_BYPASS, settings.bypass or None),
                                        (PER_CONN_AUTOCONFIG_URL, settings.script_url or None)])
    if not dll.InternetSetOptionW(None, INTERNET_OPTION_PER_CONNECTION_OPTION, ctypes.byref(option_list),
                                  ctypes.sizeof(option_list)):
        raise ctypes.WinError(ctypes.get_last_error())
    dll.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
    dll.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
    log.info("Proxy settings set to: %s", "; ".join(settings.describe()))


def without_proxy(settings):
    """The same settings with the proxy server and setup script switched off (their details are kept)."""
    return ProxySettings(PROXY_TYPE_DIRECT | (settings.flags & PROXY_TYPE_AUTO_DETECT), settings.server,
                         settings.bypass, settings.script_url)


@dataclass
class WinHttpProxy:
    """The machine-wide WinHTTP proxy, used by Windows services such as Windows Update."""
    proxy: str = ""
    bypass: str = ""

    def describe(self):
        if not self.proxy:
            return ["No proxy: Windows services connect directly."]
        return [f"Proxy server: {self.proxy}" + (f" (not for: {self.bypass})" if self.bypass else "")]


def read_winhttp_proxy():
    if not hasattr(ctypes, "WinDLL"):
        return WinHttpProxy()
    dll = ctypes.WinDLL("winhttp", use_last_error=True)
    dll.WinHttpGetDefaultProxyConfiguration.argtypes = [ctypes.POINTER(WINHTTP_PROXY_INFO)]
    dll.WinHttpGetDefaultProxyConfiguration.restype = wintypes.BOOL
    info = WINHTTP_PROXY_INFO()
    if not dll.WinHttpGetDefaultProxyConfiguration(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32 = _kernel32()
    proxy, bypass = _take_string(info.lpszProxy, kernel32), _take_string(info.lpszProxyBypass, kernel32)
    if info.dwAccessType != WINHTTP_ACCESS_TYPE_NAMED_PROXY:
        return WinHttpProxy()
    return WinHttpProxy(proxy, bypass)


# ----------------------------------------------------------------- Resets (administrator rights needed)

@dataclass
class ResetAction:
    key: str
    title: str
    description: str
    commands: list  # Each a list of arguments
    needs_restart: bool = False
    needs_admin: bool = True
    warning: str = ""  # Asked for confirmation first when set
    timeout: int = 120


RESET_ACTIONS = [
    ResetAction("flush_dns", "Flush DNS Cache",
                "Forget remembered DNS answers, so names are looked up again. Try this first when one site won't "
                "load or a server's address changed.", [["ipconfig", "/flushdns"]], needs_admin=False),
    ResetAction("renew", "Renew All DHCP Leases",
                "Ask the DHCP server for a fresh lease on every adapter that uses DHCP. Adapters without a DHCP "
                "server take up to a minute to give up.", [["ipconfig", "/renew"]], timeout=240),
    ResetAction("winsock", "Reset Winsock",
                "Rebuild the Windows Sockets catalog. Fixes \"connected but nothing works\" after VPN clients, "
                "security software or malware left it damaged.", [["netsh", "winsock", "reset"]],
                needs_restart=True,
                warning="This resets Winsock. Some VPN and security software may need reinstalling afterwards, "
                        "and the computer must be restarted to finish."),
    ResetAction("tcpip", "Reset TCP/IP",
                "Put the TCP/IP (IPv4 and IPv6) settings back to their defaults. Fixes a damaged network stack.",
                [["netsh", "int", "ip", "reset"], ["netsh", "int", "ipv6", "reset"]], needs_restart=True,
                warning="This resets TCP/IP to its defaults. Adapters with static IP addresses may go back to "
                        "DHCP, so note their settings (or save a profile on the Interfaces tab) first. The "
                        "computer must be restarted to finish."),
    ResetAction("winhttp", "Reset WinHTTP Proxy",
                "Remove the machine-wide proxy that Windows services (such as Windows Update) use.",
                [["netsh", "winhttp", "reset", "proxy"]]),
]


def run_reset(action):
    """Run a reset's commands. Returns their combined output. Raises CommandError if one fails outright."""
    outputs = []
    for command in action.commands:
        try:
            outputs.append(run_command(command, timeout=action.timeout))
        except CommandError as error:
            # "netsh int ip reset" can't reset a few protected keys and says so, but still resets the rest
            if action.key == "tcpip" and "reset" in error.output.lower() and "ok" in error.output.lower():
                outputs.append(error.output)
                continue
            raise
    return "\n".join(output for output in outputs if output)


def restart_computer(delay_seconds=10):
    run_command(["shutdown", "/r", "/t", str(int(delay_seconds)), "/c", "Restarting to finish the network reset."])


def cancel_restart():
    run_command(["shutdown", "/a"])
