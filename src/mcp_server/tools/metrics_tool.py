import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from src.core.constants import SUPPORTED_EXTENSIONS as SUPPORTED
from src.core.gitdatacollector import GitDataCollector
from src.mcp_server.middleware.security import read_limited, safe_output_path, safe_path
from src.metrics.hotspot import HotspotDetector
from src.metrics.maintainability import (
    calculate_halstead_metrics,
    calculate_loc_metrics,
    calculate_maintainability_index,
    calculate_mccabe_complexity,
)
from src.metrics.oopmetrics import OOPMetricsAnalyzer

ALLOWED_WHAT = frozenset({"oop", "complexity", "hotspots", "all"})

async def do_metrics(target: str, what: List[str] = ["all"], output_path: Optional[str] = None, allow_external: bool = False) -> dict:
    path = safe_path(target, allow_external)
    if output_path:
        safe_out = safe_output_path(output_path)
        safe_out.parent.mkdir(parents=True, exist_ok=True)
        if (path / ".git").exists():
            from src.core.config import conf
            conf['calculate_mi_per_repository'] = True

            data = GitDataCollector()
            await asyncio.to_thread(data.collect, str(path))
            await asyncio.to_thread(data.calculate_mi_for_repository, str(path))
            await asyncio.to_thread(data.calculate_mccabe_for_repository, str(path))
            await asyncio.to_thread(data.calculate_halstead_for_repository, str(path))
            await asyncio.to_thread(data.calculate_oop_for_repository, str(path))
            await asyncio.to_thread(data.refine)

            detector = HotspotDetector(data)
            hotspots = await asyncio.to_thread(detector.analyze)
            summary = detector.get_summary()

            from src.utils.export import MetricsExporter
            exporter = MetricsExporter(data, {"hotspots": hotspots, "summary": summary})
            out_dir = safe_out.parent
            json_file = await asyncio.to_thread(exporter.export_json, str(out_dir))
            generated_path = Path(json_file)
            if generated_path.resolve() != safe_out.resolve():
                import shutil
                await asyncio.to_thread(shutil.move, str(generated_path), str(safe_out))
        else:
            sub_res = await do_metrics(target=target, what=what, output_path=None)
            import json
            await asyncio.to_thread(safe_out.write_text, json.dumps(sub_res, indent=2))

        return {
            "status": "success",
            "report_path": safe_out.as_posix()
        }

    from src.mcp_server.index_db import find_repo_root
    repo_root = find_repo_root(str(path))
    what_set = {w.lower() for w in what if w.lower() in ALLOWED_WHAT}
    if not what_set or "all" in what_set:
        if path.is_file():
            what_set = {"complexity"}
        elif path.is_dir() and (path / ".git").exists():
            what_set = {"hotspots"}
        else:
            what_set = {"oop", "complexity", "hotspots"}

    result: dict[str, Any] = {"target": path.as_posix(), "type": "file" if path.is_file() else "directory"}

    if path.is_file():
        content = await asyncio.to_thread(read_limited, path)
        if "oop" in what_set or "complexity" in what_set:
            analyzer = OOPMetricsAnalyzer(use_ast=True, repo_path=repo_root)
            oop_res = await asyncio.to_thread(analyzer.analyze_file, str(path), content, path.suffix)
            if "oop" in what_set:
                result["oop"] = oop_res
            if "complexity" in what_set:
                loc = await asyncio.to_thread(calculate_loc_metrics, content, path.suffix)
                hal = await asyncio.to_thread(calculate_halstead_metrics, content, path.suffix)
                mcc = await asyncio.to_thread(calculate_mccabe_complexity, content, path.suffix)
                mi = await asyncio.to_thread(calculate_maintainability_index, loc, hal, mcc)
                result["complexity"] = {"loc": loc, "halstead": hal, "mccabe": mcc, "mi": mi}
    else:
        if "oop" in what_set or "complexity" in what_set:
            analyzer = OOPMetricsAnalyzer(use_ast=True, repo_path=repo_root)
            ignore_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", "modules", "third_party", "dataset", "build_test", "build-context"}
            files = []
            for r, d, fnames in os.walk(str(path)):
                d[:] = [dirname for dirname in d if dirname not in ignore_dirs]
                for fname in fnames:
                    fp = Path(r) / fname
                    if fp.suffix in SUPPORTED:
                        files.append(fp)
            import time

            from src.utils.helpers import print_progress_bar
            total_files = len(files)
            start_time = time.time()
            for idx, fp in enumerate(files):
                print_progress_bar(
                    idx,
                    total_files,
                    prefix="[INFO] Calculating metrics",
                    suffix=f"({idx}/{total_files}) {fp.name[:30]:<30}",
                    stream=sys.stderr,
                    start_time=start_time
                )
                try:
                    txt = await asyncio.to_thread(read_limited, fp)
                    analyzer.analyze_file(str(fp), txt, fp.suffix)
                except Exception:
                    pass
            print_progress_bar(
                total_files,
                total_files,
                prefix="[INFO] Calculating metrics",
                suffix=f"Completed ({total_files}/{total_files})",
                stream=sys.stderr,
                start_time=start_time
            )
            analyzer.calculate_afferent_coupling()
            result["metrics_summary"] = analyzer.analyze_package(str(path))

    if "hotspots" in what_set and path.is_dir() and (path/".git").exists():
        data = GitDataCollector()
        await asyncio.to_thread(data.collect, str(path))
        await asyncio.to_thread(data.calculate_mi_for_repository, str(path))
        await asyncio.to_thread(data.refine)
        detector = HotspotDetector(data)
        hotspots = await asyncio.to_thread(detector.analyze)
        result["hotspots"] = hotspots[:30]

    return result
