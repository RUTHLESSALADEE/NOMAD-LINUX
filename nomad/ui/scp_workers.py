"""Background threads for an SCP tab: logging in, browsing (one remote operation at a time), and the transfer queue
(one file at a time, on a file system channel of its own so browsing stays quick during big transfers)."""
import logging
import os
import queue
import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal

from ..terminal.files import ASK, CANCELLED, DONE, DOWNLOAD, FAILED, PART_SUFFIX, PAUSED, QUEUED, RUNNING, SKIP, \
    UPLOAD, RemoteError, TransferCancelled, TransferRunner, expand
from ..terminal.transports import ConnectionFailed
from .common import StoppableThread

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 0.15  # Seconds between progress updates for one file


class ConnectThread(StoppableThread):
    connected = pyqtSignal(object)  # The browsing file system
    failed = pyqtSignal(str)

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.connection = connection

    def run(self):
        try:
            fs = self.connection.connect()
        except ConnectionFailed as error:
            self.failed.emit(str(error))
            return
        except Exception as error:  # Anything unexpected still has to end the "Connecting..." state
            log.exception("Unexpected error connecting")
            self.failed.emit(f"Couldn't connect: {error}")
            return
        if self.stopping:
            self.connection.close()
            return
        self.connected.emit(fs)


class TaskWorker(QThread):
    """Runs browsing operations (list a folder, rename, delete...) one after another on the browsing file system.
    submit(function, done, failed): function(fs) runs here; done(result) or failed(message) run on the UI thread."""
    finished_task = pyqtSignal(object, object, object)  # (callback, result, error message or None)
    busy_changed = pyqtSignal(bool)

    def __init__(self, fs, parent=None):
        super().__init__(parent)
        self.fs = fs
        self.tasks = queue.Queue()
        self.finished_task.connect(self.deliver)
        self.cancel_event = threading.Event()

    def submit(self, function, done=None, failed=None):
        self.tasks.put((function, done, failed))

    def cancelled(self):
        return self.cancel_event.is_set()

    def stop(self):
        self.cancel_event.set()
        self.tasks.put(None)

    def run(self):
        while True:
            task = self.tasks.get()
            if task is None:
                break
            function, done, failed = task
            self.busy_changed.emit(True)
            try:
                result = function(self.fs)
            except (RemoteError, TransferCancelled, OSError) as error:
                message = "Cancelled." if isinstance(error, TransferCancelled) else \
                    str(error) if isinstance(error, RemoteError) else f"{error.filename or ''} {error.strerror or error}"
                self.finished_task.emit(failed, None, message.strip())
            except Exception as error:
                log.exception("Remote operation failed")
                self.finished_task.emit(failed, None, f"Unexpected error: {error}")
            else:
                self.finished_task.emit(done, result, None)
            if self.tasks.empty():
                self.busy_changed.emit(False)

    @staticmethod
    def deliver(callback, result, error):
        if callback is None:
            return
        if error is None:
            callback(result)
        else:
            callback(error)


class ConflictRequest:
    def __init__(self, transfer, existing):
        self.transfer, self.existing = transfer, existing
        self.result = None
        self.done = threading.Event()


class TransferWorker(QThread):
    """Works through the view's transfer list. The list and each Transfer's state are shared with the UI thread;
    changes go through `lock`."""
    changed = pyqtSignal(object)  # A Transfer whose progress or state changed
    added = pyqtSignal(object, list)  # A folder transfer, and the file transfers it expanded into
    finished_one = pyqtSignal(object)  # A Transfer that ended (done, failed, skipped...)
    conflict = pyqtSignal(object)  # A ConflictRequest for the UI to answer
    connection_lost = pyqtSignal(str)

    def __init__(self, connection, transfers, lock, parent=None):
        super().__init__(parent)
        self.connection = connection
        self.transfers = transfers
        self.lock = lock
        self.wake = threading.Condition(lock)
        self.stopping = False
        self.paused = False
        self.conflict_policy = ASK
        self.preserve_times = True
        self.runner = None
        self.current = None
        self.cancel_requested = set()  # Transfer ids to cancel (rather than pause) when they stop
        self.pending_conflict = None
        self.last_emit = 0.0

    # ----------------------------------------------------------------- Called from the UI thread

    def poke(self):
        with self.wake:
            self.wake.notify_all()

    def set_paused(self, paused):
        with self.wake:
            self.paused = paused
            if paused and self.runner is not None and self.current is not None:
                self.runner.cancel_current = True
            if not paused:
                for transfer in self.transfers:
                    if transfer.state == PAUSED:
                        transfer.state, transfer.message = QUEUED, ""
                        self.changed.emit(transfer)
            self.wake.notify_all()

    def set_conflict_policy(self, policy):
        """What to do when a file exists (ASK, OVERWRITE...). Also forgets an earlier "apply to all" answer."""
        with self.wake:
            self.conflict_policy = policy
            if self.runner is not None:
                self.runner.conflict = policy

    def cancel(self, transfer):
        with self.wake:
            if transfer is self.current and self.runner is not None:
                self.cancel_requested.add(transfer.id)
                self.runner.cancel_current = True
            elif transfer.state in (QUEUED, PAUSED):
                transfer.state, transfer.message = CANCELLED, ""
                self.changed.emit(transfer)

    def stop(self):
        with self.wake:
            self.stopping = True
            if self.runner is not None:
                self.runner.cancel_current = True
            if self.pending_conflict is not None:
                self.pending_conflict.done.set()
            self.wake.notify_all()

    # ----------------------------------------------------------------- The thread

    def next_transfer(self):
        with self.wake:
            while not self.stopping:
                if not self.paused:
                    for transfer in self.transfers:
                        if transfer.state == QUEUED:
                            transfer.state = RUNNING
                            self.current = transfer
                            return transfer
                self.wake.wait()
        return None

    def run(self):
        fs = None
        while True:
            transfer = self.next_transfer()
            if transfer is None:
                break
            try:
                if fs is None:
                    fs = self.connection.open_fs()
                    self.runner = TransferRunner(fs, self.conflict_policy, self.ask, self.report,
                                                 self.preserve_times)
                if transfer.is_dir:
                    self.expand(fs, transfer)
                else:
                    self.runner.run(transfer)
                    self.after_run(fs, transfer)
            except (ConnectionFailed, RemoteError, OSError) as error:
                transfer.state, transfer.message = FAILED, str(error)
                self.changed.emit(transfer)
            finally:
                with self.wake:
                    self.current = None
            self.finished_one.emit(transfer)
            if not self.connection.active:
                self.fail_rest("The connection closed.")
                self.connection_lost.emit("The connection closed during a transfer.")
                break
        if fs is not None:
            fs.close()

    def fail_rest(self, message):
        with self.wake:
            for transfer in self.transfers:
                if transfer.state in (QUEUED, RUNNING):
                    transfer.state, transfer.message = FAILED, message
                    self.changed.emit(transfer)

    def expand(self, fs, transfer):
        """Create a folder transfer's folders, then queue its files straight after it."""
        transfer.message = "Listing..."
        self.changed.emit(transfer)
        try:
            folders, files = expand(fs, transfer, lambda: self.stopping)
        except TransferCancelled:
            transfer.state = CANCELLED
            self.changed.emit(transfer)
            return
        for folder in folders:
            if transfer.direction == UPLOAD:
                if fs.stat(folder) is None:
                    fs.mkdir(folder)
            else:
                os.makedirs(folder, exist_ok=True)
        with self.wake:
            index = self.transfers.index(transfer) + 1
            self.transfers[index:index] = files
        transfer.state, transfer.size = DONE, sum(item.size for item in files)
        transfer.message = f"{len(files)} file{'' if len(files) == 1 else 's'}"
        self.added.emit(transfer, files)
        self.changed.emit(transfer)

    def after_run(self, fs, transfer):
        """A file stopped: sort out Pause (resume later) and Cancel (throw the partial file away)."""
        if transfer.state != CANCELLED:
            return
        with self.wake:
            cancelled = transfer.id in self.cancel_requested or self.stopping
            self.cancel_requested.discard(transfer.id)
            if not cancelled and self.paused:
                transfer.state = PAUSED if fs.can_resume else QUEUED
                transfer.message = "Paused"
                self.changed.emit(transfer)
                return
        if cancelled and not self.stopping:
            self.discard_part(fs, transfer)
        transfer.message = ""
        self.changed.emit(transfer)

    @staticmethod
    def discard_part(fs, transfer):
        try:
            if transfer.direction == DOWNLOAD:
                part = transfer.local + PART_SUFFIX
                if os.path.exists(part):
                    os.remove(part)
            elif fs.can_resume and fs.stat(transfer.remote + PART_SUFFIX) is not None:
                fs.remove(transfer.remote + PART_SUFFIX)
        except (OSError, RemoteError) as error:
            log.info("Couldn't remove the partial file of %s: %s", transfer.name, error)

    def report(self, transfer):
        now = time.monotonic()
        if transfer.state != RUNNING or now - self.last_emit >= PROGRESS_INTERVAL or transfer.done >= transfer.size:
            self.last_emit = now
            self.changed.emit(transfer)

    def ask(self, transfer, existing):
        """The runner found the target exists: ask the UI (blocks until answered)."""
        request = ConflictRequest(transfer, existing)
        with self.wake:
            if self.stopping:
                return SKIP, False
            self.pending_conflict = request
        self.conflict.emit(request)
        request.done.wait()
        with self.wake:
            self.pending_conflict = None
        return request.result or (SKIP, False)
