"""Shared test set-up: free the Qt objects each test left for deletion.

Tests end with window.deleteLater() and processEvents(), but outside a running event loop processEvents() doesn't
carry out deferred deletes: every test's windows stayed alive until the interpreter shut down, and freeing them all
then crashed now and then (a Windows access violation after the last test, with every test passed).
"""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    yield
    try:
        from PyQt5.QtCore import QCoreApplication, QEvent
    except ImportError:
        return
    if QCoreApplication.instance() is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
