from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from smartgpms.database import Database
from smartgpms.photo_pipeline import PhotoPipeline
from smartgpms.recognition import OCRItem


class FakeDatabase:
    def __init__(self):
        self.saved = []
        self.cached = []
        self.archive_status = None
        self.inventory = None

    def update_ocr_status(self, *_args):
        return None

    def save_photo(self, cpm_id, photo):
        existing = next(
            (
                row
                for row in [*self.saved, *self.cached]
                if row.get("source_url") == photo.get("source_url")
            ),
            None,
        )
        row = {
            **(existing or {}),
            **photo,
            "id": (existing or {}).get("id", len(self.saved) + len(self.cached) + 1),
            "cpm_id": cpm_id,
        }
        self.saved = [
            saved
            for saved in self.saved
            if saved.get("source_url") != photo.get("source_url")
        ]
        self.saved.append(row)
        return row

    def photos_for_cpm(self, _cpm_id):
        merged = {row.get("source_url"): row for row in self.cached}
        merged.update({row.get("source_url"): row for row in self.saved})
        return list(merged.values())

    def mark_archive_status(self, _cpm_id, status):
        self.archive_status = status

    def update_photo_inventory(self, _cpm_id, available_count, downloaded_count):
        self.inventory = (available_count, downloaded_count)

    def clear_ocr(self, _cpm_id):
        return None


class FakeBrowser:
    def __init__(self):
        self.targets = []

    def download(self, _url, target):
        self.targets.append(target)
        Image.new("RGB", (16, 16), "white").save(target, "JPEG")
        return {"sha256": "hash"}


class EmptyEngine:
    def recognize(self, _path):
        return []


def test_storage_period_uses_das_begin_date():
    assert PhotoPipeline._storage_period("2026-09-23 08:13:34") == ("2026", "09")
    assert PhotoPipeline._storage_period("20260923") == ("2026", "09")
    assert PhotoPipeline._storage_period("") == ("unknown",)


def test_original_filename_decodes_encoded_das_path():
    url = (
        "http://10.210.34.100:7070/"
        "cpmupload%2F2019_CAAU6675248%2FS1_202310301045377934_1698633935607298.jpeg"
    )
    assert (
        PhotoPipeline._original_filename(url)
        == "S1_202310301045377934_1698633935607298.jpeg"
    )
    assert PhotoPipeline._original_filename(url, "F000123") == "F000123.jpeg"


def test_original_filename_is_not_silently_modified():
    with pytest.raises(ValueError, match="跨平台安全保存"):
        PhotoPipeline._original_filename("http://das.example/photo/bad%3Fname.jpg")


def test_orientation_candidates_include_upright_180_degree_variant(tmp_path):
    source = tmp_path / "source.jpg"
    work = tmp_path / "work"
    work.mkdir()
    Image.new("RGB", (10, 20), "white").save(source, "JPEG")
    pipeline = PhotoPipeline.__new__(PhotoPipeline)
    pipeline.engine = EmptyEngine()

    variants = [
        (name, angle, image.size)
        for name, angle, image, _path, _items in pipeline._orientations(source, work)
    ]

    assert variants == [
        ("original", 0, (10, 20)),
        ("cw90", 270, (20, 10)),
        ("ccw90", 90, (20, 10)),
        ("rotate180", 180, (10, 20)),
    ]


def test_process_saves_only_stage4_originals_in_month_folder(tmp_path):
    database = FakeDatabase()
    browser = FakeBrowser()
    pipeline = PhotoPipeline.__new__(PhotoPipeline)
    pipeline.database = database
    pipeline.browser = browser
    pipeline.cache_dir = tmp_path
    pipeline.engine = EmptyEngine()

    progress = []
    pipeline.process(
        "100715",
        {
            "container_no": "CAAU5328959",
            "begin_date": "2026-09-23 08:13:34",
            "photos": [
                {
                    "step_no": 2,
                    "step_code": "U2",
                    "source_url": "http://das/photo/U2_original.jpeg",
                },
                {
                    "step_no": 4,
                    "step_code": "S1",
                    "label": "F000321",
                    "source_url": "http://das/photo/S1_original.jpeg",
                },
            ],
        },
        lambda stage, label: progress.append((stage, label)),
    )

    root = tmp_path / "2026" / "09" / "100715_CAAU5328959"
    assert browser.targets == [root / "F000321.jpeg"]
    assert (root / "F000321.jpeg").is_file()
    assert (root / "_thumb" / "F000321_thumb.jpg").is_file()
    assert database.saved[-1]["thumbnail_path"] == str(
        root / "_thumb" / "F000321_thumb.jpg"
    )
    assert (root / "_ocr").is_dir()
    assert not list((root / "_ocr").iterdir())
    assert database.archive_status == 4
    assert database.inventory == (1, 1)
    assert progress == [
        ("download", "下载四阶段照片 1/1"),
        ("ocr", "RapidOCR 识别照片 1/1"),
    ]


def test_process_reuses_ready_original_without_downloading_again(tmp_path):
    database = FakeDatabase()
    browser = FakeBrowser()
    cached = tmp_path / "cached.jpeg"
    Image.new("RGB", (16, 16), "white").save(cached, "JPEG")
    database.cached = [
        {
            "id": 7,
            "cpm_id": "100715",
            "source_url": "http://das/photo/S1_original.jpeg",
            "local_path": str(cached),
            "source_hash": "cached-hash",
            "cache_status": "ready",
            "step_no": 4,
        }
    ]
    pipeline = PhotoPipeline.__new__(PhotoPipeline)
    pipeline.database = database
    pipeline.browser = browser
    pipeline.cache_dir = tmp_path
    pipeline.engine = EmptyEngine()

    pipeline.process(
        "100715",
        {
            "container_no": "CAAU5328959",
            "begin_date": "2026-09-23 08:13:34",
            "photos": [
                {
                    "step_no": 4,
                    "step_code": "S1",
                    "source_url": "http://das/photo/S1_original.jpeg",
                }
            ],
        },
    )

    assert browser.targets == []


def test_process_renames_cached_url_filename_to_das_photo_label(tmp_path):
    database = FakeDatabase()
    browser = FakeBrowser()
    cached = tmp_path / "S1_original.jpeg"
    Image.new("RGB", (16, 16), "white").save(cached, "JPEG")
    database.cached = [
        {
            "id": 7,
            "cpm_id": "100715",
            "source_url": "http://das/photo/S1_original.jpeg",
            "local_path": str(cached),
            "source_hash": "cached-hash",
            "cache_status": "ready",
            "step_no": 4,
        }
    ]
    pipeline = PhotoPipeline.__new__(PhotoPipeline)
    pipeline.database = database
    pipeline.browser = browser
    pipeline.cache_dir = tmp_path
    pipeline.engine = EmptyEngine()

    pipeline.process(
        "100715",
        {
            "container_no": "CAAU5328959",
            "photos": [
                {
                    "step_no": 4,
                    "step_code": "S1",
                    "label": "F000321",
                    "source_url": "http://das/photo/S1_original.jpeg",
                }
            ],
        },
    )

    renamed = tmp_path / "unknown" / "100715_CAAU5328959" / "F000321.jpeg"
    assert browser.targets == []
    assert renamed.is_file()
    assert not cached.exists()
    assert database.saved[-1]["local_path"] == str(renamed)


def test_container_source_photo_is_never_reused_as_seal_evidence(tmp_path):
    database = Database(tmp_path / "data.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "100526",
            "container_no": "CAJU6050344",
            "business_stage": 4,
            "das_process_status": 4,
        }
    )
    browser = FakeBrowser()

    class SameSourcePipeline(PhotoPipeline):
        def _orientations(self, source, _work_dir):
            image = Image.new("RGB", (400, 160), "white")
            container = OCRItem(
                "CAJU6050344",
                0.99,
                [[20, 40], [280, 40], [280, 90], [20, 90]],
            )
            reversed_text = OCRItem(
                "605034CAJU",
                0.96,
                [[20, 40], [280, 40], [280, 90], [20, 90]],
            )
            yield "original", 0, image.copy(), source, [container]
            yield "rotate180", 180, image.copy(), source, [reversed_text]

    pipeline = SameSourcePipeline(database, browser, tmp_path / "cache")
    result = pipeline.process(
        "100526",
        {
            "container_no": "CAJU6050344",
            "business_stage": 4,
            "photos": [
                {
                    "step_no": 4,
                    "step_code": "S1",
                    "source_url": "http://das/photo/half_closed.jpeg",
                }
            ],
        },
    )

    assert result["container"]["observed"] == "CAJU6050344"
    assert result["seal"] is None
    assert result["downloaded_photo_count"] == 1
    assert database.best_ocr("100526", "seal") is None
    stored_photo = database.photos_for_cpm("100526")[0]
    assert stored_photo["thumbnail_path"]
    assert Path(stored_photo["thumbnail_path"]).is_file()
    record = database.cpm_by_id("100526")
    assert record["photo_count"] == 1
    assert record["downloaded_photo_count"] == 1
    assert record["archive_status"] == 4


def test_cleanup_marks_expired_originals_and_crops_as_cleaned(tmp_path):
    database = Database(tmp_path / "data.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "100715",
            "container_no": "CAAU5328959",
            "begin_date": "2026-07-01 08:00:00",
            "business_stage": 4,
            "archive_status": 5,
        }
    )
    root = tmp_path / "cache" / "2026" / "07" / "100715_CAAU5328959"
    root.mkdir(parents=True)
    original = root / "S1_original.jpeg"
    original.write_bytes(b"original")
    thumbnail = root / "_thumb" / "S1_original_thumb.jpg"
    thumbnail.parent.mkdir()
    thumbnail.write_bytes(b"thumbnail")
    photo = database.save_photo(
        "100715",
        {
            "step_code": "S1",
            "step_no": 4,
            "source_url": "http://das/photo/S1_original.jpeg",
            "local_path": str(original),
            "thumbnail_path": str(thumbnail),
            "source_hash": "hash",
            "downloaded_at": "2026-07-01T08:00:00+08:00",
            "cache_status": "ready",
        },
    )
    crop = root / "_ocr" / "S1_original_container_crop.jpg"
    crop.parent.mkdir()
    crop.write_bytes(b"crop")
    database.save_ocr(
        "100715",
        int(photo["id"]),
        "container",
        {"observed": "CAAU5328959", "crop_path": str(crop), "source_hash": "hash"},
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE ocr_cache SET created_at='2026-07-01T08:00:00+08:00'"
        )
    temporary = crop.parent / "S1_original_1_temp"
    temporary.mkdir()
    (temporary / "original.png").write_bytes(b"temporary")

    pipeline = PhotoPipeline.__new__(PhotoPipeline)
    pipeline.database = database
    pipeline.cache_dir = tmp_path / "cache"
    pipeline.evidence_dir = tmp_path / "evidence"
    result = pipeline.cleanup_cache(
        current=datetime.fromisoformat("2026-09-23T12:00:00+08:00"),
        cache_max_bytes=10**12,
        min_free_bytes=0,
    )

    assert result["photos"] == 1
    assert result["crops"] == 1
    assert result["intermediate_dirs"] == 1
    assert not original.exists()
    assert not thumbnail.exists()
    assert not crop.exists()
    assert database.photos_for_cpm("100715")[0]["cache_status"] == "cleaned"
    assert database.best_ocr("100715", "container") is None
    assert database.cpm_by_id("100715")["ocr_status"] == "cleaned"


def test_cleanup_removes_print_history_and_evidence_after_one_year(tmp_path):
    database = Database(tmp_path / "data.sqlite3")
    database.upsert_cpm(
        {"cpm_id": "100715", "container_no": "CAAU5328959", "business_stage": 4}
    )
    evidence = tmp_path / "evidence" / "100715" / "old-print"
    evidence.mkdir(parents=True)
    (evidence / "snapshot.json").write_bytes(b"evidence")
    database.record_print(
        "100715", {"verdict": "match", "evidence_dir": str(evidence)}
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE print_history SET printed_at='2025-01-01T08:00:00+08:00'"
        )

    pipeline = PhotoPipeline.__new__(PhotoPipeline)
    pipeline.database = database
    pipeline.cache_dir = tmp_path / "cache"
    pipeline.evidence_dir = tmp_path / "evidence"
    result = pipeline.cleanup_cache(
        current=datetime.fromisoformat("2026-09-23T12:00:00+08:00"),
        cache_max_bytes=10**12,
        min_free_bytes=0,
    )

    assert result["print_records"] == 1
    assert not evidence.exists()
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS value FROM print_history"
        ).fetchone()["value"]
    assert count == 0
