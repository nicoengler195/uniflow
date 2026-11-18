"""One-shot Qt workers. Functions own their own database sessions."""
import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from uniflow.gui.presentation import present_error

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    completed = Signal(object, object, object)


class Worker(QRunnable):
    def __init__(self, operation, token, fn):
        super().__init__()
        self.operation, self.token, self.fn = operation, token, fn
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.fn()
            self.signals.completed.emit(self.operation, self.token, result)
        except Exception as exc:
            if present_error(exc).code == "APPLICATION_FAILURE":
                log.exception("GUI application operation failed: %s", self.operation)
            self.signals.completed.emit(self.operation, self.token, present_error(exc))


class TaskRunner:
    def __init__(self, pool=None):
        self.pool = pool or QThreadPool.globalInstance()
        self._active = set()

    def submit(self, operation, token, fn, callback):
        worker = Worker(operation, token, fn)
        self._active.add(worker)
        def finish(op, generation, result):
            self._active.discard(worker)
            callback(op, generation, result)
        worker.signals.completed.connect(finish)
        self.pool.start(worker)
