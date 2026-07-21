"""Storage abstraction: open a source_uri as a filtered-scan-friendly pyarrow Dataset.

Supports file://, gs:// (required), s3:// (design supports, untested here).
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pyarrow.dataset as ds
import pyarrow.fs as pafs


def open_uri(source_uri: str) -> ds.Dataset:
    """Return a pyarrow Dataset for source_uri, without materializing any rows.

    Callers should apply column projection and filters when reading
    (e.g. dataset.to_table(columns=..., filter=...)) to minimize scanning.
    """
    parsed = urlsplit(source_uri)
    scheme = parsed.scheme

    if scheme == "file" or scheme == "":
        path = unquote(parsed.path) if parsed.path else source_uri
        # On Windows, file:///C:/... parses to a leading slash before the drive letter.
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        filesystem = pafs.LocalFileSystem()
    elif scheme == "gs":
        path = unquote(f"{parsed.netloc}{parsed.path}")
        filesystem = pafs.GcsFileSystem()
    elif scheme == "s3":
        path = unquote(f"{parsed.netloc}{parsed.path}")
        filesystem = pafs.S3FileSystem()
    else:
        raise ValueError(f"Unsupported source_uri scheme: {scheme!r} in {source_uri!r}")

    return ds.dataset(path, filesystem=filesystem, format="parquet")
