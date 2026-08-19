"""
Step 1 of the proposal: "every Zarr store gets a hypothetical CID, computed
ahead of time, without moving or duplicating the data yet."

This runs the real `ipfs add --only-hash` against a Zarr store that lives
in ordinary object/file storage (standing in for s3.waterpark.dkrz.de).
--only-hash merkleizes the tree and returns the CID it *would* have on
IPFS, but writes nothing to the local blockstore and touches the network
not at all. It is the real, working version of the "hypothetical CID".
"""
import json
import os
import subprocess
import sys

IPFS_BIN = os.path.join(os.path.dirname(__file__), "kubo", "ipfs")
IPFS_PATH = os.path.join(os.path.dirname(__file__), ".ipfs")


def only_hash_cid(path: str) -> str:
    env = dict(os.environ, IPFS_PATH=IPFS_PATH)
    out = subprocess.run(
        [
            IPFS_BIN, "add", "--only-hash", "-Q", "-r",
            "--cid-version=1", "--raw-leaves",
            "--chunker=size-1048576",  # ~1 MiB, per the IPFS scientific-data guide
            path,
        ],
        env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def main():
    zarr_path = sys.argv[1] if len(sys.argv) > 1 else \
        "waterpark_source/cmip6/healpix/cmip6/historical-r1i1p1f1/demo-model/P1M/level_5.zarr"

    cid = only_hash_cid(zarr_path)

    record = {
        "source_path": os.path.abspath(zarr_path),
        "ipfs_cid": cid,
        "ingested": False,
        "note": "CID computed via `ipfs add --only-hash`; no bytes were "
                "written to any IPFS blockstore and no network call was made.",
    }
    os.makedirs("registry", exist_ok=True)
    reg_file = os.path.join("registry", f"{cid}.json")
    with open(reg_file, "w") as f:
        json.dump(record, f, indent=2)

    print(json.dumps(record, indent=2))
    print(f"\nRegistry entry written: {reg_file}")

    with open("registry_latest_cid.txt", "w") as f:
        f.write(cid)


if __name__ == "__main__":
    main()
