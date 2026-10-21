import contextlib
import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class IndexProfiler:
    def __init__(self):
        self.timings = defaultdict(float)

    @contextlib.contextmanager
    def profile(self, phase_name: str):
        start = time.time()
        try:
            yield
        finally:
            self.timings[phase_name] += (time.time() - start)

    def print_summary(self):
        logger.info("=== Indexing Performance Profile ===")
        total = sum(self.timings.values())
        for phase, duration in sorted(self.timings.items(), key=lambda x: x[1], reverse=True):
            pct = (duration / total * 100) if total > 0 else 0
            logger.info(f"{phase:<30}: {duration:.2f}s ({pct:.1f}%)")
        logger.info(f"{'Total':<30}: {total:.2f}s")
        logger.info("====================================")
