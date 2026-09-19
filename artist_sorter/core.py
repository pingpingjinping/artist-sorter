from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ID_TOKEN = re.compile(r"(?<!\d)(\d{4,10})(?!\d)")


@dataclass(frozen=True)
class GalleryInfo:
    gallery_id: int
    title: str
    artists: tuple[str, ...]


@dataclass(frozen=True)
class ScanItem:
    source: Path
    gallery_id: int | None


@dataclass(frozen=True)
class PlanItem:
    source: Path
    gallery_id: int | None
    title: str
    artists: tuple[str, ...]
    artist_folder: str
    destination: Path | None
    status: str


def extract_gallery_id(name: str) -> int | None:
    stem = Path(name).stem.strip()
    if stem.isdigit() and 4 <= len(stem) <= 10:
        return int(stem)
    match = ID_TOKEN.search(stem)
    return int(match.group(1)) if match else None


def parse_artists(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    values = [part.strip() for part in raw.split("|") if part.strip()]
    return tuple(v for v in values if v.casefold() not in {"n/a", "unknown"})


def sanitize_windows_name(value: str, fallback: str = "_UNKNOWN") -> str:
    value = INVALID_WINDOWS_CHARS.sub("_", value).strip().rstrip(".")
    value = re.sub(r"\s+", " ", value)
    if not value:
        return fallback
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.upper() in reserved:
        value = f"_{value}"
    return value[:120]


def choose_artist_folder(artists: tuple[str, ...], strategy: Literal["first", "combined"]) -> str:
    if not artists:
        return "_NO_ARTIST"
    if strategy == "combined" and len(artists) > 1:
        return sanitize_windows_name(" + ".join(artists))
    return sanitize_windows_name(artists[0])


def scan_top_level(source_dir: Path) -> list[ScanItem]:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"원본 폴더를 찾을 수 없습니다: {source_dir}")

    items: list[ScanItem] = []
    for child in sorted(source_dir.iterdir(), key=lambda p: p.name.casefold()):
        if child.name.startswith("."):
            continue
        items.append(ScanItem(child, extract_gallery_id(child.name)))
    return items


def validate_violet_db(db_path: Path) -> None:
    if not db_path.is_file():
        raise FileNotFoundError(f"DB 파일을 찾을 수 없습니다: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='HitomiColumnModel'"
        ).fetchone()
        if not row:
            raise ValueError("Violet DB가 아닙니다: HitomiColumnModel 테이블이 없습니다.")
        cols = {r[1] for r in db.execute("PRAGMA table_info(HitomiColumnModel)")}
        required = {"Id", "Title", "Artists"}
        if not required.issubset(cols):
            raise ValueError(f"필수 DB 컬럼이 없습니다: {', '.join(sorted(required - cols))}")


def load_gallery_info(db_path: Path, ids: Iterable[int]) -> dict[int, GalleryInfo]:
    unique = sorted(set(ids))
    if not unique:
        return {}
    validate_violet_db(db_path)
    result: dict[int, GalleryInfo] = {}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        for start in range(0, len(unique), 500):
            batch = unique[start : start + 500]
            marks = ",".join("?" for _ in batch)
            sql = f"SELECT Id, COALESCE(Title, ''), Artists FROM HitomiColumnModel WHERE Id IN ({marks})"
            for gallery_id, title, artists in db.execute(sql, batch):
                result[int(gallery_id)] = GalleryInfo(
                    int(gallery_id), str(title or ""), parse_artists(artists)
                )
    return result


def make_plan(
    source_dir: Path,
    output_dir: Path,
    db_path: Path,
    strategy: Literal["first", "combined"] = "first",
) -> list[PlanItem]:
    scanned = scan_top_level(source_dir)
    valid_ids = [x.gallery_id for x in scanned if x.gallery_id is not None]
    info = load_gallery_info(db_path, valid_ids)
    output_dir = output_dir.expanduser().resolve()

    plan: list[PlanItem] = []
    for item in scanned:
        if item.gallery_id is None:
            plan.append(PlanItem(item.source, None, "", (), "", None, "번호 없음"))
            continue
        gallery = info.get(item.gallery_id)
        if gallery is None:
            plan.append(PlanItem(item.source, item.gallery_id, "", (), "", None, "DB 미매칭"))
            continue
        folder = choose_artist_folder(gallery.artists, strategy)
        destination = output_dir / folder / item.source.name
        status = "준비"
        if destination.exists():
            status = "대상에 이미 존재"
        plan.append(
            PlanItem(
                item.source,
                item.gallery_id,
                gallery.title,
                gallery.artists,
                folder,
                destination,
                status,
            )
        )
    return plan


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def execute_plan(
    plan: Iterable[PlanItem],
    mode: Literal["move", "copy"],
) -> tuple[int, int, list[str]]:
    completed = 0
    skipped = 0
    errors: list[str] = []
    for item in plan:
        if item.status != "준비" or item.destination is None:
            skipped += 1
            continue
        try:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            if item.destination.exists():
                skipped += 1
                continue
            if mode == "move":
                shutil.move(str(item.source), str(item.destination))
            else:
                _copy_path(item.source, item.destination)
            completed += 1
        except Exception as exc:
            errors.append(f"{item.source.name}: {exc}")
    return completed, skipped, errors


def download_database(url: str, destination: Path, timeout: int = 60) -> Path:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("DB URL은 http:// 또는 https:// 로 시작해야 합니다.")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(url, headers={"User-Agent": "ArtistSorter/0.1"})
    fd, temp_name = tempfile.mkstemp(prefix="artist-sorter-", suffix=".db", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, temp_path.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        validate_violet_db(temp_path)
        os.replace(temp_path, destination)
        return destination
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
