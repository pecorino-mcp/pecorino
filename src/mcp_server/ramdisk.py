import logging

"""
RAM-backed index builder.

Builds DuckDB and Gorgonzola indexes in /dev/shm (tmpfs) to avoid SSD write
amplification, then copies the final database files to their persistent SSD
location in a single, sequential write.

Usage as a context manager:

    with RamdiskIndex(ssd_db_path, max_bytes=60*1024*1024) as ram:
        # ram.db_path  -> points to /dev/shm/<session>/...duckdb
        # ram.gorgonzola_path -> /dev/shm/<session>/..._gorgonzola/
        # ... do all indexing work against ram.db_path ...
    # On __exit__, files are synced to SSD and the tmpfs dir is cleaned up.
"""

import os
import shutil
import sys
import time
import uuid

from filelock import FileLock

logger = logging.getLogger(__name__)



_SHM_ROOT = "/dev/shm"


class RamdiskQuotaExceeded(Exception):
    """Raised when the ramdisk usage exceeds the configured quota."""
    pass


class RamdiskIndex:
    """Manages a tmpfs-backed workspace for index building."""

    def __init__(self, ssd_db_path: str, max_bytes: int = 60 * 1024 * 1024):
        """
        Args:
            ssd_db_path: The *final* DuckDB path on the SSD
                         (e.g. ~/.pecorino/indexes/<hash>_code_search.duckdb).
            max_bytes:   Hard cap on total bytes written to the ramdisk dir.
                         Default 60 MB.  Set to 0 to disable the quota.
        """
        self.ssd_db_path = os.path.abspath(ssd_db_path)
        self.max_bytes = max_bytes

        # Derive the Gorgonzola dir name exactly as index_db does
        from src.mcp_server.index_db import get_graph_path_for_repo
        self.ssd_gorgonzola_path = get_graph_path_for_repo(self.ssd_db_path)

        # Session directory under /dev/shm
        session_id = f"pecorino_{uuid.uuid4().hex[:12]}"
        self.ram_dir = os.path.join(_SHM_ROOT, session_id)

        # Mirror the filenames inside the ramdisk dir
        db_basename = os.path.basename(self.ssd_db_path)
        gorgonzola_basename = os.path.basename(self.ssd_gorgonzola_path)

        self.db_path = os.path.join(self.ram_dir, db_basename)
        self.gorgonzola_path = os.path.join(self.ram_dir, gorgonzola_basename)

        self._active = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        os.makedirs(self.ram_dir, exist_ok=True)
        self._active = True

        # Copy existing database and graph files from SSD to RAM disk if they exist
        if os.path.exists(self.ssd_db_path):
            shutil.copy2(self.ssd_db_path, self.db_path)
        if os.path.exists(self.ssd_gorgonzola_path) and os.path.isdir(self.ssd_gorgonzola_path):
            shutil.copytree(self.ssd_gorgonzola_path, self.gorgonzola_path)

        logger.info(f"[ramdisk] Building index in RAM: {self.ram_dir} "
              f"(quota {self.max_bytes / 1024 / 1024:.0f} MB)")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._active = False
        if exc_type is not None:
            # On error, clean up ramdisk but do NOT overwrite SSD data
            logger.info(f"[ramdisk] Indexing failed ({exc_type.__name__}), "
                  f"discarding ramdisk build.",
                  file=sys.stderr, flush=True)
            self._cleanup()
            return False  # re-raise the exception

        # Success — sync to SSD
        try:
            self._sync_to_ssd()
        finally:
            self._cleanup()
        return False

    # ------------------------------------------------------------------
    # Quota enforcement
    # ------------------------------------------------------------------

    def check_quota(self):
        """Check if the ramdisk directory exceeds the quota.
        Call this periodically during indexing."""
        if self.max_bytes <= 0:
            return
        usage = self._dir_size(self.ram_dir)
        if usage > self.max_bytes:
            raise RamdiskQuotaExceeded(
                f"Ramdisk usage {usage / 1024 / 1024:.1f} MB exceeds "
                f"quota of {self.max_bytes / 1024 / 1024:.0f} MB"
            )

    def get_usage_bytes(self) -> int:
        """Return current ramdisk usage in bytes."""
        if not os.path.isdir(self.ram_dir):
            return 0
        return self._dir_size(self.ram_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dir_size(self, path: str) -> int:
        """Recursively compute total size of files under `path`."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def _sync_to_ssd(self):
        """Copy finished databases from ramdisk to their SSD locations."""
        t0 = time.monotonic()

        # 1. Sync DuckDB file
        if os.path.exists(self.db_path):
            ssd_dir = os.path.dirname(self.ssd_db_path)
            os.makedirs(ssd_dir, exist_ok=True)

            with FileLock(self.ssd_db_path + ".lock"):
                # Copy to a temp name first, then atomic rename
                tmp_ssd = self.ssd_db_path + ".tmp"
                shutil.copy2(self.db_path, tmp_ssd)
                os.replace(tmp_ssd, self.ssd_db_path)

                # Also copy any DuckDB WAL files (.duckdb.wal)
                wal_path = self.db_path + ".wal"
                if os.path.exists(wal_path):
                    ssd_wal = self.ssd_db_path + ".wal"
                    tmp_wal = ssd_wal + ".tmp"
                    shutil.copy2(wal_path, tmp_wal)
                    os.replace(tmp_wal, ssd_wal)
                else:
                    # Remove stale WAL on SSD if ramdisk has none
                    ssd_wal = self.ssd_db_path + ".wal"
                    if os.path.exists(ssd_wal):
                        os.remove(ssd_wal)

            db_size = os.path.getsize(self.ssd_db_path)
            logger.info(f"[ramdisk] Synced DuckDB to SSD: "
                  f"{db_size / 1024 / 1024:.2f} MB")

        # 2. Sync Gorgonzola file (single file, not a directory)
        if os.path.exists(self.gorgonzola_path):
            ssd_parent = os.path.dirname(self.ssd_gorgonzola_path)
            os.makedirs(ssd_parent, exist_ok=True)

            with FileLock(self.ssd_gorgonzola_path + ".lock"):
                if os.path.isfile(self.gorgonzola_path):
                    tmp_gorgonzola = self.ssd_gorgonzola_path + ".tmp"
                    shutil.copy2(self.gorgonzola_path, tmp_gorgonzola)
                    os.replace(tmp_gorgonzola, self.ssd_gorgonzola_path)
                    gorg_size = os.path.getsize(self.ssd_gorgonzola_path)
                    logger.info(f"[ramdisk] Synced Gorgonzola to SSD: "
                          f"{gorg_size / 1024 / 1024:.2f} MB")
                elif os.path.isdir(self.gorgonzola_path):
                    # Fallback: if it's a directory (future Gorgonzola versions)
                    tmp_gorgonzola = self.ssd_gorgonzola_path + ".tmp"
                    if os.path.exists(tmp_gorgonzola):
                        shutil.rmtree(tmp_gorgonzola)
                    shutil.copytree(self.gorgonzola_path, tmp_gorgonzola)
                    if os.path.exists(self.ssd_gorgonzola_path):
                        shutil.rmtree(self.ssd_gorgonzola_path)
                    os.rename(tmp_gorgonzola, self.ssd_gorgonzola_path)
                    gorg_size = self._dir_size(self.ssd_gorgonzola_path)
                    logger.info(f"[ramdisk] Synced Gorgonzola (dir) to SSD: "
                          f"{gorg_size / 1024 / 1024:.2f} MB")

                # Also sync any Gorgonzola WAL
                gorg_wal = self.gorgonzola_path + ".wal"
                if os.path.exists(gorg_wal):
                    ssd_gwal = self.ssd_gorgonzola_path + ".wal"
                    tmp_gwal = ssd_gwal + ".tmp"
                    shutil.copy2(gorg_wal, tmp_gwal)
                    os.replace(tmp_gwal, ssd_gwal)
                else:
                    ssd_gwal = self.ssd_gorgonzola_path + ".wal"
                    if os.path.exists(ssd_gwal):
                        os.remove(ssd_gwal)
        else:
            logger.warning(f"[ramdisk] Expected Gorgonzola graph at {self.gorgonzola_path} but it was not found. Graph data will be missing on SSD.", file=sys.stderr, flush=True)

        elapsed = time.monotonic() - t0
        total_bytes = self.get_usage_bytes()
        logger.info(f"[ramdisk] SSD sync completed in {elapsed:.2f}s — "
              f"wrote {total_bytes / 1024 / 1024:.2f} MB to SSD")

    def _cleanup(self):
        """Remove the ramdisk directory and its contents."""
        if os.path.exists(self.ram_dir):
            shutil.rmtree(self.ram_dir)
            logger.info(f"[ramdisk] Cleaned up: {self.ram_dir}")
