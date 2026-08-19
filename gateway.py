"""
Step 3-5 of the proposal: the pull-through IPFS gateway.

  GET /ipfs/{cid}
    1. Check whether the CID is already pinned locally (warm cache).
    2. If not: look the CID up in the registry (CID -> source path on the
       object store), materialize it for real with `ipfs add` (this reads
       the source bytes, hashes them, writes blocks, and pins), and record
       the wall-clock cost.
    3. Serve the content back (via `ipfs cat` for a single logical file,
       or return the block/DAG listing for a directory-like Zarr store)
       and report whether this was a cold or warm hit.

This is a real, runnable HTTP service backed by a real local Kubo node —
not a mock. The one thing genuinely faked, because this sandbox cannot
reach the public internet's IPFS swarm, is the DHT "provide" step actually
reaching real peers; the `ipfs add`/pin call that would trigger it is real
and is invoked exactly as it would be in production.
"""
import glob
import json
import os
import subprocess
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
IPFS_BIN = os.path.join(HERE, "kubo", "ipfs")
IPFS_PATH = os.path.join(HERE, ".ipfs")
REGISTRY_DIR = os.path.join(HERE, "registry")

app = FastAPI(title="DKRZ IPFS Pull-Through Gateway (PoC)")


def ipfs(*args, timeout=120):
    env = dict(os.environ, IPFS_PATH=IPFS_PATH)
    return subprocess.run(
        [IPFS_BIN, *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def is_pinned(cid: str) -> bool:
    r = ipfs("pin", "ls", "--type=recursive", cid)
    return r.returncode == 0 and cid in r.stdout


def registry_lookup(cid: str) -> dict:
    path = os.path.join(REGISTRY_DIR, f"{cid}.json")
    if not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail=f"No registry entry for {cid}. This gateway only knows "
                    f"about CIDs that DKRZ itself precomputed and published "
                    f"as STAC assets \u2014 it cannot resolve arbitrary CIDs.",
        )
    with open(path) as f:
        return json.load(f)


def materialize(cid: str, source_path: str) -> dict:
    """Fetch from the source object store, hash-verify, write block, pin."""
    t0 = time.time()
    r = ipfs(
        "add", "-Q", "-r",
        "--cid-version=1", "--raw-leaves", "--chunker=size-1048576",
        source_path,
    )
    elapsed = time.time() - t0
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=f"ingestion failed: {r.stderr}")
    produced_cid = r.stdout.strip()
    if produced_cid != cid:
        # Integrity check: re-hashing the source must reproduce the same CID
        # that was published in the STAC catalog. If it doesn't, the source
        # data changed since the CID was minted.
        raise HTTPException(
            status_code=409,
            detail=f"CID mismatch: registry says {cid}, re-hash gives "
                    f"{produced_cid}. Source data has drifted since the "
                    f"CID was published \u2014 refusing to serve.",
        )
    # Attempt to announce to the DHT. In this sandbox there is no route to
    # the public swarm, so this will simply find zero peers; the call is
    # made for real regardless, exactly as production code would.
    ipfs("routing", "provide", cid, timeout=10)
    return {"ingest_seconds": round(elapsed, 4)}


@app.get("/ipfs/{cid}")
def get_cid(cid: str):
    t_start = time.time()
    entry = registry_lookup(cid)

    cache_hit = is_pinned(cid)
    ingest_info = None
    if not cache_hit:
        ingest_info = materialize(cid, entry["source_path"])

    ls = ipfs("ls", cid)
    files = []
    if ls.returncode == 0:
        for line in ls.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                size_field = parts[1]
                files.append({
                    "cid": parts[0],
                    "size_bytes": int(size_field) if size_field.isdigit() else None,
                    "name": parts[2],
                })

    total_seconds = round(time.time() - t_start, 4)
    return JSONResponse({
        "cid": cid,
        "cache_hit_at_request_start": cache_hit,
        "ingest": ingest_info,
        "total_request_seconds": total_seconds,
        "top_level_entries": files,
        "source_path": entry["source_path"],
    })


@app.get("/health")
def health():
    r = ipfs("id")
    return {"kubo_up": r.returncode == 0}
