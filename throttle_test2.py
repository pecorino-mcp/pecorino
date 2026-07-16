import psutil
import time
import math
import threading
from concurrent.futures import ThreadPoolExecutor

_THROTTLE = False

def monitor():
    global _THROTTLE
    while True:
        pct = psutil.cpu_percent(interval=0.5)
        _THROTTLE = (pct > 75.0)

threading.Thread(target=monitor, daemon=True).start()

def worker(i):
    for _ in range(100):
        if _THROTTLE:
            time.sleep(0.1)
        math.factorial(20000)

with ThreadPoolExecutor(max_workers=8) as ex:
    for i in range(16):
        ex.submit(worker, i)
