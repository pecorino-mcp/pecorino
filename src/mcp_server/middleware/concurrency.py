import logging
import threading
import time
from functools import wraps

logger = logging.getLogger(__name__)

class FIFOConcurrencyLimiter:
    """
    FIFO queue-based concurrency limiter with timeout.

    Ensures requests are processed in arrival order while limiting
    concurrent executions. Uses a ticket-based system for fairness.
    """

    def __init__(self, max_concurrent: int, timeout: float = 60.0):
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_count = 0
        self._next_ticket = 0
        self._serving_ticket = 0

    def acquire(self, timeout: float = None) -> int:
        """Acquire a slot in FIFO order. Returns ticket number.

        Raises TimeoutError if slot cannot be acquired within timeout.
        """
        timeout = timeout or self._timeout

        with self._condition:
            my_ticket = self._next_ticket
            self._next_ticket += 1

            # Wait until it's our turn AND there's capacity
            start = time.monotonic()

            while self._serving_ticket != my_ticket or self._active_count >= self._max_concurrent:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    # Timeout: skip our ticket so others can proceed
                    if self._serving_ticket == my_ticket:
                        self._serving_ticket += 1
                        self._condition.notify_all()
                    raise TimeoutError(f"Queue timeout after {timeout}s (ticket {my_ticket})")

                self._condition.wait(timeout=min(remaining, 1.0))

            # It's our turn, take the slot
            self._active_count += 1
            self._serving_ticket += 1
            self._condition.notify_all()
            return my_ticket

    def release(self):
        """Release a slot."""
        with self._condition:
            self._active_count -= 1
            self._condition.notify_all()

    @property
    def stats(self) -> dict:
        """Get current queue statistics."""
        with self._lock:
            return {
                "active": self._active_count,
                "max_concurrent": self._max_concurrent,
                "next_ticket": self._next_ticket,
                "serving_ticket": self._serving_ticket,
                "queued": self._next_ticket - self._serving_ticket
            }
