#!/usr/bin/env python3
"""
Reproducer for QGIS #62030: mesh rasterization samples the upper-left pixel
corner instead of the pixel center.

It creates a 10x10 synthetic mesh with vertex datasets:

  x_value = x coordinate
  y_value = y coordinate
  bump    = small hill, only for visual orientation

After rasterizing with pixel size 1, correct center sampling gives:

  x_value[col] = col + 0.5
  y_value[row] = 9.5 - row

The bug signature is:

  x_delta = actual_x - expected_x = -0.5
  y_delta = actual_y - expected_y = +0.5

Run from an environment where QGIS Python works, for example from a QGIS build:

  export QGIS_PREFIX_PATH="$PWD/build-qgis/output"
  export QGIS_PLUGINPATH="$PWD/build-qgis/output/Contents/PlugIns/qgis"
  export DYLD_LIBRARY_PATH="$PWD/build-qgis/output/Contents/Frameworks:$DYLD_LIBRARY_PATH"
  export PYTHONPATH="$PWD/build-qgis/output/python:$PWD/python:$PWD/python/plugins:$PYTHONPATH"
  python3 qgis_mesh_shift_repro.py
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path


SIZE = 10
PIXEL_SIZE = 1.0
DATASETS = ("x_value", "y_value", "bump")


def die(message: str) -> None:
    raise SystemExit(message)


def add_processing_provider() -> None:
    from qgis.analysis import QgsNativeAlgorithms
    from qgis.core import QgsApplication

    if QgsApplication.processingRegistry().providerById("native") is None:
        QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())


def init_qgis():
    try:
        import qgis
        from qgis.core import QgsApplication
    except Exception as exc:
        die(
            "Could not import QGIS Python bindings.\n"
            "Set QGIS_PREFIX_PATH/PYTHONPATH for your QGIS install or build tree.\n"
            f"Original error: {exc}"
        )

    plugins = Path(qgis.__file__).resolve().parents[1] / "plugins"
    if plugins.exists():
        sys.path.insert(0, str(plugins))

    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)

    app = QgsApplication([], False)
    app.initQgis()
    add_processing_provider()
    return app


def bump(x: float, y: float) -> float:
    return math.exp(-((x - 5.0) ** 2 + (y - 5.0) ** 2) / 2.0)


def nodes() -> list[tuple[int, int]]:
    return [(x, y) for y in range(SIZE + 1) for x in range(SIZE + 1)]


def write_2dm(path: Path, z_value) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write("MESH2D\n")
        for i, (x, y) in enumerate(nodes(), 1):
            f.write(f"ND {i} {x:.6f} {y:.6f} {z_value(x, y):.12g}\n")

        eid = 1
        for y in range(SIZE):
            for x in range(SIZE):
                n1 = y * (SIZE + 1) + x + 1
                n2 = n1 + 1
                n3 = n1 + SIZE + 2
                n4 = n1 + SIZE + 1
                f.write(f"E3T {eid} {n1} {n2} {n3} 1\n")
                eid += 1
                f.write(f"E3T {eid} {n1} {n3} {n4} 1\n")
                eid += 1


def write_dat(path: Path) -> None:
    values = {
        "x_value": [x for x, _ in nodes()],
        "y_value": [y for _, y in nodes()],
        "bump": [bump(x, y) for x, y in nodes()],
    }

    with path.open("w", encoding="utf-8") as f:
        f.write('DATASET\nOBJTYPE "mesh2d"\nRT_JULIAN 2433282.500000\n')
        for name in DATASETS:
            f.write(f'BEGSCL\nND {len(nodes())}\nNC {SIZE * SIZE * 2}\nNAME "{name}"\n')
            f.write("TIMEUNITS se\nTS 0 0.000000\n")
            f.writelines(f"{value:.12g}\n" for value in values[name])
            f.write("ENDDS\n")


def write_inputs(out_dir: Path) -> tuple[Path, Path]:
    mesh_path = out_dir / "shift_repro.2dm"
    dat_path = out_dir / "shift_repro.dat"

    write_2dm(mesh_path, bump)
    write_2dm(out_dir / "x_value_mesh.2dm", lambda x, y: x)
    write_2dm(out_dir / "y_value_mesh.2dm", lambda x, y: y)
    write_dat(dat_path)

    return mesh_path, dat_path


def dataset_group_ids(layer) -> dict[str, int]:
    provider = layer.dataProvider()
    ids = {}
    for i in range(provider.datasetGroupCount()):
        name = provider.datasetGroupMetadata(i).name()
        if name in DATASETS:
            ids[name] = i

    missing = set(DATASETS) - set(ids)
    if missing:
        die(f"Missing dataset group(s): {', '.join(sorted(missing))}")
    return ids


def rasterize(layer, group_id: int, output: Path) -> None:
    from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback

    alg = QgsApplication.processingRegistry().algorithmById("native:meshrasterize")
    if alg is None:
        die("Could not load native:meshrasterize")

    result, ok = alg.run(
        {
            "INPUT": layer,
            "DATASET_GROUPS": [group_id],
            "DATASET_TIME": {"type": "dataset-time-step", "value": [group_id, 0]},
            "EXTENT": f"0,{SIZE},0,{SIZE}",
            "PIXEL_SIZE": PIXEL_SIZE,
            "OUTPUT": str(output),
        },
        QgsProcessingContext(),
        QgsProcessingFeedback(),
    )
    if not ok:
        die(f"native:meshrasterize failed for group {group_id}: {result}")


def read_band(path: Path):
    from osgeo import gdal

    ds = gdal.Open(str(path))
    if ds is None:
        die(f"Could not open raster: {path}")
    return ds.ReadAsArray().astype(float), ds.GetGeoTransform(), ds.GetProjection()


def write_tif(path: Path, array, geotransform, projection) -> None:
    from osgeo import gdal

    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Float64
    )
    ds.SetGeoTransform(geotransform)
    ds.SetProjection(projection)
    ds.GetRasterBand(1).WriteArray(array)
    ds.FlushCache()


def expected_xy(shape, geotransform):
    import numpy as np

    rows, cols = shape
    x0, px, _, y0, _, py = geotransform
    col = np.arange(cols)[None, :]
    row = np.arange(rows)[:, None]
    return x0 + (col + 0.5) * px, y0 + (row + 0.5) * py


def stats(array) -> str:
    import numpy as np

    finite = array[np.isfinite(array)]
    return f"mean={finite.mean():.6g}, min={finite.min():.6g}, max={finite.max():.6g}"


def svg_heatmap(array, title: str) -> str:
    import numpy as np

    finite = array[np.isfinite(array)]
    vmax = max(abs(float(finite.min())), abs(float(finite.max())), 1e-12)
    cells = []
    for r in range(array.shape[0]):
        for c in range(array.shape[1]):
            value = float(array[r, c])
            t = max(-1.0, min(1.0, value / vmax))
            rgb = (50, int(180 + 60 * (1 + t)), 255) if t < 0 else (255, int(220 - 170 * t), 45)
            cells.append(
                f'<rect x="{c * 24}" y="{r * 24}" width="24" height="24" '
                f'fill="rgb{rgb}"><title>{value:.6g}</title></rect>'
            )

    return (
        f"<section><h2>{title}</h2><p>{stats(array)}</p>"
        f'<svg viewBox="0 0 {array.shape[1] * 24} {array.shape[0] * 24}" '
        f'width="360" height="360">{"".join(cells)}</svg></section>'
    )


def write_report(path: Path, x_actual, y_actual, x_delta, y_delta, bump_array) -> None:
    path.write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<title>QGIS mesh rasterize half-cell shift repro</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
svg {{ image-rendering: pixelated; border: 1px solid #999; }}
.grid {{ display: flex; gap: 2rem; flex-wrap: wrap; }}
</style>
<h1>QGIS mesh rasterize half-cell shift repro</h1>
<p>
The mesh has vertex datasets <code>x_value = x</code> and <code>y_value = y</code>.
After rasterizing at pixel size 1, every pixel should contain its center coordinate.
</p>
<p>
Correct deltas are near 0. The half-cell bug gives <code>x_delta = -0.5</code>
and <code>y_delta = +0.5</code> everywhere.
</p>
<div class="grid">
{svg_heatmap(x_actual, "rasterized x_value")}
{svg_heatmap(y_actual, "rasterized y_value")}
{svg_heatmap(x_delta, "actual x_value - expected center x")}
{svg_heatmap(y_delta, "actual y_value - expected center y")}
{svg_heatmap(bump_array, "rasterized bump reference")}
</div>
""",
        encoding="utf-8",
    )


def run(out_dir: Path) -> int:
    from qgis.core import QgsMeshLayer

    out_dir.mkdir(parents=True, exist_ok=True)
    mesh_path, dat_path = write_inputs(out_dir)

    layer = QgsMeshLayer(str(mesh_path), "shift_repro", "mdal")
    if not layer.isValid():
        die(f"Invalid mesh layer: {mesh_path}")
    if not layer.dataProvider().addDataset(str(dat_path)):
        die(f"Could not add dataset: {dat_path}")

    ids = dataset_group_ids(layer)
    for name in DATASETS:
        rasterize(layer, ids[name], out_dir / f"{name}.tif")

    x_actual, geotransform, projection = read_band(out_dir / "x_value.tif")
    y_actual, _, _ = read_band(out_dir / "y_value.tif")
    bump_array, _, _ = read_band(out_dir / "bump.tif")
    x_expected, y_expected = expected_xy(x_actual.shape, geotransform)

    x_delta = x_actual - x_expected
    y_delta = y_actual - y_expected
    write_tif(out_dir / "x_delta.tif", x_delta, geotransform, projection)
    write_tif(out_dir / "y_delta.tif", y_delta, geotransform, projection)
    write_report(out_dir / "report.html", x_actual, y_actual, x_delta, y_delta, bump_array)

    print(f"wrote: {out_dir}")
    print(f"x_delta: {stats(x_delta)}")
    print(f"y_delta: {stats(y_delta)}")
    print(f"open: {out_dir / 'report.html'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("qgis_mesh_shift_repro"),
        help="output directory",
    )
    args = parser.parse_args()

    app = init_qgis()
    try:
        return run(args.out)
    finally:
        app.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
