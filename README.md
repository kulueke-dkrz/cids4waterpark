> [!WARNING]
> ## 🚧 Work in Progress 🚧


# CID-Based Access to Waterpark Zarr Data — Working Proof of Concept

This is a runnable implementation of the following concept: a STAC catalog whose items carry a precomputed IPFS CID, served
through a pull-through gateway that ingests data into IPFS on first
request. It runs against a **real local IPFS (Kubo) node**.

## Components

| Piece | Status |
|---|---|
| IPFS node (Kubo) | running locally |
| CID computation (`ipfs add --only-hash`) | CIDv1, no data written |
| Zarr → IPFS ingestion (`ipfs add`) | pinning |
| Byte-for-byte / xarray round-trip |verified below |
| CID-mismatch / integrity check | verified below |
| Waterpark dataset | **Synthetic stand-in.** `make_dataset.py` generates a small Zarr store with the same shape (Zarr group, chunked `(time, cell)` arrays, HEALPix-style metadata) as a real catalog item. |
## Files

- `make_dataset.py` — builds the synthetic waterpark-style Zarr store.
- `precompute_cid.py` — Step 1: `ipfs add --only-hash` to mint the CID without touching the blockstore. Writes `registry/<cid>.json`. Uses whatever `ipfs` is already on your `PATH` and whatever `IPFS_PATH`/repo you already have configured — it doesn't assume a bundled binary or a specific repo location. Override with the `IPFS_BIN` env var if `ipfs` isn't on `PATH`.
- `gateway.py` — Steps 3–5: the FastAPI pull-through gateway. `GET /ipfs/{cid}` checks the local pin, and on a miss, fetches from the registry's source path, re-hashes, pins, and (attempts to) announce to the DHT. Every response — success or error — is guaranteed to be strict, standard JSON: no bare `Infinity`/`NaN` tokens, and unhandled exceptions are returned as JSON instead of FastAPI's default plain-text 500. The ingestion timeout defaults to 3600s and is configurable via `INGEST_TIMEOUT_SECONDS` for larger real datasets.
- `add_stac_asset.py` — Step 2: adds an `ipfs-cid` asset to a STAC Item
- `registry/` — the CID → source-path lookup table the gateway reads. `ingested`/`ingested_at` in each entry are bookkeeping only, updated by the gateway after a real ingestion — the gateway's actual "is this cached?" check is always the live `ipfs pin ls` call, never this file.
- `stac_item_with_cid.json` — example output of Step 2.

## Reproducing it

```bash
# 1. Get a Kubo binary
https://docs.ipfs.tech/install/command-line/#install-official-binary-distributions

# 2. Init and start the node
ipfs init
ipfs daemon

# 3. Build the synthetic dataset and precompute its CID
python make_dataset.py
python precompute_cid.py        # -> registry/<cid>.json, registry_latest_cid.txt
python add_stac_asset.py        # -> stac_item_with_cid.json

# 4. Start the gateway
python -m uvicorn gateway:app --host 127.0.0.1 --port 9000


# 5. Request the CID
CID=$(cat registry_latest_cid.txt)

# IMPORTANT: pins persist across runs in the same IPFS repo. If you've
# requested this CID before (even in an earlier session), it's already
# warm -- reset it first to see a genuine cold/warm pair:
ipfs pin rm "$CID"
ipfs repo gc

curl -s http://127.0.0.1:9000/ipfs/$CID | python -m json.tool   # cold: triggers ingestion
curl -s http://127.0.0.1:9000/ipfs/$CID | python -m json.tool   # warm: served from cache
```

## What was actually verified in this environment

**1. The CID is genuinely computed before anything is ingested.**
```
$ ipfs add --only-hash -Q -r --cid-version=1 --raw-leaves ... level_5.zarr
bafybeicpgbhyu6abinbfddlff7da5vjtahdvfaggfr53rddsbnlmjvrrsa

$ ipfs block stat bafybeicpg...
Error: block was not found locally (offline)   <- confirmed: not ingested
```

**2. Cold request triggers real ingestion; warm request doesn't.**
```
First  GET /ipfs/<cid>  -> cache_hit_at_request_start: false, ingest: {"ingest_seconds": 0.082}
Second GET /ipfs/<cid>  -> cache_hit_at_request_start: true,  ingest: null
total_request_seconds dropped from 0.173s to 0.091s
```

**3. Round-trip integrity: data pulled back out of IPFS is identical.**
```
$ ipfs get <cid> -o /tmp/retrieved
$ diff -rq /tmp/retrieved  waterpark_source/.../level_5.zarr
IDENTICAL: byte-for-byte match

>>> xr.open_zarr(original).identical(xr.open_zarr(retrieved_from_ipfs))
True
```

**4. Source-drift protection.** After the CID was published, the underlying
file was modified (simulating an S3 object changing after its CID was
minted). The gateway re-hashes on ingestion and refuses to serve it:
```
HTTP 409
"CID mismatch: registry says bafybeicpg..., re-hash gives bafybeif2a...
 Source data has drifted since the CID was published — refusing to serve."
```

**5. Unknown CIDs are rejected**, not resolved from the wider network:
```
HTTP 404
"No registry entry for <cid>. This gateway only knows about CIDs that
 DKRZ itself precomputed and published as STAC assets."
```

**6. Ingestion output is parsed defensively.** `ipfs add` is run with
`--progress=false` and only its last non-empty stdout line is trusted as
the CID (rather than the whole captured blob), and a slow/large ingest
that exceeds `INGEST_TIMEOUT_SECONDS` (default 3600s) returns a clean
`504` instead of hanging or producing malformed output:
```
HTTP 504
"Ingestion did not finish within 0s. For larger datasets, raise
 INGEST_TIMEOUT_SECONDS."
```
## Limitations of this PoC

This PoC validates the mechanics: hashing, pull-through caching, integrity
checking on a single node. It does not validate, and a real deployment
would still need to work out:

- **Scale**: this ran on a 2 MB toy dataset; petabyte-scale hashing
  throughput are unproven here.
- **Versioning for growing simulations** not implemented
  in this PoC
- **Concurrency** — many simultaneous cold requests for the same CID would
  need request coalescing, which this gateway does not implement.
