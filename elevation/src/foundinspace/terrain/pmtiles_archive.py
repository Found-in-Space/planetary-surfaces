"""Bounded PMTiles v3 IO and a resumable, disk-indexed PNG packer.

The upstream Python writer retains all entries and deduplication hashes in RAM.
Here only one leaf directory and the small root directory are materialized.
Tile payloads are written directly into the eventual archive, without a tile tree
or a second archive-sized copy. SQLite is working state, never a runtime service.
"""

import gzip
import hashlib
import json
import os
import sqlite3
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from pmtiles.reader import Reader
from pmtiles.tile import (
    Compression,
    Entry,
    TileType,
    deserialize_directory,
    serialize_directory,
    serialize_header,
)

from .acquisition import digest, write_json


@contextmanager
def local_archive(path):
    with Path(path).open("rb") as stream:

        def read(offset, length):
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise ValueError("Truncated archive range")
            return data

        yield Reader(read)


class RangeSource:
    """Strict, validator-bound ranges with portable, hashed on-disk receipts."""

    def __init__(self, url, directory, *, budget=64 * 1024**2):
        self.url = url
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.transferred = 0
        with urllib.request.urlopen(
            urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "foundinspace-terrain/0.1"}
            ),
            timeout=60,
        ) as response:
            self.identity = {
                "url": url,
                "bytes": int(response.headers["Content-Length"]),
                "etag": response.headers["ETag"],
                "lastModified": response.headers.get("Last-Modified"),
            }
        if self.identity["etag"].startswith("W/"):
            raise ValueError("A strong ETag is required for snapshot-bound ranges")
        identity_path = self.directory / "http-identity.json"
        if identity_path.exists():
            if json.loads(identity_path.read_text()) != self.identity:
                raise ValueError(
                    "Remote source changed; use a new acquisition directory"
                )
        else:
            write_json(identity_path, self.identity)

    def __call__(self, offset, length):
        if not (0 <= offset < offset + length <= self.identity["bytes"]):
            raise ValueError("Range outside source")
        path = self.directory / f"{offset}-{length}.bin"
        receipt = path.with_suffix(".json")
        if path.exists():
            recorded = json.loads(receipt.read_text())
            if path.stat().st_size != length or digest(path) != recorded["sha256"]:
                raise ValueError("Cached range changed")
            return path.read_bytes()
        if self.transferred + length > self.budget:
            raise ValueError("Inspection transfer budget exceeded")
        request = urllib.request.Request(
            self.url,
            headers={
                "Range": f"bytes={offset}-{offset + length - 1}",
                "If-Match": self.identity["etag"],
                "Accept-Encoding": "identity",
                "User-Agent": "foundinspace-terrain/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            if (
                response.status != 206
                or response.headers.get("Content-Range")
                != f"bytes {offset}-{offset + length - 1}/{self.identity['bytes']}"
                or response.headers.get("ETag") != self.identity["etag"]
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise ValueError("Server did not honor the snapshot-bound byte range")
            data = response.read(length + 1)
        if len(data) != length:
            raise ValueError("Incomplete or oversized range")
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        write_json(
            receipt,
            {
                "offset": offset,
                "bytes": length,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
        temporary.replace(path)
        self.transferred += length
        return data


def tile_entries(reader):
    """Yield directory entries without expanding runs or reading tile payloads."""
    header = reader.header()
    if header["internal_compression"] != Compression.GZIP:
        raise ValueError("This workflow requires gzip-compressed PMTiles directories")

    def walk(offset, length, depth):
        if depth > 2 or length > 16 * 1024**2:
            raise ValueError("Unexpected PMTiles directory depth or size")
        entries = deserialize_directory(reader.get_bytes(offset, length))
        for entry in entries:
            if entry.run_length:
                yield entry
            else:
                start = header["leaf_directory_offset"] + entry.offset
                end = start + entry.length
                if (
                    end
                    > header["leaf_directory_offset"] + header["leaf_directory_length"]
                ):
                    raise ValueError("Leaf directory outside its section")
                yield from walk(start, entry.length, depth + 1)

    yield from walk(header["root_offset"], header["root_length"], 0)


def check_structure(reader, size):
    """Scan every directory entry and range; never load the complete index."""
    h = reader.header()
    sections = []
    for name in ("root", "metadata", "leaf_directory", "tile_data"):
        start, length = h[f"{name}_offset"], h[f"{name}_length"]
        if length:
            if start < 127 or start + length > size:
                raise ValueError(f"Invalid {name} section")
            sections.append((start, start + length))
    sections.sort()
    if any(a[1] > b[0] for a, b in zip(sections, sections[1:], strict=False)):
        raise ValueError("Overlapping PMTiles sections")
    if h["root_offset"] + h["root_length"] > 16384:
        raise ValueError("PMTiles root exceeds the first 16 KiB")
    count = entries = previous_end = 0
    by_zoom = {}
    from pmtiles.tile import tileid_to_zxy

    for entry in tile_entries(reader):
        if entry.tile_id < previous_end or entry.length <= 0 or entry.offset < 0:
            raise ValueError("Unsorted, overlapping or invalid tile entries")
        if entry.offset + entry.length > h["tile_data_length"]:
            raise ValueError("Tile payload outside its section")
        first_zoom = tileid_to_zxy(entry.tile_id)[0]
        last_zoom = tileid_to_zxy(entry.tile_id + entry.run_length - 1)[0]
        if first_zoom != last_zoom or not h["min_zoom"] <= first_zoom <= h["max_zoom"]:
            raise ValueError("Tile run crosses zoom bounds")
        by_zoom[first_zoom] = by_zoom.get(first_zoom, 0) + entry.run_length
        previous_end = entry.tile_id + entry.run_length
        count += entry.run_length
        entries += 1
    if count != h["addressed_tiles_count"] or entries != h["tile_entries_count"]:
        raise ValueError("PMTiles directory counts disagree with the header")
    return {"addressedTiles": count, "entries": entries, "tilesByZoom": by_zoom}


class DiskPacker:
    """Append tile groups, checkpoint after fsync, and finalize in bounded leaves.

    Resume discards only uncommitted bytes in our own partial archive. Callers
    checkpoint source progress in the same SQLite transaction as tile entries.
    No global offset map or tile-content deduplication table is held in memory.
    """

    def __init__(self, path, identity):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(self.path)
        self.partial = self.path.with_suffix(self.path.suffix + ".part")
        self.db = sqlite3.connect(self.path.with_suffix(".work.sqlite"))
        self.db.execute("PRAGMA cache_size=-8192")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tiles (id INTEGER PRIMARY KEY, offset INTEGER, length INTEGER)"
        )
        serialized = json.dumps(identity, sort_keys=True)
        stored = self.get_state("identity")
        if stored is not None and stored != serialized:
            self.db.close()
            raise ValueError("Partial build has a different input or recipe")
        if stored is None:
            if self.partial.exists():
                self.db.close()
                raise ValueError("Partial payload has no authenticated working state")
            self.db.execute("INSERT INTO state VALUES ('identity', ?)", (serialized,))
            self.db.execute("INSERT INTO state VALUES ('end', '16384')")
            self.db.commit()
        self.stream = self.partial.open("r+b" if self.partial.exists() else "w+b")
        end = int(self.get_state("end"))
        if self.stream.seek(0, 2) < end and end != 16384:
            self.close()
            raise ValueError("Partial payload shorter than its checkpoint")
        self.stream.truncate(end)
        self.stream.seek(end)

    def get_state(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def add(self, tile_id, payload):
        position = self.stream.tell()
        self.stream.write(payload)
        self.db.execute(
            "INSERT INTO tiles VALUES (?, ?, ?)",
            (tile_id, position - 16384, len(payload)),
        )

    def checkpoint(self, progress):
        self.stream.flush()
        os.fsync(self.stream.fileno())
        for key, value in {"progress": progress, "end": self.stream.tell()}.items():
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES (?, ?)", (key, str(value))
            )
        self.db.commit()

    def finish(self, header, metadata, *, leaf_size=65536):
        self.checkpoint(self.get_state("progress") or -1)
        data_end = self.stream.tell()
        leaf_start = data_end
        root = []
        cursor = self.db.execute("SELECT id, offset, length FROM tiles ORDER BY id")
        while rows := cursor.fetchmany(leaf_size):
            entries = [
                Entry(tile_id, offset, length, 1) for tile_id, offset, length in rows
            ]
            encoded = serialize_directory(entries)
            # pmtiles 3.7 uses gzip's clock default. Normalize its timestamp.
            encoded = gzip.compress(gzip.decompress(encoded), mtime=0)
            root.append(
                Entry(
                    entries[0].tile_id, self.stream.tell() - leaf_start, len(encoded), 0
                )
            )
            self.stream.write(encoded)
        root_data = gzip.compress(gzip.decompress(serialize_directory(root)), mtime=0)
        if len(root_data) > 16384 - 127:
            raise ValueError("Root directory too large; increase leaf_size on resume")
        metadata_start = self.stream.tell()
        metadata_bytes = gzip.compress(
            json.dumps(metadata, sort_keys=True).encode(), mtime=0
        )
        self.stream.write(metadata_bytes)
        count = self.db.execute("SELECT count(*) FROM tiles").fetchone()[0]
        header = {
            **header,
            "root_offset": 127,
            "root_length": len(root_data),
            "metadata_offset": metadata_start,
            "metadata_length": len(metadata_bytes),
            "leaf_directory_offset": leaf_start,
            "leaf_directory_length": metadata_start - leaf_start,
            "tile_data_offset": 16384,
            "tile_data_length": data_end - 16384,
            "addressed_tiles_count": count,
            "tile_entries_count": count,
            "tile_contents_count": count,
            "clustered": False,
            "internal_compression": Compression.GZIP,
            "tile_compression": Compression.NONE,
            "tile_type": header.get("tile_type", TileType.PNG),
        }
        self.stream.seek(0)
        self.stream.write(serialize_header(header))
        self.stream.write(root_data)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.db.close()
        self.partial.replace(self.path)
        return header

    def close(self):
        self.stream.close()
        self.db.close()
