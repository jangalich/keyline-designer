"""
test_render_opening_disc_lead.py

Offline (no-network) verification for the disc structuring element and the lead
erode added for production_area.py's render opening (branch: disc + lead erode +
MIN_ZONE_WAIST_METERS retune).

Covers the raster-primitive-level guarantees:
  V1  binary_erode / binary_dilate / eroded_cell_mask defaults are byte-identical
      to the pre-change behavior (square, no lead).
  V2  a disc element differs from the square default as intended: a disc erosion
      of a disc-shaped mask leaves a rounder boundary (higher exterior vertex
      count) than the square element's 45-degree-facetted one.
  V3  the lead erode composes into a SINGLE erosion (extra_erode_cells adds to
      the radius) rather than being a distinct second pass.
  V6  the lead erode widens the neck-severance threshold from 2r to 2(r+1).

(Boundedness and the exactly-one-cell inset are verified in
test_production_area_render_opening.py; the MIN_ZONE_WAIST_METERS 12->24 change
is verified through test_production_area.py's updated waist fixtures.)

Synthetic 5.0m x 5.0m DEM throughout. Nothing here touches the network.
"""

import numpy as np

from raster_grid import (
    D8_OFFSETS,
    _shift,
    binary_dilate,
    binary_erode,
    cell_union_footprint,
    connected_components,
    eroded_cell_mask,
    waist_erosion_radius_cells,
)

DEM = {"resolution_meters": (5.0, 5.0), "origin_x": 500000.0, "origin_y": 4500000.0, "crs": "EPSG:32617"}


def _rect(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask(shape, cells):
    m = np.zeros(shape, dtype=bool)
    for r, c in cells:
        m[r, c] = True
    return m


def _exterior_vertices(geom):
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords)
    return max(len(p.exterior.coords) for p in geom.geoms)


# ===================================================================== #
# V1 -- defaults byte-identical to before.                             #
# Independently reconstruct the pre-change SQUARE behavior (D8 ring     #
# repeated radius_cells times) and require the default output to match  #
# it bit-for-bit, for erode, dilate, and eroded_cell_mask.             #
# ===================================================================== #

_probe = _mask((20, 24), _rect(3, 17, 4, 20))  # an off-center block


def _reference_square_erode(mask, r):
    out = mask.copy()
    for _ in range(r):
        shrunk = out.copy()
        for dr, dc in D8_OFFSETS:
            shrunk &= _shift(out, dr, dc)
        out = shrunk
    return out


def _reference_square_dilate(mask, r):
    out = mask.copy()
    for _ in range(r):
        grown = out.copy()
        for dr, dc in D8_OFFSETS:
            grown |= _shift(out, dr, dc)
        out = grown
    return out


for r in (1, 2, 3, 4):
    assert binary_erode(_probe, r).tobytes() == _reference_square_erode(_probe, r).tobytes(), (
        f"binary_erode default must be byte-identical to the pre-change square erosion (r={r})"
    )
    assert binary_erode(_probe, r).tobytes() == binary_erode(_probe, r, element="square").tobytes()
    assert binary_dilate(_probe, r).tobytes() == _reference_square_dilate(_probe, r).tobytes(), (
        f"binary_dilate default must be byte-identical to the pre-change square dilation (r={r})"
    )
    assert binary_dilate(_probe, r).tobytes() == binary_dilate(_probe, r, element="square").tobytes()

# eroded_cell_mask default (no element, no lead) == binary_erode by the computed radius.
_cells = _rect(3, 17, 4, 20)
_ecm_default = eroded_cell_mask(_cells, (20, 24), DEM, 12.0)
_ecm_reference = binary_erode(_mask((20, 24), _cells), waist_erosion_radius_cells(DEM, 12.0))
assert _ecm_default.tobytes() == _ecm_reference.tobytes(), (
    "eroded_cell_mask default must be byte-identical to a plain binary_erode by the computed radius"
)

# invalid element rejected on both primitives
for fn in (binary_erode, binary_dilate):
    try:
        fn(_probe, 2, element="octagon")
    except ValueError:
        pass
    else:
        raise AssertionError(f"{fn.__name__} must reject an unknown element")

print(
    "V1 defaults unchanged: binary_erode/binary_dilate/eroded_cell_mask are byte-identical to the pre-change "
    "square behavior with no new arguments (checked r=1..4); an unknown element raises ValueError."
)


# ===================================================================== #
# V2 -- disc differs from square (rounder boundary).                    #
# ===================================================================== #

N, ctr, rad = 40, 20, 16
_disc_mask = _mask((N, N), [(r, c) for r in range(N) for c in range(N) if (r - ctr) ** 2 + (c - ctr) ** 2 <= rad * rad])

_sq_eroded = binary_erode(_disc_mask, 4, element="square")
_dc_eroded = binary_erode(_disc_mask, 4, element="disc")
_sq_verts = _exterior_vertices(cell_union_footprint(DEM, _sq_eroded))
_dc_verts = _exterior_vertices(cell_union_footprint(DEM, _dc_eroded))

# The square (Chebyshev) element reaches sqrt(2)*r diagonally, cutting the disc's
# corners into flat 45-degree facets -- fewer exterior vertices and less surviving
# area. The disc element reaches only r in every direction, so it stays round.
assert _dc_verts > _sq_verts, (
    f"a disc erosion must leave a rounder boundary (more exterior vertices) than the square element's "
    f"45-degree facets -- disc {_dc_verts} vs square {_sq_verts}"
)
assert int(_dc_eroded.sum()) > int(_sq_eroded.sum()), (
    "the disc element (Euclidean reach r) erodes a disc less than the square element (Chebyshev reach ~1.4r)"
)
print(
    f"V2 disc vs square: eroding a disc-shaped mask by radius 4 leaves {_sq_verts} exterior vertices under the "
    f"square element (flat 45-degree facets) vs {_dc_verts} under the disc (rounder) -- disc keeps "
    f"{int(_dc_eroded.sum())} cells vs square's {int(_sq_eroded.sum())}."
)


# ===================================================================== #
# V3 -- the lead erode composes into a single erosion.                  #
# ===================================================================== #

_v3_cells = _rect(0, 20, 0, 20)
_v3_shape = (30, 30)
# At 5m cells, waist_erosion_radius_cells(12) = 2 and (24) = 3. So extra_erode=1
# on top of the 12m radius must equal extra_erode=0 on the 24m radius (both = 3
# cells) -- i.e. the lead is ADDED to the computed radius, one composed erosion.
assert waist_erosion_radius_cells(DEM, 12.0) == 2 and waist_erosion_radius_cells(DEM, 24.0) == 3
_via_lead = eroded_cell_mask(_v3_cells, _v3_shape, DEM, 12.0, extra_erode_cells=1)
_via_radius = eroded_cell_mask(_v3_cells, _v3_shape, DEM, 24.0, extra_erode_cells=0)
assert _via_lead.tobytes() == _via_radius.tobytes(), (
    "extra_erode_cells=1 on the 12m radius must be byte-identical to extra_erode_cells=0 on the 24m radius "
    "(both a single 3-cell erosion) -- the lead composes into the radius, not a separate pass"
)
# and, for the square element, consecutive erosions compose: erode(r) then erode(1) == erode(r+1)
_blk = _mask(_v3_shape, _v3_cells)
assert binary_erode(binary_erode(_blk, 2), 1).tobytes() == binary_erode(_blk, 3).tobytes(), (
    "consecutive square erosions must compose: erode(m, 2) then erode(., 1) == erode(m, 3)"
)
# also holds through the disc element used by the render opening
_via_lead_disc = eroded_cell_mask(_v3_cells, _v3_shape, DEM, 12.0, element="disc", extra_erode_cells=1)
_via_radius_disc = eroded_cell_mask(_v3_cells, _v3_shape, DEM, 24.0, element="disc", extra_erode_cells=0)
assert _via_lead_disc.tobytes() == _via_radius_disc.tobytes()
print(
    "V3 lead composes: eroded_cell_mask(..., extra_erode_cells=1) at the 12m radius equals "
    "eroded_cell_mask(..., extra_erode_cells=0) at the 24m radius (a single 3-cell erosion), for both the "
    "square and disc elements; consecutive square erosions compose (erode 2 then 1 == erode 3)."
)


# ===================================================================== #
# V6 -- the lead widens the neck-severance threshold from 2r to 2(r+1). #
# ===================================================================== #

_r = waist_erosion_radius_cells(DEM, 12.0)  # 2, the render opening's base radius
_neck_w = 2 * _r + 1  # 5 cells: survives an r-erosion (5 > 2r=4), severed by an (r+1)-erosion (5 <= 2(r+1)=6)
_v6_shape = (30, 60)
_lobe_a = _rect(0, 20, 0, 14)
_lobe_b = _rect(0, 20, 20, 34)
_nr0 = (20 - _neck_w) // 2
_neck = _rect(_nr0, _nr0 + _neck_w, 14, 20)
_v6_mask = _mask(_v6_shape, _lobe_a + _lobe_b + _neck)

_sev_at_r = connected_components(binary_erode(_v6_mask, _r, element="disc"))[1]
_sev_at_r1 = connected_components(binary_erode(_v6_mask, _r + 1, element="disc"))[1]
assert _sev_at_r == 1, (
    f"a {_neck_w}-cell neck (width 2r+1) must SURVIVE the base r={_r} erosion (stay 1 component), got {_sev_at_r}"
)
assert _sev_at_r1 == 2, (
    f"the same neck must be SEVERED by the r+1={_r + 1} erosion the lead adds (2 components), got {_sev_at_r1}"
)
print(
    f"V6 severance threshold: a {_neck_w}-cell (width 2r+1) neck survives the base r={_r} erosion (1 component) "
    f"but is severed by the lead's r+1={_r + 1} erosion (2 components) -- the lead widens severance from 2r to 2(r+1)."
)


print("\nAll render-opening disc + lead checks passed.")
