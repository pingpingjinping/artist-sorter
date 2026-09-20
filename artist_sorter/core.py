from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.request
import uuid
from contextlib import closing
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urljoin, urlparse, urlunparse

INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ID_TOKEN = re.compile(r"(?<!\d)(\d{4,10})(?!\d)")
UNKNOWN_ARTIST_FOLDER = "기타"

DuplicatePolicy = Literal["skip", "rename", "overwrite"]


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
    replace_existing: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    completed: int
    skipped: int
    errors: list[str]


@dataclass(frozen=True)
class DbServerInfo:
    version: int
    download_url: str


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


def choose_artist_folder(
    artists: tuple[str, ...],
    strategy: Literal["first", "combined"],
) -> str:
    if not artists:
        return UNKNOWN_ARTIST_FOLDER
    if strategy == "combined" and len(artists) > 1:
        return sanitize_windows_name(" + ".join(artists))
    return sanitize_windows_name(artists[0])


def _is_same_or_child(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def scan_items(
    source_dir: Path,
    recursive: bool = False,
    exclude_paths: Iterable[Path] = (),
) -> list[ScanItem]:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"원본 폴더를 찾을 수 없습니다: {source_dir}")

    excluded = [p.expanduser().resolve() for p in exclude_paths]

    def excluded_path(path: Path) -> bool:
        return any(_is_same_or_child(path, base) for base in excluded)

    if not recursive:
        items: list[ScanItem] = []
        for child in sorted(source_dir.iterdir(), key=lambda p: p.name.casefold()):
            if child.name.startswith(".") or excluded_path(child):
                continue
            items.append(ScanItem(child, extract_gallery_id(child.name)))
        return items

    items: list[ScanItem] = []

    def walk(folder: Path) -> None:
        for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            if child.name.startswith(".") or excluded_path(child):
                continue
            gallery_id = extract_gallery_id(child.name)
            if child.is_dir():
                if gallery_id is not None:
                    items.append(ScanItem(child, gallery_id))
                else:
                    walk(child)
            elif gallery_id is not None:
                items.append(ScanItem(child, gallery_id))

    walk(source_dir)
    return items


def validate_violet_db(db_path: Path) -> None:
    if not db_path.is_file():
        raise FileNotFoundError(f"DB 파일을 찾을 수 없습니다: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='HitomiColumnModel'"
        ).fetchone()
        if not row:
            raise ValueError(
                "Violet DB가 아닙니다: HitomiColumnModel 테이블이 없습니다."
            )
        cols = {
            r[1] for r in db.execute('PRAGMA table_info("HitomiColumnModel")')
        }
        required = {"Id", "Title", "Artists"}
        if not required.issubset(cols):
            raise ValueError(
                f"필수 DB 컬럼이 없습니다: "
                f"{', '.join(sorted(required - cols))}"
            )


def load_gallery_info(
    db_path: Path,
    ids: Iterable[int],
) -> dict[int, GalleryInfo]:
    unique = sorted(set(ids))
    if not unique:
        return {}
    validate_violet_db(db_path)
    result: dict[int, GalleryInfo] = {}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        for start in range(0, len(unique), 500):
            batch = unique[start:start + 500]
            marks = ",".join("?" for _ in batch)
            sql = (
                "SELECT Id, COALESCE(Title, ''), Artists "
                f"FROM HitomiColumnModel WHERE Id IN ({marks})"
            )
            for gallery_id, title, artists in db.execute(sql, batch):
                result[int(gallery_id)] = GalleryInfo(
                    int(gallery_id),
                    str(title or ""),
                    parse_artists(artists),
                )
    return result


def _destination_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _unique_destination(path: Path, reserved: set[str]) -> Path:
    if not path.exists() and _destination_key(path) not in reserved:
        return path

    if path.is_dir() or (not path.exists() and not path.suffix):
        base = path.name
        suffix = ""
    else:
        base = path.stem
        suffix = path.suffix

    index = 1
    while True:
        candidate = path.with_name(f"{base} ({index}){suffix}")
        if (
            not candidate.exists()
            and _destination_key(candidate) not in reserved
        ):
            return candidate
        index += 1


def make_plan(
    source_dir: Path,
    output_dir: Path,
    db_path: Path,
    strategy: Literal["first", "combined"] = "first",
    recursive: bool = False,
    duplicate_policy: DuplicatePolicy = "skip",
    exclude_paths: Iterable[Path] = (),
) -> list[PlanItem]:
    if duplicate_policy not in {"skip", "rename", "overwrite"}:
        raise ValueError(f"지원하지 않는 중복 처리 방식: {duplicate_policy}")

    source_resolved = source_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()

    excluded = [p.expanduser().resolve() for p in exclude_paths]
    if (
        output_dir != source_resolved
        and _is_same_or_child(output_dir, source_resolved)
    ):
        excluded.append(output_dir)

    scanned = scan_items(
        source_resolved,
        recursive=recursive,
        exclude_paths=excluded,
    )
    valid_ids = [
        x.gallery_id for x in scanned if x.gallery_id is not None
    ]
    info = load_gallery_info(db_path, valid_ids)

    plan: list[PlanItem] = []
    reserved: set[str] = set()

    for item in scanned:
        if item.gallery_id is None:
            plan.append(
                PlanItem(
                    item.source, None, "", (), "", None, "번호 없음"
                )
            )
            continue

        gallery = info.get(item.gallery_id)
        db_unmatched = gallery is None
        if db_unmatched:
            title = ""
            artists: tuple[str, ...] = ()
            folder = UNKNOWN_ARTIST_FOLDER
        else:
            assert gallery is not None
            title = gallery.title
            artists = gallery.artists
            folder = choose_artist_folder(artists, strategy)

        desired = output_dir / folder / item.source.name
        desired_key = _destination_key(desired)

        def with_db_state(status: str) -> str:
            if db_unmatched:
                return f"{status} (DB 미매칭)"
            return status

        if desired_key == _destination_key(item.source):
            plan.append(
                PlanItem(
                    item.source,
                    item.gallery_id,
                    title,
                    artists,
                    folder,
                    desired,
                    with_db_state("이미 정리됨"),
                )
            )
            continue

        if desired_key in reserved:
            if duplicate_policy == "rename":
                destination = _unique_destination(desired, reserved)
                status = with_db_state("준비 (이름 변경)")
                replace_existing = False
            else:
                plan.append(
                    PlanItem(
                        item.source,
                        item.gallery_id,
                        title,
                        artists,
                        folder,
                        desired,
                        with_db_state("계획 내 대상 중복"),
                    )
                )
                continue
        elif desired.exists():
            if duplicate_policy == "skip":
                plan.append(
                    PlanItem(
                        item.source,
                        item.gallery_id,
                        title,
                        artists,
                        folder,
                        desired,
                        with_db_state("대상에 이미 존재"),
                    )
                )
                continue
            if duplicate_policy == "rename":
                destination = _unique_destination(desired, reserved)
                status = with_db_state("준비 (이름 변경)")
                replace_existing = False
            else:
                destination = desired
                status = with_db_state("준비 (덮어쓰기)")
                replace_existing = True
        else:
            destination = desired
            status = with_db_state("준비")
            replace_existing = False

        reserved.add(_destination_key(destination))
        plan.append(
            PlanItem(
                item.source,
                item.gallery_id,
                title,
                artists,
                folder,
                destination,
                status,
                replace_existing,
            )
        )

    return plan


def plan_is_ready(item: PlanItem) -> bool:
    return item.status.startswith("준비")


def plan_stats(plan: Iterable[PlanItem]) -> dict[str, int]:
    entries = list(plan)
    db_unmatched = [
        x for x in entries if "DB 미매칭" in x.status
    ]
    db_matched = [
        x
        for x in entries
        if (
            x.gallery_id is not None
            and bool(x.artist_folder)
            and "DB 미매칭" not in x.status
        )
    ]
    unknown_artist = [
        x for x in db_matched if x.artist_folder == UNKNOWN_ARTIST_FOLDER
    ]
    artists = {
        x.artist_folder
        for x in db_matched
        if x.artist_folder and x.artist_folder != UNKNOWN_ARTIST_FOLDER
    }
    return {
        "total": len(entries),
        "artists": len(artists),
        "matched": len(db_matched),
        "unknown_artist": len(unknown_artist),
        "db_unmatched": len(db_unmatched),
        "no_id": sum(x.status == "번호 없음" for x in entries),
        "ready": sum(plan_is_ready(x) for x in entries),
    }


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def clear_undo_state(log_path: Path, backup_root: Path) -> None:
    log_path.unlink(missing_ok=True)
    if backup_root.exists():
        shutil.rmtree(backup_root, ignore_errors=True)


def execute_plan(
    plan: Iterable[PlanItem],
    mode: Literal["move", "copy"],
    undo_log_path: Path | None = None,
    undo_backup_root: Path | None = None,
) -> ExecutionResult:
    if mode not in {"move", "copy"}:
        raise ValueError(f"지원하지 않는 실행 방식: {mode}")

    completed = 0
    skipped = 0
    errors: list[str] = []
    move_records: list[dict[str, str | None]] = []

    if mode == "move" and undo_log_path and undo_backup_root:
        session_dir = undo_backup_root / uuid.uuid4().hex
    else:
        session_dir = None

    for item in plan:
        if not plan_is_ready(item) or item.destination is None:
            skipped += 1
            continue

        source = item.source
        destination = item.destination
        backup_path: Path | None = None

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)

            if destination.exists():
                if not item.replace_existing:
                    skipped += 1
                    continue

                if mode == "move" and session_dir is not None:
                    session_dir.mkdir(parents=True, exist_ok=True)
                    backup_path = (
                        session_dir
                        / f"{len(move_records):04d}_{destination.name}"
                    )
                    shutil.move(str(destination), str(backup_path))
                else:
                    _remove_path(destination)

            if mode == "move":
                shutil.move(str(source), str(destination))
                move_records.append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "replaced_backup": (
                            str(backup_path) if backup_path else None
                        ),
                    }
                )
            else:
                _copy_path(source, destination)

            completed += 1
        except Exception as exc:
            if (
                backup_path is not None
                and backup_path.exists()
                and not destination.exists()
            ):
                try:
                    shutil.move(str(backup_path), str(destination))
                except Exception:
                    pass
            errors.append(f"{source.name}: {exc}")

    if (
        mode == "move"
        and move_records
        and undo_log_path is not None
    ):
        if undo_backup_root is not None and undo_backup_root.exists():
            keep = (
                session_dir.resolve()
                if session_dir is not None and session_dir.exists()
                else None
            )
            for child in undo_backup_root.iterdir():
                if keep is not None and child.resolve() == keep:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)

        undo_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "mode": "move", "moves": move_records}
        temp = undo_log_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, undo_log_path)

    return ExecutionResult(completed, skipped, errors)


def undo_last_move(
    log_path: Path,
    backup_root: Path,
) -> ExecutionResult:
    if not log_path.is_file():
        raise FileNotFoundError("되돌릴 마지막 이동 작업이 없습니다.")

    payload = json.loads(log_path.read_text(encoding="utf-8"))
    moves = payload.get("moves")
    if not isinstance(moves, list) or not moves:
        raise ValueError("되돌리기 로그가 비어 있거나 손상되었습니다.")

    completed = 0
    skipped = 0
    errors: list[str] = []
    failed_records: list[dict] = []

    for record in reversed(moves):
        source = Path(record["source"])
        destination = Path(record["destination"])
        backup_raw = record.get("replaced_backup")
        backup = Path(backup_raw) if backup_raw else None

        try:
            if source.exists():
                raise FileExistsError(
                    f"원래 위치에 같은 이름이 이미 있습니다: {source}"
                )
            if not destination.exists():
                raise FileNotFoundError(
                    f"이동된 항목을 찾을 수 없습니다: {destination}"
                )

            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))

            if backup is not None and backup.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(destination))

            completed += 1
        except Exception as exc:
            errors.append(f"{destination.name}: {exc}")
            failed_records.append(record)

    if failed_records:
        remaining = {
            "version": 1,
            "mode": "move",
            "moves": list(reversed(failed_records)),
        }
        log_path.write_text(
            json.dumps(remaining, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        clear_undo_state(log_path, backup_root)

    return ExecutionResult(completed, skipped, errors)


def normalize_db_download_url(value: str) -> str:
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("Pi DB URL은 http:// 또는 https:// 로 시작해야 합니다.")

    parsed = urlparse(value)
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        path = "/rawdata"
    normalized = parsed._replace(path=path, params="", query="", fragment="")
    return urlunparse(normalized)


def syncversion_url(value: str) -> str:
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("Pi DB URL은 http:// 또는 https:// 로 시작해야 합니다.")
    parsed = urlparse(value)
    return urlunparse(
        parsed._replace(
            path="/syncversion.txt",
            params="",
            query="",
            fragment="",
        )
    )


def parse_syncversion(text: str, base_url: str) -> DbServerInfo:
    parts = text.strip().split()
    if len(parts) < 3 or parts[0].casefold() != "db":
        raise ValueError("syncversion.txt 형식을 인식할 수 없습니다.")
    try:
        version = int(parts[1])
    except ValueError as exc:
        raise ValueError("DB 버전 값이 올바르지 않습니다.") from exc
    download_url = urljoin(base_url, parts[2])
    return DbServerInfo(version, download_url)


def fetch_server_db_info(
    server_url: str,
    timeout: int = 10,
) -> DbServerInfo:
    url = syncversion_url(server_url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ArtistSorter/0.2"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read(4096).decode("utf-8", errors="replace")
    return parse_syncversion(text, url)


def check_db_update(
    server_url: str,
    db_path: Path,
    known_version: int | None = None,
    timeout: int = 10,
) -> tuple[bool, DbServerInfo]:
    info = fetch_server_db_info(server_url, timeout=timeout)
    if known_version is not None:
        return info.version > known_version, info
    if not db_path.exists():
        return True, info
    return info.version > int(db_path.stat().st_mtime), info


def download_database(
    url: str,
    destination: Path,
    timeout: int = 60,
) -> Path:
    url = normalize_db_download_url(url)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ArtistSorter/0.2"},
    )
    fd, temp_name = tempfile.mkstemp(
        prefix="artist-sorter-",
        suffix=".db",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        remote_mtime: float | None = None
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            temp_path.open("wb") as out,
        ):
            last_modified = response.headers.get("Last-Modified")
            if last_modified:
                try:
                    remote_mtime = parsedate_to_datetime(
                        last_modified
                    ).timestamp()
                except (TypeError, ValueError, OverflowError):
                    remote_mtime = None
            shutil.copyfileobj(response, out, length=1024 * 1024)

        try:
            validate_violet_db(temp_path)
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                "DB 파일이 아닌 응답을 받았습니다. "
                "Pi 서버 주소를 확인해주세요."
            ) from exc

        os.replace(temp_path, destination)
        if remote_mtime is not None:
            os.utime(destination, (remote_mtime, remote_mtime))
        return destination
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
