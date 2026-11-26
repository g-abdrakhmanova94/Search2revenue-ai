"""Bound the number of running and waiting searches."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore


class SearchQueue:
    def __init__(self, workers: int = 4, waiting: int = 12, wait_seconds: float = 6):
        self._workers = BoundedSemaphore(workers)
        self._admission = BoundedSemaphore(workers + waiting)
        self._wait_seconds = wait_seconds

    @contextmanager
    def slot(self) -> Iterator[bool]:
        admitted = self._admission.acquire(blocking=False)
        if not admitted:
            yield False
            return
        try:
            running = self._workers.acquire(timeout=self._wait_seconds)
            try:
                yield running
            finally:
                if running:
                    self._workers.release()
        finally:
            self._admission.release()
