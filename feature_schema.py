"""
feature_schema.py

Shared GeoJSON FeatureCollection schema used by every vector-data layer in
this pipeline (hydrology and soil today; other layers as they're converted
in later passes).

Every Feature built with this schema follows the standard GeoJSON Feature
shape, plus two required properties beyond the usual "name"-type fields:

    {
        "id": "<unique-string>",
        "type": "Feature",
        "geometry": {"type": "<Point|LineString|Polygon|...>", "coordinates": [...]},
        "properties": {
            "layer": "<layer-name>",
            "label": "<human-readable name>",
            "confidence": "high" | "medium" | "low",
            "confidence_notes": "<required — plain-language caveat>",
            ...additional layer-specific fields...
        }
    }

"confidence_notes" is required and non-empty on every feature: a
plain-language statement of that layer's (or that specific feature's)
data-quality limitations — survey scale, currency, generalization, whatever
actually applies. A design tool built on public data is only as trustworthy
as the caveats attached to it, so this is enforced here rather than left as
a convention individual layers might skip.

This module only defines and validates the shape — it doesn't fetch
geometry itself. Each layer module (hydrology_data.py, soil_data.py,
nlcd_data.py, ...) is responsible for producing a GeoJSON geometry dict in
WGS84 (EPSG:4326) and calling make_feature() to wrap it. polygonal_parts()
is a small shared helper for layers that clip source geometry to a
boundary (soil_data.py's SQL-side STIntersection, nlcd_data.py's raster
polygonize-then-clip) — that kind of clip can produce a degenerate
GeometryCollection when the boundary only grazes a polygon's edge, and
every clipping layer needs the same fix for it.
"""

from typing import Any, Optional

from shapely.ops import unary_union

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
VALID_CONFIDENCE_LEVELS = {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW}

# Point/LineString/Polygon are the shapes named in the schema contract; the
# Multi* variants are included because real source data (e.g. a single
# SSURGO map unit split across several disjoint patches) legitimately
# produces them, and this is a wrapping step, not a geometry-editing one.
VALID_GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
}


def polygonal_parts(geom):
    """
    Clipping a source geometry to a boundary (SQL Server's STIntersection,
    or a raster polygonize-then-intersect) can, in edge cases where the
    boundary only touches the source shape along a shared edge or at a
    single vertex, return a GeometryCollection mixing stray points or lines
    in with the actual polygon area. Only the Polygon/MultiPolygon parts
    represent real area; this drops everything else and returns None if
    nothing polygonal survives (or the geometry was already empty).
    """
    if geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        return unary_union(polys) if polys else None
    return None


def make_feature(
    feature_id: str,
    geometry: dict,
    layer: str,
    label: str,
    confidence: str,
    confidence_notes: str,
    extra_properties: Optional[dict[str, Any]] = None,
) -> dict:
    """
    Builds a single schema-conformant GeoJSON Feature.

    geometry must already be a GeoJSON geometry dict
    ({"type": ..., "coordinates": ...}) in WGS84 — this function wraps it,
    it does not reproject, simplify, or otherwise transform it.

    Raises ValueError if confidence isn't one of the valid levels,
    confidence_notes is empty, or geometry isn't a recognizable GeoJSON
    geometry — these are treated as programmer errors in the calling layer
    module, not recoverable runtime conditions.
    """
    if confidence not in VALID_CONFIDENCE_LEVELS:
        raise ValueError(
            f"confidence must be one of {sorted(VALID_CONFIDENCE_LEVELS)}, "
            f"got {confidence!r} (feature_id={feature_id!r})"
        )

    if not confidence_notes or not str(confidence_notes).strip():
        raise ValueError(
            f"confidence_notes is required and cannot be empty (feature_id={feature_id!r})"
        )

    if not isinstance(geometry, dict) or geometry.get("type") not in VALID_GEOMETRY_TYPES:
        raise ValueError(
            f"geometry must be a GeoJSON geometry dict with a valid type "
            f"(feature_id={feature_id!r}), got {geometry!r}"
        )

    if "coordinates" not in geometry:
        raise ValueError(f"geometry is missing 'coordinates' (feature_id={feature_id!r})")

    properties = {
        "layer": layer,
        "label": label,
        "confidence": confidence,
        "confidence_notes": confidence_notes,
    }
    if extra_properties:
        properties.update(extra_properties)

    return {
        "id": feature_id,
        "type": "Feature",
        "geometry": geometry,
        "properties": properties,
    }


def make_feature_collection(features: list[dict]) -> dict:
    """Wraps a list of schema-conformant Features into a FeatureCollection."""
    return {"type": "FeatureCollection", "features": features}


def validate_feature_collection(collection: dict) -> None:
    """
    Raises ValueError on the first problem found; returns None if the
    collection is valid. Checks:

      - top-level shape is a valid FeatureCollection
      - every feature has a unique string id, type "Feature", and a
        geometry with a recognized type and coordinates
      - every feature has properties.layer, and a properties.confidence
        drawn from the valid set with a non-empty properties.confidence_notes

    Intended for tests and a one-time sanity check after fetching a layer,
    not for hot-path use on every request.
    """
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError("collection must be a dict with type == 'FeatureCollection'")

    features = collection.get("features")
    if not isinstance(features, list):
        raise ValueError("collection['features'] must be a list")

    seen_ids = set()

    for i, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise ValueError(f"feature at index {i} is not a dict")

        if feature.get("type") != "Feature":
            raise ValueError(f"feature at index {i} must have type == 'Feature'")

        feature_id = feature.get("id")
        if not feature_id or not isinstance(feature_id, str):
            raise ValueError(f"feature at index {i} must have a non-empty string 'id'")
        if feature_id in seen_ids:
            raise ValueError(f"duplicate feature id: {feature_id!r}")
        seen_ids.add(feature_id)

        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in VALID_GEOMETRY_TYPES:
            raise ValueError(f"feature {feature_id!r} has an invalid or missing geometry")
        if "coordinates" not in geometry:
            raise ValueError(f"feature {feature_id!r} geometry is missing 'coordinates'")

        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"feature {feature_id!r} must have a 'properties' dict")

        if not properties.get("layer"):
            raise ValueError(f"feature {feature_id!r} is missing required properties.layer")

        confidence = properties.get("confidence")
        if confidence not in VALID_CONFIDENCE_LEVELS:
            raise ValueError(
                f"feature {feature_id!r} properties.confidence must be one of "
                f"{sorted(VALID_CONFIDENCE_LEVELS)}, got {confidence!r}"
            )

        confidence_notes = properties.get("confidence_notes")
        if not confidence_notes or not str(confidence_notes).strip():
            raise ValueError(
                f"feature {feature_id!r} is missing required properties.confidence_notes"
            )
