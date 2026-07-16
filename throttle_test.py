import psutil
import time
import math
from concurrent.futures import ThreadPoolExecutor

def worker(i):
    for _ in range(100):
        if psutil.cpu_percent(interval=None) > 75.0:
            time.sleep(0.1)
        math.factorial(50000)

with ThreadPoolExecutor(max_workers=8) as ex:
    for i in range(16):
        ex.submit(worker, i)
