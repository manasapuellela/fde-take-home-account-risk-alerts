# Tests for app.storage: open_uri dispatch for file:// and gs:// (mocked)
import pytest

from app import storage


def test_open_uri_file_reads_real_parquet(sample_parquet_path):
    dataset = storage.open_uri(sample_parquet_path.as_uri())
    table = dataset.to_table(columns=["account_id"])
    assert table.num_rows == 10


def test_open_uri_gs_dispatches_to_gcs_filesystem_and_pushes_down_path(monkeypatch):
    # No live GCS access in this environment: verify scheme dispatch and the
    # bucket/path parsed into pyarrow.dataset.dataset() without a network call.
    captured = {}

    class FakeGcsFileSystem:
        def __init__(self):
            captured["constructed"] = True

    def fake_dataset(path, filesystem=None, format=None):
        captured["path"] = path
        captured["filesystem"] = filesystem
        captured["format"] = format
        return "FAKE_DATASET"

    monkeypatch.setattr(storage.pafs, "GcsFileSystem", FakeGcsFileSystem)
    monkeypatch.setattr(storage.ds, "dataset", fake_dataset)

    result = storage.open_uri("gs://my-bucket/path/to/file.parquet")

    assert result == "FAKE_DATASET"
    assert captured["path"] == "my-bucket/path/to/file.parquet"
    assert isinstance(captured["filesystem"], FakeGcsFileSystem)
    assert captured["format"] == "parquet"


def test_open_uri_s3_dispatches_to_s3_filesystem(monkeypatch):
    captured = {}

    class FakeS3FileSystem:
        def __init__(self):
            captured["constructed"] = True

    def fake_dataset(path, filesystem=None, format=None):
        captured["path"] = path
        captured["filesystem"] = filesystem
        return "FAKE_DATASET"

    monkeypatch.setattr(storage.pafs, "S3FileSystem", FakeS3FileSystem)
    monkeypatch.setattr(storage.ds, "dataset", fake_dataset)

    result = storage.open_uri("s3://my-bucket/path/to/file.parquet")

    assert result == "FAKE_DATASET"
    assert captured["path"] == "my-bucket/path/to/file.parquet"
    assert isinstance(captured["filesystem"], FakeS3FileSystem)


def test_open_uri_unsupported_scheme_raises():
    with pytest.raises(ValueError):
        storage.open_uri("ftp://example.com/file.parquet")
