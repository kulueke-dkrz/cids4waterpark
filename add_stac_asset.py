"""
Step 2 of the proposal: publish the CID as an extra STAC asset, alongside
zarr-access / local-access, without changing anything else about the item.

This uses the same shape of STAC Item actually returned by
https://freva.dkrz.de/api/freva-nextgen/stacapi/product/collections/cmip6/items
(see the "assets" block) and only adds one new asset.
"""
import json

STAC_ITEM = {
    "type": "Feature",
    "stac_version": "1.1.0",
    "id": "demo-1000000000000000",
    "properties": {
        "cmor_table": ["Amon"],
        "dataset": "general-waterpark-cmip6-demo",
        "experiment": ["historical"],
        "format": "zarr",
        "fs_type": "s3",
        "model": ["DEMO-MODEL"],
        "title": "https://s3.waterpark.dkrz.de/cmip6/healpix/cmip6/historical-r1i1p1f1/demo-model/P1M/level_5.zarr",
    },
    "assets": {
        "freva-databrowser": {
            "href": "https://freva.dkrz.de/databrowser/?file=...level_5.zarr",
            "title": "Freva Web DataBrowser",
            "roles": ["overview"],
            "type": "text/html",
        },
        "zarr-access": {
            "href": "https://freva.dkrz.de/api/freva-nextgen/databrowser/load/freva?file=...level_5.zarr",
            "title": "Stream Zarr Data",
            "roles": ["data"],
            "type": "application/vnd+zarr",
            # In today's catalog this asset requires OAuth2 (see "requires"/
            # "authentication" fields observed live). In the future state
            # this briefing was asked about, waterpark data needs no auth,
            # so that gate is simply absent here.
        },
        "local-access": {
            "href": "https://freva.dkrz.de/api/freva-nextgen/databrowser/data-search/freva/file?file=...level_5.zarr",
            "title": "Access data locally",
            "roles": ["data"],
            "type": "application/netcdf",
        },
    },
    "collection": "cmip6",
}


def add_ipfs_asset(item: dict, cid: str, gateway_base: str = "http://localhost:8080") -> dict:
    item = json.loads(json.dumps(item))  # deep copy
    item["assets"]["ipfs-cid"] = {
        "href": f"ipfs://{cid}",
        "title": "IPFS Content Identifier (on-demand)",
        "description": (
            "Content-addressed reference for this Zarr store. The bytes are "
            "not necessarily resident on the public IPFS network yet: "
            "dereferencing this CID through the DKRZ gateway triggers "
            "on-demand ingestion from the source object store on first "
            "request. Subsequent requests are served from cache or peers."
        ),
        "roles": ["data", "alternate"],
        "type": "application/vnd+zarr",
        "alternate:name": "ipfs",
        "gateway": f"{gateway_base}/ipfs/{cid}",
    }
    return item


if __name__ == "__main__":
    with open("registry_latest_cid.txt") as f:
        cid = f.read().strip()
    item = add_ipfs_asset(STAC_ITEM, cid)
    with open("stac_item_with_cid.json", "w") as f:
        json.dump(item, f, indent=2)
    print(json.dumps(item["assets"], indent=2))
