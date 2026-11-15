#!/usr/bin/env python3
"""Verify the built wheel carries a valid openHop plugin manifest and entry point.

Run after `python -m build --wheel`; used by CI and by hand before a release. The
openHop plugin manager loads the manifest from
``.data/data/share/openhop/plugins/<id>/openhop-plugin.json`` and starts the
console script named in ``runtime.entrypoint``, so both must be present and agree.
"""
from __future__ import annotations

import glob
import json
import sys
import zipfile


def main() -> int:
    wheels = sorted(glob.glob("dist/*.whl"))
    if not wheels:
        print("FAIL: no wheel in dist/", file=sys.stderr)
        return 1
    wheel = wheels[-1]

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        manifests = [n for n in names if n.endswith("discord.bridge/openhop-plugin.json")]
        if len(manifests) != 1:
            print(f"FAIL: expected exactly one manifest, found {manifests}", file=sys.stderr)
            return 1

        manifest = json.loads(archive.read(manifests[0]))
        entry_points_path = next(
            (n for n in names if n.endswith("dist-info/entry_points.txt")), None
        )
        if entry_points_path is None:
            print("FAIL: no entry_points.txt in wheel", file=sys.stderr)
            return 1
        entry_points = archive.read(entry_points_path).decode()

    expected = f"{manifest['runtime']['entrypoint']} = openhop_discord_bridge.server:main"
    checks = [
        (manifest.get("schema") == 1, "manifest schema is 1"),
        (manifest.get("id") == "discord.bridge", "manifest id is discord.bridge"),
        (manifest.get("runtime", {}).get("type") == "python", "runtime type is python"),
        (expected in entry_points, f"console script present ({expected})"),
        ("openhop_discord_bridge/server.py" in "".join(zipfile.ZipFile(wheel).namelist()),
         "server module packaged"),
    ]

    failed = False
    for ok, label in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed |= not ok

    if failed:
        return 1
    print(f"OK {wheel}: {manifest['id']} {manifest['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
