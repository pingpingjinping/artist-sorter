import sqlite3
import tempfile
import unittest
from pathlib import Path

from artist_sorter.core import (
    choose_artist_folder,
    extract_gallery_id,
    load_gallery_info,
    make_plan,
    parse_artists,
    sanitize_windows_name,
)


class CoreTests(unittest.TestCase):
    def test_extract_gallery_id(self):
        self.assertEqual(extract_gallery_id("4192094"), 4192094)
        self.assertEqual(extract_gallery_id("[4192094] sample title"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.zip"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.rar"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.7z"), 4192094)
        self.assertEqual(extract_gallery_id("[4192094] sample title.cbz"), 4192094)
        self.assertEqual(extract_gallery_id("4192094.cbr"), 4192094)
        self.assertIsNone(extract_gallery_id("sample title"))

    def test_parse_artists(self):
        self.assertEqual(parse_artists("|alice|bob|"), ("alice", "bob"))
        self.assertEqual(parse_artists("|N/A|"), ())
        self.assertEqual(parse_artists(None), ())

    def test_windows_name(self):
        self.assertEqual(sanitize_windows_name('a:b*c?'), "a_b_c_")
        self.assertEqual(sanitize_windows_name("CON"), "_CON")

    def test_artist_strategy(self):
        artists = ("alice", "bob")
        self.assertEqual(choose_artist_folder(artists, "first"), "alice")
        self.assertEqual(choose_artist_folder(artists, "combined"), "alice + bob")

    def test_db_lookup_and_plan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE HitomiColumnModel "
                "(Id INTEGER PRIMARY KEY, Title TEXT, Artists TEXT)"
            )
            db.execute(
                "INSERT INTO HitomiColumnModel VALUES "
                "(1234567, 'Test Work', '|alice|bob|')"
            )
            db.commit()
            db.close()

            source = root / "downloads"
            source.mkdir()
            (source / "1234567").mkdir()
            output = root / "sorted"

            infos = load_gallery_info(db_path, [1234567])
            self.assertEqual(infos[1234567].artists, ("alice", "bob"))

            plan = make_plan(source, output, db_path, "first")
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0].artist_folder, "alice")
            self.assertEqual(
                plan[0].destination,
                (output / "alice" / "1234567").resolve(),
            )
            self.assertEqual(plan[0].status, "준비")

    def test_archive_files_are_sorted_without_extracting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "data.db"
            db = sqlite3.connect(db_path)
            db.execute(
                "CREATE TABLE HitomiColumnModel "
                "(Id INTEGER PRIMARY KEY, Title TEXT, Artists TEXT)"
            )
            db.execute(
                "INSERT INTO HitomiColumnModel VALUES "
                "(4192094, 'Archive Work', '|archive artist|')"
            )
            db.commit()
            db.close()

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
            self.assertTrue(all(item.gallery_id == 4192094 for item in plan))
            self.assertTrue(
                all(item.artist_folder == "archive artist" for item in plan)
            )
            self.assertEqual(
                {item.destination.name for item in plan if item.destination},
                set(archive_names),
            )
            self.assertTrue(all(item.status == "준비" for item in plan))


if __name__ == "__main__":
    unittest.main()
