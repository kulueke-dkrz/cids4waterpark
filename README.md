> [!WARNING]
> ## 🚧 Work in Progress 🚧
>
> Needs more testing

# CID-Based Access to Waterpark Zarr Data — Working Proof of Concept

This is a runnable implementation of the following concept: a STAC catalog whose items carry a precomputed IPFS CID, served
through a pull-through gateway that ingests data into IPFS on first
request. It runs against a **real local IPFS (Kubo) node**.

## What's real vs. what's stood in for

| Piece | Status |
|---|---|
| IPFS node (Kubo) | **Real binary, real daemon**, running locally |
| CID computation (`ipfs add --only-hash`) | **Real** — genuine CIDv1, no data written |
| Zarr → IPFS ingestion (`ipfs add`) | **Real** — genuine merkleization, pinning |
| Byte-for-byte / xarray round-trip | **Real** — verified below |
| CID-mismatch / integrity check | **Real** — verified below |
| Waterpark dataset | **Synthetic stand-in.** This sandbox cannot reach `s3.waterpark.dkrz.de`, so `make_dataset.py` generates a small Zarr store with the same shape (Zarr group, chunked `(time, cell)` arrays, HEALPix-style metadata) as a real catalog item. |
| DHT "provide" / public peer discovery | **Attempted for real** (`ipfs routing provide`) but finds zero peers — this sandbox has no route to the public IPFS swarm. In production this call is unchanged; only the network path differs. |
| Auth | Omitted entirely, per the "future state: no auth needed" assumption. |

## Files

- `make_dataset.py` — builds the synthetic waterpark-style Zarr store.
- `precompute_cid.py` — Step 1: `ipfs add --only-hash` to mint the CID without touching the blockstore. Writes `registry/<cid>.json`.
- `add_stac_asset.py` — Step 2: adds an `ipfs-cid` asset to a STAC Item, in the same shape the real Freva catalog uses.
- `gateway.py` — Steps 3–5: the FastAPI pull-through gateway. `GET /ipfs/{cid}` checks the local pin, and on a miss, fetches from the registry's source path, re-hashes, pins, and (attempts to) announce to the DHT.
- `registry/` — the CID → source-path lookup table the gateway reads.
- `stac_item_with_cid.json` — example output of Step 2.

## Reproducing it

```bash
# 1. Get a real Kubo binary (allowed from github.com in this sandbox)
curl -sL -o kubo.tar.gz \
  https://github.com/ipfs/kubo/releases/download/v0.33.2/kubo_v0.33.2_linux-amd64.tar.gz
tar xzf kubo.tar.gz

# 2. Init and start the node (offline mode — no public swarm route here)
export IPFS_PATH=$PWD/.ipfs
./kubo/ipfs init --profile=server
./kubo/ipfs daemon --offline &

# 3. Build the synthetic dataset and precompute its CID
pip install --break-system-packages zarr numpy xarray fastapi uvicorn
python3 make_dataset.py
python3 precompute_cid.py        # -> registry/<cid>.json, registry_latest_cid.txt
python3 add_stac_asset.py        # -> stac_item_with_cid.json

# 4. Start the gateway
python3 -m uvicorn gateway:app --host 127.0.0.1 --port 9000 &

# 5. Request the CID
CID=$(cat registry_latest_cid.txt)
curl -s http://127.0.0.1:9000/ipfs/$CID | python3 -m json.tool   # cold: triggers ingestion
curl -s http://127.0.0.1:9000/ipfs/$CID | python3 -m json.tool   # warm: served from cache
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

## What this does *not* prove

This PoC validates the mechanics: hashing, pull-through caching, integrity
checking on a single node. It does not validate, and a real deployment
would still need to work out:

- **Real DHT propagation** to public IPFS peers (needs real network egress).
- **Scale**: this ran on a 2 MB toy dataset; petabyte-scale hashing
  throughput, HAMT sharding for very large chunk counts, and re-chunking
  large production Zarr stores to ~1 MiB blocks are unproven here.
- **A real pinning/retention policy** — this demo never garbage-collects,
  a production cluster needs one.
- **Versioning for growing simulations** (IPNS/DNSLink) — not implemented
  in this PoC; every run here targets a single, static store.
- **Concurrency** — many simultaneous cold requests for the same CID would
  need request coalescing, which this gateway does not implement.
