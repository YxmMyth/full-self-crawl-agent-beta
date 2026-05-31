"""ArtifactService — read-only filesystem view of a run's artifacts.

Lists samples / catalog / workspace(+data) / research / verification with size +
mtime, computes inventory deltas for the event bus, and serves file contents.
Path-jailed: every served path is resolved and checked to live under the run
dir, so the console can never read outside artifacts/{domain}/runs/{run_id}/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from web.config import WebConfig

# Directories inside a run dir the console surfaces.
ARTIFACT_DIRS = ["samples", "catalog", "workspace", "research", "verification"]
# Where harvest crawlers write the full dataset — probe both common spots.
DATA_SUBDIRS = ["workspace/data", "data"]

# Cap on a single served file (avoid streaming a huge dataset into the UI).
MAX_SERVE_BYTES = 2_000_000


class ArtifactService:
    """Read-only view of artifacts on disk. No writes."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}  # path -> last-seen size, for deltas

    # ── Run discovery (cross-domain, filesystem) ─────────

    def list_all_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Scan artifacts/*/runs/* for recent runs across all domains, newest first.

        Filesystem-based so the launch page works without a domain filter
        (db.list_runs needs a domain).
        """
        root = WebConfig.ARTIFACTS_DIR
        out: list[dict[str, Any]] = []
        if not root.is_dir():
            return out
        for domain_dir in root.iterdir():
            if not domain_dir.is_dir() or domain_dir.name.startswith("_"):
                continue  # skip _profiles etc.
            runs_dir = domain_dir / "runs"
            if not runs_dir.is_dir():
                continue
            for rd in runs_dir.iterdir():
                if not rd.is_dir():
                    continue
                req = ""
                req_file = rd / "requirement.txt"
                if req_file.is_file():
                    try:
                        req = req_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        req = ""
                has_models = (rd / "verification").is_dir() or (
                    rd / "strategy_report.md"
                ).is_file()
                try:
                    mtime = rd.stat().st_mtime
                except OSError:
                    mtime = 0.0
                out.append(
                    {
                        "run_id": rd.name,
                        "domain": domain_dir.name,
                        "requirement": req,
                        "mtime": mtime,
                        "has_models": has_models,
                    }
                )
        out.sort(key=lambda r: r["mtime"], reverse=True)
        return out[:limit]

    # ── Per-run inventory ────────────────────────────────

    def inventory(self, domain: str, run_id: str) -> dict[str, Any]:
        run_dir = WebConfig.run_dir(domain, run_id)
        dirs: dict[str, list[dict[str, Any]]] = {}
        for name in ARTIFACT_DIRS:
            dirs[name] = self._list_dir(run_dir / name)

        data_files = 0
        data_bytes = 0
        for rel in DATA_SUBDIRS:
            for f in self._iter_files(run_dir / rel):
                data_files += 1
                try:
                    data_bytes += f.stat().st_size
                except OSError:
                    pass

        return {
            "dirs": dirs,
            "counts": {
                "samples": len(dirs.get("samples", [])),
                "catalog": len(dirs.get("catalog", [])),
                "data_files": data_files,
                "data_bytes": data_bytes,
            },
        }

    def counts(self, domain: str, run_id: str) -> dict[str, int]:
        return self.inventory(domain, run_id)["counts"]

    def diff_new_files(self, domain: str, run_id: str) -> list[dict[str, Any]]:
        """Files added or grown since the last call (drives artifact events)."""
        run_dir = WebConfig.run_dir(domain, run_id)
        changes: list[dict[str, Any]] = []
        for name in list(ARTIFACT_DIRS) + DATA_SUBDIRS:
            base = run_dir / name
            label = "data" if name in DATA_SUBDIRS else name
            for f in self._iter_files(base):
                key = str(f)
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                prev = self._seen.get(key)
                if prev is None:
                    changes.append(
                        {"dir": label, "filename": f.name, "size": size, "change": "added"}
                    )
                    self._seen[key] = size
                elif size != prev:
                    changes.append(
                        {"dir": label, "filename": f.name, "size": size, "change": "changed"}
                    )
                    self._seen[key] = size
        return changes

    def reset(self) -> None:
        self._seen.clear()

    # ── File serving (path-jailed) ───────────────────────

    def resolve_in_run(self, domain: str, run_id: str, rel_path: str) -> Path | None:
        run_dir = WebConfig.run_dir(domain, run_id).resolve()
        target = (run_dir / rel_path).resolve()
        try:
            target.relative_to(run_dir)
        except ValueError:
            return None
        return target

    def read_text_file(self, path: Path) -> str:
        data = path.read_bytes()[:MAX_SERVE_BYTES]
        return data.decode("utf-8", errors="replace")

    # ── Internals ────────────────────────────────────────

    def _list_dir(self, d: Path) -> list[dict[str, Any]]:
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.iterdir()):
            if f.is_file():
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def _iter_files(self, d: Path) -> Iterator[Path]:
        if not d.is_dir():
            return
        for f in d.rglob("*"):
            if f.is_file():
                yield f
