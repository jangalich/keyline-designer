"""
make_landform_fixture.py

Captures ONE real landform session -- generate, then three commits -- and
writes it out as JSON for the frontend's own tests to run against.

WHY A CAPTURE RATHER THAN A HAND-WRITTEN FIXTURE. The frontend's crossing
agreement test compares zoneGeometry.js's cautionsFor() -- real
polygon-clipping, in JS -- against what THIS backend actually recorded for the
same zone. A hand-written payload would make that comparison a comparison of
two guesses. Everything below comes off session_manager, step_orchestrator and
commit_validation, with only the network mocked (test_step_commit.py's own
harness, imported rather than copied).

Run:  python make_landform_fixture.py <out.json>
"""

import json
import sys

import test_step_commit as T


def main(out_path):
    with T.Harness():
        session = T.Session()
        payload = session.generate()
        document_after_generate = session.stored()

        # The zone that crosses hydric ABOVE the floor.
        hydric = T._drawn("drawn-hydric", T.HYDRIC_ZONE_RING)
        committed = session.commit(
            T._collection([hydric]), {"drawn-hydric": "user_added"}, base_revision=0
        )
        hydric_crossings = committed["steps"]["landform"]["features"]["features"][0][
            "properties"
        ]["exclusion_crossings"]

        # The zone that grazes hydric BELOW the floor and crosses slope above it.
        session2 = T.Session()
        session2.generate()
        graze = T._drawn("drawn-graze", T.HYDRIC_GRAZE_RING)
        committed2 = session2.commit(
            T._collection([graze]), {"drawn-graze": "user_added"}, base_revision=0
        )
        graze_crossings = committed2["steps"]["landform"]["features"]["features"][0][
            "properties"
        ]["exclusion_crossings"]

        # A 422: an off-parcel drawn zone alongside a valid selected proposal.
        session3 = T.Session()
        payload3 = session3.generate()
        keep = payload3["suggested_zones"]["features"][0]
        off = T._drawn("drawn-off", T.OFF_PARCEL_RING)
        try:
            session3.commit(
                T._collection([keep, off]),
                {keep["id"]: "generated", "drawn-off": "user_added"},
                base_revision=0,
            )
            raise AssertionError("the off-parcel commit was expected to be rejected")
        except Exception as exc:
            rejection = exc.as_payload()

        # Reopen, so the frontend can test the restored selection.
        session4 = T.Session()
        payload4 = session4.generate()
        picked = [f["id"] for f in payload4["suggested_zones"]["features"][:2]]
        drawn4 = T._drawn("drawn-keep", T.HYDRIC_ZONE_RING)
        chosen = [
            f for f in payload4["suggested_zones"]["features"] if f["id"] in picked
        ] + [drawn4]
        provenance = {f_id: "generated" for f_id in picked}
        provenance["drawn-keep"] = "user_added"
        committed4 = session4.commit(
            T._collection(chosen), provenance, base_revision=0
        )
        reopened = session4.reopen()

        fixture = {
            "boundary": [list(point) for point in T.REAL_BOUNDARY],
            "payload": payload,
            "document_generated": document_after_generate,
            "document_committed": committed,
            "hydric": {
                "feature": hydric,
                "recorded_crossings": hydric_crossings,
            },
            "graze": {
                "feature": graze,
                "recorded_crossings": graze_crossings,
            },
            "rejection_422": rejection,
            "reopen": {
                "committed_document": committed4,
                "reopened_document": reopened,
                "selected_ids": picked,
            },
        }

    with open(out_path, "w") as handle:
        json.dump(fixture, handle)
    print(f"wrote {out_path}")
    print(f"  payload keys: {sorted(fixture['payload'].keys())}")
    print(f"  hydric crossings: {hydric_crossings}")
    print(f"  graze crossings:  {graze_crossings}")
    print(f"  rejection: {rejection}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "landform_fixture.json")
