"""
Step 3-4 of the proposal: the pull-through IPFS gateway.

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
import math
import os
import shutil
import subprocess
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
# Use the ipfs binary already on PATH (or IPFS_BIN if set), and whatever
# IPFS_PATH/repo is already configured in the environment -- same as
# precompute_cid.py. No bundled binary or repo path is assumed.
IPFS_BIN = os.environ.get("IPFS_BIN") or shutil.which("ipfs") or "ipfs"
REGISTRY_DIR = os.path.join(HERE, "registry")

app = FastAPI(title="DKRZ IPFS Pull-Through Gateway (PoC)")


def strict_json(content: dict, status_code: int = 200) -> Response:
    """
    A JSONResponse that can never emit non-standard tokens (Infinity, -Infinity,
    NaN). Starlette's default JSONResponse uses json.dumps(allow_nan=True),
    which happily writes a bare `Infinity` into the body for any float that
    is inf/-inf/nan -- valid to Python's own parser, but rejected by many
    other JSON consumers (jq, browsers, strict parsers in other languages).
    We guard against ever producing that, regardless of where it might come
    from, rather than trying to prove no code path can create it.
    """
    def sanitize(o):
        if isinstance(o, float) and not math.isfinite(o):
            return None
        if isinstance(o, dict):
            return {k: sanitize(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sanitize(v) for v in o]
        return o

    body = json.dumps(sanitize(content), allow_nan=False)
    return Response(content=body, status_code=status_code, media_type="application/json")


@app.exception_handler(Exception)
async def all_errors_as_json(request: Request, exc: Exception):
    # Any unhandled error still comes back as JSON (not FastAPI's default
    # plain-text "Internal Server Error"), so a client piping the response
    # straight into a JSON parser always gets something parseable.
    return strict_json({"error": type(exc).__name__, "detail": str(exc)}, status_code=500)


@app.exception_handler(HTTPException)
async def http_errors_as_json(request: Request, exc: HTTPException):
    return strict_json({"detail": exc.detail}, status_code=exc.status_code)


def ipfs(*args, timeout=120):
    # No env= override: subprocess inherits the current process's
    # environment, so the caller's own IPFS_PATH (or IPFS's ~/.ipfs
    # default, if unset) is what gets used.
    return subprocess.run(
        [IPFS_BIN, *args], capture_output=True, text=True, timeout=timeout
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


def mark_registry_ingested(cid: str) -> None:
    """
    Update the registry entry's bookkeeping fields after a real ingestion.
    This is informational only -- the gateway's actual "is this cached?"
    check is always the live `ipfs pin ls` call in is_pinned(), never this
    file -- but leaving "ingested" frozen at its precompute-time value of
    false forever is misleading, so keep it honest.
    """
    path = os.path.join(REGISTRY_DIR, f"{cid}.json")
    try:
        with open(path) as f:
            entry = json.load(f)
        entry["ingested"] = True
        entry["ingested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "w") as f:
            json.dump(entry, f, indent=2)
    except (OSError, json.JSONDecodeError):
        # Best-effort bookkeeping -- never fail the request over this.
        pass


def materialize(cid: str, source_path: str) -> dict:
    """Fetch from the source object store, hash-verify, write block, pin."""
    t0 = time.time()
    ingest_timeout = int(os.environ.get("INGEST_TIMEOUT_SECONDS", "3600"))
    try:
        r = ipfs(
            "add", "-Q", "-r",
            "--cid-version=1", "--raw-leaves", "--chunker=size-1048576",
            "--progress=false",  # never emit a progress bar / transfer-rate line
            source_path,
            timeout=ingest_timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"Ingestion did not finish within {ingest_timeout}s. For "
                    f"larger datasets, raise INGEST_TIMEOUT_SECONDS.",
        )
    elapsed = time.time() - t0
    if r.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"ingestion failed (exit {r.returncode}): {r.stderr.strip()[-2000:]}",
        )

    # `-Q --progress=false` should print exactly one line: the CID. Take the
    # last non-empty line defensively, in case anything else ever lands on
    # stdout, rather than trusting the whole blob.
    stdout_lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    produced_cid = stdout_lines[-1].strip() if stdout_lines else ""

    if not produced_cid or any(ch.isspace() for ch in produced_cid):
        raise HTTPException(
            status_code=500,
            detail=f"Could not parse a CID out of `ipfs add` output. "
                    f"Raw stdout (last 500 chars): {r.stdout.strip()[-500:]!r}",
        )

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
    mark_registry_ingested(cid)
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
    return strict_json({
        "cid": cid,
        "cache_hit_at_request_start": cache_hit,
        "ingest": ingest_info,
        "total_request_seconds": total_seconds,
        "top_level_entries": files,
        "source_path": entry["source_path"],
    })


@app.get("/")
def root():
    known_cids = sorted(
        f[:-5] for f in os.listdir(REGISTRY_DIR) if f.endswith(".json")
    ) if os.path.isdir(REGISTRY_DIR) else []
    return {
        "service": "DKRZ IPFS Pull-Through Gateway (PoC)",
        "usage": "GET /ipfs/{cid} to fetch a registered CID (see below)",
        "health_check": "/health",
        "interactive_docs": "/docs",
        "known_cids": known_cids,
    }


@app.get("/favicon.ico")
def favicon():
    # Silences the browser's automatic favicon request; there's no icon to
    # serve, so just answer "no content" instead of a 404 in the logs.
    return Response(status_code=204)


@app.get("/health")
def health():
    r = ipfs("id")
    return {"kubo_up": r.returncode == 0}
