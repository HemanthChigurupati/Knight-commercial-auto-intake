"""Intake: turn whatever arrived (an .eml, a zip, loose files) into a flat list of
source files, with nested archives expanded and byte-identical duplicates linked.

Nothing here looks at file names to decide what a document *is*; names are kept only
for display and for flagging name/content mismatches later.
"""
from __future__ import annotations

import email
import hashlib
import mimetypes
import shutil
import zipfile
from email import policy
from pathlib import Path, PurePosixPath

from ..schemas import SourceFile

MAX_ARCHIVE_DEPTH = 3
MAX_TOTAL_BYTES = 200 * 1024 * 1024     # zip-bomb guard


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    # Strip directories and anything that could escape the working folder (zip-slip).
    return PurePosixPath(name.replace("\\", "/")).name or "unnamed"


def _expand(path: Path, logical: str, work: Path, depth: int, out: list[tuple[Path, str]],
            budget: list[int]) -> None:
    suffix = path.suffix.lower()
    if suffix == ".zip" and depth < MAX_ARCHIVE_DEPTH:
        budget[1] += 1
        target = work / f"x{budget[1]}_{path.stem}"
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename.startswith("__MACOSX/"):
                    continue
                budget[0] -= info.file_size
                if budget[0] < 0:
                    raise ValueError("archive expands beyond the size limit")
                budget[1] += 1
                dest = target / f"{budget[1]}_{_safe_name(info.filename)}"
                with zf.open(info) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                _expand(dest, f"{logical}/{info.filename}", work, depth + 1, out, budget)
        return
    if suffix == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            body_path = work / f"{len(out)}_email_body.txt"
            body_path.write_text(
                f"From: {msg['from']}\nTo: {msg['to']}\nSubject: {msg['subject']}\n"
                f"Date: {msg['date']}\n\n{body.get_content()}")
            out.append((body_path, f"{logical}#body"))
        for part in msg.iter_attachments():
            name = _safe_name(part.get_filename() or "attachment")
            budget[1] += 1
            dest = work / f"{budget[1]}_{name}"
            dest.write_bytes(part.get_payload(decode=True) or b"")
            _expand(dest, f"{logical}/{name}", work, depth + 1, out, budget)
        return
    out.append((path, logical))


def ingest(inputs: list[Path], work_dir: Path) -> list[SourceFile]:
    """Expand every input into leaf files and register each one with its hash."""
    work_dir.mkdir(parents=True, exist_ok=True)
    leaves: list[tuple[Path, str]] = []
    budget = [MAX_TOTAL_BYTES, 0]          # [bytes left, unique-name counter]
    for p in inputs:
        _expand(Path(p), Path(p).name, work_dir, 0, leaves, budget)

    files: list[SourceFile] = []
    first_by_hash: dict[str, str] = {}
    for i, (path, logical) in enumerate(leaves):
        digest = _sha256(path)
        file_id = f"f{i:03d}"
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files.append(SourceFile(
            file_id=file_id, name=PurePosixPath(logical).name, path_in_package=logical,
            sha256=digest, size=path.stat().st_size, media_type=media,
            duplicate_of=first_by_hash.get(digest)))
        first_by_hash.setdefault(digest, file_id)
        # Keep the working copy addressable by id.
        stored = work_dir / "files" / f"{file_id}{path.suffix.lower()}"
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, stored)
    return files


def stored_path(work_dir: Path, f: SourceFile) -> Path:
    return work_dir / "files" / f"{f.file_id}{PurePosixPath(f.name).suffix.lower()}"
