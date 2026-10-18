"""Validate a packed archive over a temporary loopback HTTP Range endpoint."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pmtiles.reader import Reader
from pmtiles.tile import tileid_to_zxy

from .acquisition import digest, write_json
from .pmtiles_archive import RangeSource, local_archive, tile_entries


@contextmanager
def range_server(path):
    """Serve one immutable file on an ephemeral loopback port; no tile server."""
    path = Path(path)
    size = path.stat().st_size
    etag = '"' + digest(path) + '"'
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            if self.path != "/archive.pmtiles":
                self.send_error(404)
                return
            self.send_response(200)
            self.common_headers(size)
            self.end_headers()

        def common_headers(self, length):
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", "application/vnd.pmtiles")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Expose-Headers",
                "Content-Range,ETag,Accept-Ranges,Content-Length",
            )

        def do_GET(self):
            if self.path != "/archive.pmtiles":
                self.send_error(404)
                return
            if self.headers.get("If-Match", etag) != etag:
                self.send_error(412)
                return
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            if match is None:
                self.send_error(416, "Only byte-range retrieval is allowed")
                return
            start = int(match[1])
            end = min(int(match[2]) if match[2] else size - 1, size - 1)
            if not 0 <= start <= end < size:
                self.send_error(416)
                return
            length = end - start + 1
            self.send_response(206)
            self.common_headers(length)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(remaining, 1024**2))
                    self.wfile.write(block)
                    remaining -= len(block)
            requests.append(
                {"start": start, "end": end, "bytes": length, "status": 206}
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/archive.pmtiles", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def check_http(archive, directory, *, coordinates=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if coordinates is None:
        with local_archive(archive) as reader:
            # First and last represented coordinates at each zoom.
            extremes = {}
            for entry in tile_entries(reader):
                z, _, _ = tileid_to_zxy(entry.tile_id)
                extremes.setdefault(z, [entry.tile_id, entry.tile_id])
                extremes[z][1] = entry.tile_id + entry.run_length - 1
            coordinates = [
                tileid_to_zxy(tile_id)
                for tile_id in sorted({i for pair in extremes.values() for i in pair})
            ]
    binary = shutil.which("pmtiles")
    if binary is None:
        raise ValueError("Independent go-pmtiles CLI is required for this check")
    version = subprocess.check_output([binary, "version"], text=True).strip()
    if not version.startswith("pmtiles 1.28.2,"):
        raise ValueError("This validation recipe pins go-pmtiles 1.28.2")
    records = []
    with range_server(archive) as (url, requests), local_archive(archive) as local:
        remote = Reader(RangeSource(url, directory / "http-ranges"))
        for zxy in coordinates:
            expected = local.get(*zxy)
            actual = remote.get(*zxy)
            if actual != expected:
                raise ValueError("HTTP PMTiles reader changed tile bytes or absence")
            item = {"zxy": list(zxy), "present": expected is not None}
            if expected is not None:
                item["sha256"] = hashlib.sha256(expected).hexdigest()
                independent = subprocess.run(
                    [binary, "tile", url, *(str(i) for i in zxy)],
                    capture_output=True,
                    check=True,
                    timeout=60,
                ).stdout
                if independent != expected:
                    raise ValueError(
                        "Independent Go reader returned different tile bytes"
                    )
                item["independentGoReader"] = "passed"
            records.append(item)
        traffic = list(requests)
    report = {
        "status": "passed",
        "transport": "loopback HTTP 206 byte ranges; Python PMTiles and independent Go CLI",
        "archiveSha256": digest(archive),
        "goPmtiles": {"version": version, "binarySha256": digest(binary)},
        "samples": records,
        "requests": traffic,
        "fullArchiveGetAllowed": False,
        "limitations": [
            "Local Range access only; no S3/CDN or browser/Quest acceptance established"
        ],
    }
    write_json(directory / "http-validation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check_http(args.archive, args.output)
    print(
        json.dumps(
            {
                "status": result["status"],
                "samples": len(result["samples"]),
                "rangeRequests": len(result["requests"]),
            }
        )
    )


if __name__ == "__main__":
    main()
