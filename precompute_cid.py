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
import shutil
import subprocess
import sys

# Use the `ipfs` binary already on PATH rather than a bundled copy.
# (Set IPFS_BIN yourself to override, e.g. if `ipfs` isn't on PATH.)
IPFS_BIN = os.environ.get("IPFS_BIN") or shutil.which("ipfs") or "ipfs"


def only_hash_cid(path: str) -> str:
    # Deliberately do NOT set or override IPFS_PATH here. subprocess
    # inherits the current process's environment by default, so whatever
    # IPFS_PATH is already exported in your shell (or IPFS's own default of
    # ~/.ipfs if it isn't set) is what gets used -- this script just uses
    # your existing node/repo as-is.
    out = subprocess.run(
        [
            IPFS_BIN, "add", "--only-hash", "-Q", "-r",
            "--cid-version=1", "--raw-leaves",
            "--chunker=size-1048576",  # ~1 MiB, per the IPFS scientific-data guide
            "--progress=false",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def main():
    if shutil.which(IPFS_BIN) is None:
        sys.exit(
            f"Could not find/execute an `ipfs` binary ('{IPFS_BIN}'). Either "
            "install Kubo and make sure `ipfs` is on PATH, or set the "
            "IPFS_BIN environment variable to the full path of the binary."
        )

    zarr_path = sys.argv[1] if len(sys.argv) > 1 else \
        "waterpark_source/cmip6/healpix/cmip6/historical-r1i1p1f1/demo-model/P1M/level_5.zarr"

    repo = os.environ.get("IPFS_PATH", "~/.ipfs (IPFS_PATH not set, using its default)")
    print(f"Using ipfs binary: {IPFS_BIN}")
    print(f"Using IPFS repo:   {repo}\n")

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
