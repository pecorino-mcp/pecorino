import sys, os, glob
from unittest.mock import patch
from src.mcp_server.ramdisk import RamdiskIndex
import src.mcp_server.index_worker as worker

# Monkey-patch RamdiskIndex to use SSD directly
def mock_init(self, ssd_db_path, max_bytes=None):
    self.ssd_db_path = os.path.abspath(ssd_db_path)
    if ssd_db_path.endswith(".duckdb"):
        self.ssd_gorgonzola_path = ssd_db_path[:-7] + "_gorgonzola"
    else:
        self.ssd_gorgonzola_path = ssd_db_path + "_gorgonzola"
    self.db_path = self.ssd_db_path
    self.gorgonzola_path = self.ssd_gorgonzola_path
    self._active = False

def mock_enter(self):
    print("[test] Bypassing ramdisk, writing DIRECTLY TO SSD")
    return self
    
def mock_exit(self, exc_type, exc_val, exc_tb):
    print("[test] SSD run finished")
    return False

def mock_check_quota(self):
    pass

def mock_get_usage_bytes(self):
    return 0

RamdiskIndex.__init__ = mock_init
RamdiskIndex.__enter__ = mock_enter
RamdiskIndex.__exit__ = mock_exit
RamdiskIndex.check_quota = mock_check_quota
RamdiskIndex.get_usage_bytes = mock_get_usage_bytes

# Clear old indexes
pattern = os.path.expanduser('~/.gitstats3/indexes/9b588785d0c8a4209b75688b097de1a3_*')
for f in glob.glob(pattern):
    os.remove(f)
print("Cleaned old indexes")

# Run worker
sys.argv = ["index_worker.py", "/run/media/lechibang/cb09d199-3769-4ec8-9af5-954929515428/projects/gitstats3", "/run/media/lechibang/cb09d199-3769-4ec8-9af5-954929515428/projects/gitstats3"]
worker.main()
