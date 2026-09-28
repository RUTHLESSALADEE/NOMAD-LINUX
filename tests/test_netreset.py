import pytest

from nomad import netreset
from nomad.netreset import PROXY_TYPE_AUTO_DETECT, PROXY_TYPE_AUTO_PROXY_URL, PROXY_TYPE_DIRECT, PROXY_TYPE_PROXY, \
    RESET_ACTIONS, ProxySettings, WinHttpProxy, run_reset, without_proxy
from nomad.system import CommandError


def test_proxy_descriptions():
    direct = ProxySettings(PROXY_TYPE_DIRECT | PROXY_TYPE_AUTO_DETECT)
    assert not direct.active and direct.describe()[0].startswith("No proxy")
    manual = ProxySettings(PROXY_TYPE_DIRECT | PROXY_TYPE_PROXY, "10.0.0.1:8080", "<local>")
    assert manual.active and manual.describe()[0] == "Proxy server: 10.0.0.1:8080 (not for: <local>)"
    script = ProxySettings(PROXY_TYPE_DIRECT | PROXY_TYPE_AUTO_PROXY_URL, script_url="http://wpad/wpad.dat")
    assert script.active and "Setup script: http://wpad/wpad.dat" in script.describe()
    assert ProxySettings(PROXY_TYPE_DIRECT | PROXY_TYPE_PROXY).active is False  # Ticked but no server
    assert WinHttpProxy().describe()[0].startswith("No proxy")


def test_without_proxy_keeps_details_and_auto_detect():
    settings = ProxySettings(PROXY_TYPE_DIRECT | PROXY_TYPE_PROXY | PROXY_TYPE_AUTO_PROXY_URL | PROXY_TYPE_AUTO_DETECT,
                             "10.0.0.1:8080", "<local>", "http://wpad/wpad.dat")
    off = without_proxy(settings)
    assert not off.active and off.auto_detect
    assert (off.server, off.bypass, off.script_url) == (settings.server, settings.bypass, settings.script_url)


def test_run_reset_tolerates_protected_keys(monkeypatch):
    tcpip = next(action for action in RESET_ACTIONS if action.key == "tcpip")
    outputs = iter([CommandError(["netsh"], "Resetting Global, OK!\nResetting Echo Request, failed.\nAccess is "
                                            "denied.\nRestart the computer to complete this action."),
                    "Resetting Interface, OK!"])

    def fake(command, timeout=120):
        result = next(outputs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(netreset, "run_command", fake)
    assert "Resetting Interface, OK!" in run_reset(tcpip)


def test_run_reset_reports_real_failures(monkeypatch):
    winsock = next(action for action in RESET_ACTIONS if action.key == "winsock")

    def fake(command, timeout=120):
        raise CommandError(command, "The requested operation requires elevation.")

    monkeypatch.setattr(netreset, "run_command", fake)
    with pytest.raises(CommandError, match="elevation"):
        run_reset(winsock)


def test_actions_that_change_the_system_warn_first():
    for action in RESET_ACTIONS:
        if action.needs_restart:
            assert action.warning and action.needs_admin
    assert not next(action for action in RESET_ACTIONS if action.key == "flush_dns").needs_admin
