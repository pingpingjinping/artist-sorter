import sqlite3
import tempfile
import unittest
from pathlib import Path

from artist_sorter.core import (
    UNKNOWN_ARTIST_FOLDER,
    choose_artist_folder,
    execute_plan,
    extract_gallery_id,
    load_gallery_info,
    make_plan,
    normalize_db_download_url,
    parse_artists,
    parse_syncversion,
    plan_stats,
    sanitize_windows_name,
    syncversion_url,
    undo_last_move,
)


def make_db(path: Path, rows: list[tuple[int, str, str | None]]) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE HitomiColumnModel "
        "(Id INTEGER PRIMARY KEY, Title TEXT, Artists TEXT)"
    )
    db.executemany(
        "INSERT INTO HitomiColumnModel VALUES (?, ?, ?)",
        rows,
    )
    db.commit()
    db.close()


def make_db_with_groups(
    path: Path,
    rows: list[tuple[int, str, str | None, str | None]],
    column: str = "Groups",
) -> None:
    db = sqlite3.connect(path)
    db.execute(
        f"CREATE TABLE HitomiColumnModel "
        f"(Id INTEGER PRIMARY KEY, Title TEXT, Artists TEXT, "
        f'"{column}" TEXT)'
    )
    db.executemany(
        f'INSERT INTO HitomiColumnModel '
        f'(Id, Title, Artists, "{column}") VALUES (?, ?, ?, ?)',
        rows,
    )
    db.commit()
    db.close()


class CoreTests(unittest.TestCase):
    def test_extract_gallery_id(self):
        self.assertEqual(extract_gallery_id("4192094"), 4192094)
        self.assertEqual(extract_gallery_id("[4192094] sample title"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.zip"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.rar"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.7z"), 4192094)
        self.assertEqual(
            extract_gallery_id("[4192094] sample title.cbz"),
            4192094,
        )
        self.assertEqual(extract_gallery_id("4192094.cbr"), 4192094)
        self.assertIsNone(extract_gallery_id("sample title"))

    def test_parse_artists_and_unknown_folder(self):
        self.assertEqual(parse_artists("|alice|bob|"), ("alice", "bob"))
        self.assertEqual(parse_artists("|N/A|"), ())
        self.assertEqual(parse_artists(None), ())
        self.assertEqual(
            choose_artist_folder((), "first"),
            UNKNOWN_ARTIST_FOLDER,
        )
        self.assertEqual(UNKNOWN_ARTIST_FOLDER, "기타")

    def test_windows_name(self):
        self.assertEqual(sanitize_windows_name('a:b*c?'), "a_b_c_")
        self.assertEqual(sanitize_windows_name("CON"), "_CON")

    def test_artist_strategy(self):
        artists = ("alice", "bob")
        self.assertEqual(choose_artist_folder(artists, "first"), "alice")
        self.assertEqual(
            choose_artist_folder(artists, "combined"),
            "alice + bob",
        )

    def test_db_lookup_and_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(
                db_path,
                [(1234567, "Test Work", "|alice|bob|")],
            )

            source = root / "downloads"
            source.mkdir()
            (source / "1234567").mkdir()
            output = root / "sorted"

            infos = load_gallery_info(db_path, [1234567])
            self.assertEqual(infos[1234567].artists, ("alice", "bob"))

            plan = make_plan(source, output, db_path, "first")
            self.assertEqual(len(plan), 1)
            self.assertEqual(
                plan[0].artist_folder,
                str(Path("artist") / "alice"),
            )
            self.assertEqual(
                plan[0].destination,
                (output / "artist" / "alice" / "1234567").resolve(),
            )
            self.assertEqual(plan[0].status, "준비")

    def test_add_title_to_filename_for_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(
                db_path,
                [
                    (
                        4192094,
                        'Sample: Title / Test?',
                        "|alice|",
                    ),
                    (
                        4192095,
                        "Folder Title",
                        "|alice|",
                    ),
                ],
            )

            source = root / "downloads"
            source.mkdir()
            archive = source / "4192094.zip"
            archive.write_bytes(b"archive")
            folder = source / "4192095"
            folder.mkdir()
            output = root / "sorted"

            plan = make_plan(
                source,
                output,
                db_path,
                add_title_to_filename=True,
            )
            by_id = {item.gallery_id: item for item in plan}

            self.assertEqual(
                by_id[4192094].destination.name,
                "4192094 (Sample_ Title _ Test_).zip",
            )
            self.assertEqual(
                by_id[4192095].destination.name,
                "4192095",
            )

    def test_title_rename_can_reprocess_existing_sorted_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(
                db_path,
                [(7777777, "Existing Work", "|alice|")],
            )

            existing = (
                root
                / "artist"
                / "alice"
                / "7777777.zip"
            )
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"x")

            plan = make_plan(
                root,
                root,
                db_path,
                recursive=True,
                add_title_to_filename=True,
            )
            item = next(
                x for x in plan if x.gallery_id == 7777777
            )
            self.assertEqual(
                item.destination,
                (
                    root
                    / "artist"
                    / "alice"
                    / "7777777 (Existing Work).zip"
                ).resolve(),
            )
            self.assertEqual(item.status, "준비")

    def test_archive_files_are_sorted_without_extracting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(
                db_path,
                [(4192094, "Archive Work", "|archive artist|")],
            )

            source = root / "downloads"
            source.mkdir()
            archive_names = (
                "4192094.zip",
                "4192094.rar",
                "4192094.7z",
                "[4192094] Archive Work.cbz",
                "4192094.cbr",
            )
            for name in archive_names:
                (source / name).write_bytes(b"test")

            output = root / "sorted"
            plan = make_plan(source, output, db_path, "first")

            self.assertEqual(len(plan), len(archive_names))
            self.assertTrue(
                all(item.gallery_id == 4192094 for item in plan)
            )
            self.assertTrue(
                all(
                    item.artist_folder == str(Path("artist") / "archive artist")
                    for item in plan
                )
            )
            self.assertEqual(
                {
                    item.destination.name
                    for item in plan
                    if item.destination
                },
                set(archive_names),
            )
            self.assertTrue(
                all(item.status == "준비" for item in plan)
            )

    def test_recursive_scan_stops_at_gallery_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(
                db_path,
                [
                    (1111111, "Folder Work", "|alice|"),
                    (2222222, "Archive Work", "|bob|"),
                ],
            )
            source = root / "downloads"
            nested = source / "bucket"
            gallery = nested / "1111111"
            gallery.mkdir(parents=True)
            (gallery / "page-2222222.jpg").write_bytes(b"image")
            archive = nested / "more" / "2222222.zip"
            archive.parent.mkdir()
            archive.write_bytes(b"archive")

            output = root / "sorted"
            plan = make_plan(
                source,
                output,
                db_path,
                recursive=True,
            )
            names = {item.source.name for item in plan}
            self.assertEqual(names, {"1111111", "2222222.zip"})

    def test_group_only_goes_under_group_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db_with_groups(
                db_path,
                [
                    (
                        3333001,
                        "Group Work",
                        "|N/A|",
                        "|circle a|circle b|",
                    )
                ],
            )
            source = root / "downloads"
            source.mkdir()
            (source / "3333001.zip").write_bytes(b"x")
            output = root / "sorted"

            infos = load_gallery_info(db_path, [3333001])
            self.assertEqual(
                infos[3333001].groups,
                ("circle a", "circle b"),
            )

            plan = make_plan(source, output, db_path)
            self.assertEqual(plan[0].artists, ())
            self.assertEqual(
                plan[0].groups,
                ("circle a", "circle b"),
            )
            self.assertEqual(
                plan[0].artist_folder,
                str(Path("group") / "circle a"),
            )
            self.assertEqual(
                plan[0].destination,
                (
                    output
                    / "group"
                    / "circle a"
                    / "3333001.zip"
                ).resolve(),
            )

            stats = plan_stats(plan)
            self.assertEqual(stats["artists"], 0)
            self.assertEqual(stats["groups"], 1)
            self.assertEqual(stats["unknown_artist"], 0)

    def test_artist_takes_priority_over_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db_with_groups(
                db_path,
                [
                    (
                        3333002,
                        "Artist Work",
                        "|alice|",
                        "|circle a|",
                    )
                ],
                column="Group",
            )
            source = root / "downloads"
            source.mkdir()
            (source / "3333002.zip").write_bytes(b"x")
            output = root / "sorted"

            plan = make_plan(source, output, db_path)
            self.assertEqual(
                plan[0].artist_folder,
                str(Path("artist") / "alice"),
            )
            self.assertEqual(
                plan[0].destination,
                (
                    output
                    / "artist"
                    / "alice"
                    / "3333002.zip"
                ).resolve(),
            )
            self.assertEqual(plan[0].groups, ("circle a",))

    def test_unknown_artist_goes_to_n_a_equivalent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(db_path, [(3333333, "Unknown", "|N/A|")])
            source = root / "downloads"
            source.mkdir()
            (source / "3333333.zip").write_bytes(b"x")
            output = root / "sorted"

            plan = make_plan(source, output, db_path)
            self.assertEqual(plan[0].artist_folder, "기타")
            self.assertEqual(
                plan[0].destination.parent,
                (output / "기타").resolve(),
            )

    def test_db_unmatched_goes_to_n_a_and_is_executable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(db_path, [])

            source = root / "downloads"
            source.mkdir()
            incoming = source / "8888888.zip"
            incoming.write_bytes(b"unmatched")
            output = root / "sorted"

            plan = make_plan(source, output, db_path)
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0].artist_folder, "기타")
            self.assertEqual(
                plan[0].destination,
                (output / "기타" / "8888888.zip").resolve(),
            )
            self.assertTrue(plan[0].status.startswith("준비"))
            self.assertIn("DB 미매칭", plan[0].status)

            stats = plan_stats(plan)
            self.assertEqual(stats["matched"], 0)
            self.assertEqual(stats["unknown_artist"], 0)
            self.assertEqual(stats["db_unmatched"], 1)
            self.assertEqual(stats["ready"], 1)

            result = execute_plan(plan, "move")
            self.assertEqual(result.completed, 1)
            self.assertFalse(incoming.exists())
            self.assertTrue(
                (output / "기타" / "8888888.zip").exists()
            )

    def test_duplicate_policies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(db_path, [(4444444, "Dup", "|alice|")])
            source = root / "downloads"
            source.mkdir()
            incoming = source / "4444444.zip"
            incoming.write_bytes(b"new")
            output = root / "sorted"
            existing = output / "artist" / "alice" / "4444444.zip"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"old")

            skipped = make_plan(
                source,
                output,
                db_path,
                duplicate_policy="skip",
            )[0]
            self.assertEqual(skipped.status, "대상에 이미 존재")

            renamed = make_plan(
                source,
                output,
                db_path,
                duplicate_policy="rename",
            )[0]
            self.assertEqual(renamed.status, "준비 (이름 변경)")
            self.assertEqual(
                renamed.destination.name,
                "4444444 (1).zip",
            )

            overwrite = make_plan(
                source,
                output,
                db_path,
                duplicate_policy="overwrite",
            )[0]
            self.assertEqual(overwrite.status, "준비 (덮어쓰기)")
            self.assertTrue(overwrite.replace_existing)

    def test_move_undo_restores_overwritten_destination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(db_path, [(5555555, "Undo", "|alice|")])
            source = root / "downloads"
            source.mkdir()
            incoming = source / "5555555.zip"
            incoming.write_bytes(b"new")
            output = root / "sorted"
            existing = output / "artist" / "alice" / "5555555.zip"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"old")

            plan = make_plan(
                source,
                output,
                db_path,
                duplicate_policy="overwrite",
            )
            log_path = root / "last-operation.json"
            backup_root = root / "undo-backup"

            result = execute_plan(
                plan,
                "move",
                undo_log_path=log_path,
                undo_backup_root=backup_root,
            )
            self.assertEqual(result.completed, 1)
            self.assertFalse(incoming.exists())
            self.assertEqual(existing.read_bytes(), b"new")
            self.assertTrue(log_path.exists())

            undo = undo_last_move(log_path, backup_root)
            self.assertEqual(undo.completed, 1)
            self.assertEqual(incoming.read_bytes(), b"new")
            self.assertEqual(existing.read_bytes(), b"old")
            self.assertFalse(log_path.exists())

    def test_plan_stats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            make_db(
                db_path,
                [
                    (6000001, "A", "|alice|"),
                    (6000002, "B", "|alice|"),
                    (6000003, "C", "|N/A|"),
                ],
            )
            source = root / "downloads"
            source.mkdir()
            for gallery_id in (
                6000001,
                6000002,
                6000003,
                6999999,
            ):
                (source / f"{gallery_id}.zip").write_bytes(b"x")
            (source / "no-number.zip").write_bytes(b"x")
            output = root / "sorted"

            plan = make_plan(source, output, db_path)
            stats = plan_stats(plan)
            self.assertEqual(stats["artists"], 1)
            self.assertEqual(stats["matched"], 3)
            self.assertEqual(stats["unknown_artist"], 1)
            self.assertEqual(stats["db_unmatched"], 1)
            self.assertEqual(stats["no_id"], 1)

    def test_portable_app_files_are_excluded_from_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "rawdata-korean.db"
            make_db(
                db_path,
                [
                    (7000001, "Real Work", "|alice|"),
                    (7000002, "Undo Work", "|bob|"),
                ],
            )

            real = root / "7000001.zip"
            real.write_bytes(b"real")
            exe = root / "ArtistSorter.exe"
            exe.write_bytes(b"exe")
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            log = root / "last-operation.json"
            log.write_text("{}", encoding="utf-8")
            backup = root / "undo-backup"
            backup.mkdir()
            (backup / "7000002.zip").write_bytes(b"backup")
            output = root / "sorted"

            excluded = [exe, config, db_path, log, backup]

            flat = make_plan(
                root,
                output,
                db_path,
                exclude_paths=excluded,
            )
            self.assertEqual(
                [item.source.name for item in flat],
                ["7000001.zip"],
            )

            recursive = make_plan(
                root,
                output,
                db_path,
                recursive=True,
                exclude_paths=excluded,
            )
            self.assertEqual(
                [item.source.name for item in recursive],
                ["7000001.zip"],
            )

    def test_server_url_helpers(self):
        base = "http://192.168.0.39:3002"
        self.assertEqual(
            normalize_db_download_url(base),
            "http://192.168.0.39:3002/rawdata",
        )
        self.assertEqual(
            normalize_db_download_url(base + "/rawdata"),
            base + "/rawdata",
        )
        self.assertEqual(
            syncversion_url(base + "/rawdata"),
            base + "/syncversion.txt",
        )
        info = parse_syncversion(
            "db 123456 http://192.168.0.39:3002/rawdata\n",
            base + "/syncversion.txt",
        )
        self.assertEqual(info.version, 123456)
        self.assertEqual(info.download_url, base + "/rawdata")


if __name__ == "__main__":
    unittest.main()
