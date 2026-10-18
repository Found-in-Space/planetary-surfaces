"""Small, repeatable scratch benchmark of the real ordered conversion path.

Reads at most --sample-tiles source tiles per pass; writes no archive. Timings
include source reads, spawn startup/shutdown, IPC, conversion and output hashing,
but exclude packing, fsync, final validation and a cold global-source workload.
"""

import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from contextlib import closing
from itertools import islice
from pathlib import Path

from foundinspace.terrain.acquisition import digest, write_json
from foundinspace.terrain.mapterhorn import (
    admit_source,
    code_identity,
    converted_tiles,
    execution_settings,
    now,
    source_tiles,
)
from foundinspace.terrain.pmtiles_archive import local_archive


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def benchmark(args):
    source = json.loads(args.manifest.read_text())
    if (
        digest(args.source) != source["sha256"]
        or args.source.stat().st_size != source["bytes"]
    ):
        raise ValueError("Source does not match its immutable manifest")
    results = []
    expected = None
    with local_archive(args.source) as reader:
        header = admit_source(reader)

        def inputs():
            for _ in range(args.passes):
                yield from islice(
                    source_tiles(reader, header, -1, args.source.parent),
                    args.sample_tiles,
                )

        for workers in args.workers:
            execution = execution_settings(workers, args.max_in_flight)
            trials = []
            for _ in range(args.trials):
                checksum = hashlib.sha256()
                count = payload_bytes = 0
                start = time.perf_counter()
                with closing(
                    converted_tiles(
                        inputs(),
                        workers=workers,
                        max_in_flight=execution["maxInFlight"],
                    )
                ) as converted:
                    for tile_id, children in converted:
                        checksum.update(tile_id.to_bytes(8, "little"))
                        for child_id, payload in children:
                            checksum.update(child_id.to_bytes(8, "little"))
                            checksum.update(len(payload).to_bytes(8, "little"))
                            checksum.update(payload)
                            payload_bytes += len(payload)
                        count += 1
                        del children, payload
                elapsed = time.perf_counter() - start
                observed = (count, payload_bytes, checksum.hexdigest())
                if expected is None:
                    expected = observed
                if observed != expected:
                    raise ValueError(
                        "Worker counts or trials changed ordered tile bytes"
                    )
                trials.append(elapsed)
            median = statistics.median(trials)
            results.append(
                {
                    "execution": execution,
                    "trialSeconds": trials,
                    "medianSeconds": median,
                    "sourceTilesPerSecond": count / median,
                    "sourceTileConversions": count,
                    "childPayloadBytes": payload_bytes,
                    "orderedPayloadSha256": checksum.hexdigest(),
                }
            )
    return {
        "status": "scratch-benchmark",
        "createdUtc": now(),
        "hardware": args.hardware,
        "platform": platform.platform(),
        "logicalCpus": os.cpu_count(),
        "producer": code_identity(),
        "sourceSha256": source["sha256"],
        "sourceBytes": source["bytes"],
        "sourceScope": source["kind"],
        "sampleTileLimit": args.sample_tiles,
        "passesPerTrial": args.passes,
        "trialsPerSetting": args.trials,
        "results": results,
        "limitations": [
            "First source tiles only; repeated warm reads, not a representative geographic or I/O sample",
            "Includes pool startup/shutdown, parent reads, IPC, conversion and ordered hashing",
            "Excludes archive packing, durable checkpoints, z0 reduction and final validation",
            "Not a global runtime, memory, storage or output-size forecast",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--hardware", required=True, help="Local CPU and RAM description"
    )
    parser.add_argument("--sample-tiles", type=positive, default=16)
    parser.add_argument("--passes", type=positive, default=16)
    parser.add_argument("--trials", type=positive, default=3)
    parser.add_argument("--workers", type=positive, nargs="+", default=[1, 2, 4])
    parser.add_argument("--max-in-flight", type=positive)
    args = parser.parse_args()
    report = benchmark(args)
    write_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
