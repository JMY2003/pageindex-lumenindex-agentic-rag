import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .indexer import index_document
from .storage import atomic_gzip_json, atomic_json, now_iso


def _read(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: worker.py request.json progress.json result.json", file=sys.stderr)
        return 2
    request_path = Path(sys.argv[1])
    progress_path = Path(sys.argv[2])
    result_path = Path(sys.argv[3])
    request = _read(request_path)
    source = Path(request["upload_path"])
    output_dir = Path(request["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    def progress(value: int, message: str) -> None:
        atomic_json(progress_path, {"status": "indexing", "progress": value, "message": message, "updated_time": now_iso(), "pid": os.getpid()})

    try:
        progress(1, "worker started")
        metadata, pages = index_document(source, progress)
        atomic_json(output_dir / "metadata.json", metadata)
        atomic_gzip_json(output_dir / "pages.json.gz", pages)
        atomic_json(result_path, {"status": "completed", "metadata_path": str(output_dir / "metadata.json"), "pages_path": str(output_dir / "pages.json.gz"), "page_count": len(pages), "updated_time": now_iso()})
        progress(100, "worker completed")
        return 0
    except Exception as exc:
        atomic_json(
            result_path,
            {
                "status": "failed",
                "error": str(exc),
                "traceback": traceback.format_exc(limit=20),
                "updated_time": now_iso(),
            },
        )
        progress(100, "worker failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
