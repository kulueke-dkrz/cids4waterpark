"""
Creates a small synthetic Zarr store that stands in for a "waterpark" S3
climate dataset (e.g. a CMIP6-style HEALPix monthly-mean variable),
since the sandbox cannot reach the real s3.waterpark.dkrz.de endpoint.

Structurally this mirrors a real item from the Freva STAC catalog:
one Zarr group, a couple of data variables, chunked arrays.
"""
import numpy as np
import xarray as xr
import pandas as pd
import shutil, os

OUT = "waterpark_source/cmip6/healpix/cmip6/historical-r1i1p1f1/demo-model/P1M/level_5.zarr"

def main():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    time = pd.date_range("1990-01-16T12:00:00", periods=24, freq="MS")
    ncells = 12 * 4**5  # a HEALPix level-5 style cell count (small stand-in)
    rng = np.random.default_rng(42)

    tas = 288 + 10 * np.sin(np.linspace(0, 6.28, len(time)))[:, None] + rng.normal(0, 0.5, (len(time), ncells)).astype("float32")
    pr = np.abs(rng.normal(2e-5, 1e-5, (len(time), ncells))).astype("float32")

    ds = xr.Dataset(
        {
            "tas": (("time", "cell"), tas.astype("float32")),
            "pr": (("time", "cell"), pr.astype("float32")),
        },
        coords={"time": time, "cell": np.arange(ncells)},
        attrs={"institute": "Demo Institute", "experiment": "historical", "grid": "healpix-level5"},
    )
    ds.to_zarr(OUT, mode="w", encoding={
        "tas": {"chunks": (6, ncells)},
        "pr": {"chunks": (6, ncells)},
    })
    size = sum(f.stat().st_size for f in __import__("pathlib").Path(OUT).rglob("*") if f.is_file())
    nfiles = sum(1 for f in __import__("pathlib").Path(OUT).rglob("*") if f.is_file())
    print(f"Wrote Zarr store: {OUT}")
    print(f"  {nfiles} files, {size/1e6:.2f} MB total")

if __name__ == "__main__":
    main()
