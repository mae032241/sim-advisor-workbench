"""STEP is geometry: test evidence, limits, and actual CAD-kernel measures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from advisor import geometry_reader as geometry
from advisor.model_reader import ModelInspectionError, inspect_model


STEP = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('geometry only'),'2;1');
FILE_NAME('test.step','2026-01-01',('private author'),('private organization'),'tool','tool','');
FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));
ENDSEC;
DATA;
/* #900=ADVANCED_FACE('fake comment'); */
#1=PRODUCT('product-id','Steel inlet;''name/*not a comment*/','ADVANCED_FACE(fake)',());
#2=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));
#3=MANIFOLD_SOLID_BREP('example declaration only',#4);
#4=CLOSED_SHELL('',(#5));
#5=ADVANCED_FACE('',(),#6,.T.);
#6=CARTESIAN_POINT('',(0.,0.,0.));
ENDSEC;
END-ISO-10303-21;
"""
NO_CLI = {"available": False, "version": None}
NO_KERNEL = {"available": False, "engine": "gmsh/OpenCASCADE", "version": None, "detail": "not installed in this test"}
YES_KERNEL = {"available": True, "engine": "gmsh/OpenCASCADE", "version": "test", "detail": "test fixture"}
OBSERVATION = {"engine": "gmsh/OpenCASCADE", "version": "test", "status": "imported", "target_unit": "m",
               "topology": {"vertices": 8, "edges": 12, "faces": 6, "solids": 1},
               "bounding_box_values": [0, 0, 0, .01, .02, .03], "volume_value": .000006,
               "bodies": [{"tag": 7, "name": "Steel inlet (unverified name)", "surface_count": 6,
                           "bounding_box_values": [0, 0, 0, .01, .02, .03], "volume_value": .000006}],
               "bodies_total": 1, "bodies_truncated": False}


class GeometryReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=geometry.PROJECT_ROOT)
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def fixture(self, data=STEP, name="geometry.step"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def inspect_native(self, path):
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=NO_KERNEL):
            return inspect_model(path)

    def test_dispatch_and_intentional_undefined_simulation(self):
        path = self.fixture(name="STEEL_INLET.STP")
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        snapshot = self.inspect_native(path)
        self.assertEqual(snapshot["asset_kind"], "cad_geometry")
        self.assertEqual(snapshot["simulation_state"], "not_defined")
        for field in ("physics", "materials", "studies", "components"):
            self.assertEqual(snapshot[field], [])
            self.assertEqual(snapshot["definition_status"][field], "not_defined")
        self.assertEqual(snapshot["parameters"], {})
        self.assertEqual(snapshot["definition_status"]["boundary_conditions"], "not_defined")
        self.assertIn("fallback", snapshot["parser"])
        self.assertEqual(snapshot["fingerprint"], hashlib.sha256(before[0]).hexdigest())
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertNotIn("private author", json.dumps(snapshot))
        json.dumps(snapshot, ensure_ascii=False)

    def test_native_declarations_are_not_topology(self):
        snapshot = self.inspect_native(self.fixture())
        cad = snapshot["geometry"]
        self.assertEqual(cad["raw_entity_counts"]["ADVANCED_FACE"], 1)
        self.assertEqual(cad["raw_entity_counts"]["MANIFOLD_SOLID_BREP"], 1)
        self.assertEqual(cad["records_observed"], 6)
        self.assertEqual(cad["length_unit_declarations"][0]["unit"], "mm")
        self.assertIn("/*not a comment*/", cad["products"][0]["name"])
        self.assertEqual(cad["schemas"], ["CONFIG_CONTROL_DESIGN"])
        self.assertIsNone(cad["kernel"]["topology"])
        self.assertIsNone(cad["kernel"]["bounding_box"])
        self.assertIsNone(cad["kernel"]["volume"])
        self.assertEqual(cad["capabilities"]["overall_bounding_box"], "unavailable")
        self.assertEqual(cad["capabilities"]["body_summaries"], "unavailable")
        self.assertEqual(cad["capabilities"]["face_curvature"], "not_implemented")
        self.assertTrue(any("不能替代" in item for item in snapshot["unknowns"]))

    def test_actual_cli_metadata_failure_is_labelled_fallback(self):
        status = {"available": True, "version": "test", "command": ["not-run"]}
        with patch.object(geometry, "parser_status", return_value=status), patch.object(geometry, "geometry_status", return_value=NO_KERNEL), patch.object(geometry, "_scan_step", side_effect=subprocess.TimeoutExpired("scan", 1)):
            snapshot = inspect_model(self.fixture())
        self.assertFalse(snapshot["parser_detail"]["scan_succeeded"])
        self.assertIn("fallback", snapshot["parser"])

    def test_kernel_measures_are_scoped_and_separate_from_step_counts(self):
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", return_value=OBSERVATION):
            snapshot = inspect_model(self.fixture())
        cad = snapshot["geometry"]
        self.assertEqual(cad["raw_entity_counts"]["ADVANCED_FACE"], 1)
        self.assertEqual(cad["kernel"]["topology"]["faces"], 6)
        self.assertEqual(cad["kernel"]["bounding_box"]["unit"], "m")
        self.assertEqual(cad["kernel"]["volume"]["unit"], "m^3")
        self.assertIn("overlaps not subtracted", cad["kernel"]["volume"]["method"])
        self.assertEqual(cad["kernel"]["validity"], "not_checked")
        self.assertEqual(cad["capabilities"]["body_summaries"], "observed")
        self.assertEqual(cad["capabilities"]["per_face_surface_type_and_area"], "not_implemented")
        self.assertEqual(cad["capabilities"]["physical_relevance"], "requires_goal_and_user_or_physics_evidence")
        body = cad["bodies"][0]
        self.assertEqual(body["kernel_dim_tag"], [3, 7])
        self.assertEqual(body["surface_count"], 6)
        self.assertEqual(body["physical_role"], "unknown")
        self.assertEqual(body["volume"]["unit"], "m^3")
        self.assertTrue(any("跨编辑" in item for item in cad["inspection_limits"]))
        self.assertTrue(any("小体积" in item for item in cad["inspection_limits"]))

    def test_unknown_units_do_not_silently_use_importer_defaults(self):
        data = STEP.replace(b"#2=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));", b"")
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", return_value=OBSERVATION):
            snapshot = inspect_model(self.fixture(data))
        self.assertIsNotNone(snapshot["geometry"]["kernel"]["topology"])
        self.assertIsNone(snapshot["geometry"]["kernel"]["bounding_box"])
        self.assertIsNone(snapshot["geometry"]["kernel"]["volume"])
        self.assertIsNone(snapshot["geometry"]["bodies"][0]["bounding_box"])
        self.assertIsNone(snapshot["geometry"]["bodies"][0]["volume"])
        self.assertEqual(snapshot["geometry"]["bodies"][0]["surface_count"], 6)
        self.assertEqual(snapshot["geometry"]["capabilities"]["per_body_bounds"], "unavailable")
        self.assertTrue(any("默认缩放" in item for item in snapshot["unknowns"]))

    def test_body_ids_scoped_to_fingerprint_and_body_list_limits(self):
        first = self.fixture()
        same = self.fixture(name="same-bytes.stp")
        changed = self.fixture(STEP.replace(b"geometry only", b"same geometry, new file revision"), name="changed.step")
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", return_value=OBSERVATION):
            a, b, c = (inspect_model(path) for path in (first, same, changed))
        self.assertEqual(a["geometry"]["bodies"][0]["id"], b["geometry"]["bodies"][0]["id"])
        self.assertNotEqual(a["geometry"]["bodies"][0]["id"], c["geometry"]["bodies"][0]["id"])
        limited = {**OBSERVATION, "bodies": OBSERVATION["bodies"] * 3, "bodies_total": 3, "bodies_truncated": True}
        with patch.object(geometry, "MAX_BODY_SUMMARIES", 2), patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", return_value=limited):
            result = inspect_model(first)
        self.assertEqual(len(result["geometry"]["bodies"]), 2)
        self.assertEqual(result["geometry"]["capabilities"]["body_summaries"], "partial")
        self.assertTrue(result["geometry"]["body_summaries_truncated"])
        self.assertEqual(result["geometry"]["bodies_total"], 3)
        self.assertTrue(any("前 2 项" in item for item in result["geometry"]["inspection_limits"]))

    def test_kernel_timeout_is_not_a_missing_physics_error(self):
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", side_effect=subprocess.TimeoutExpired("kernel", 1)):
            snapshot = inspect_model(self.fixture())
        self.assertEqual(snapshot["geometry"]["kernel"]["status"], "failed")
        self.assertEqual(snapshot["simulation_state"], "not_defined")
        self.assertEqual(snapshot["definition_status"]["physics"], "not_defined")

    def test_native_kernel_fault_has_one_bounded_disclosed_retry(self):
        failed = subprocess.CompletedProcess([], 3221225477, "", "")
        valid = subprocess.CompletedProcess([], 0, geometry._RESULT_PREFIX + json.dumps(OBSERVATION), "")
        with patch.object(geometry, "run_text", side_effect=[failed, valid]) as runner:
            value = geometry._kernel_geometry(self.fixture())
        self.assertEqual(value["import_retry_count"], 1)
        self.assertEqual(runner.call_count, 2)
        for call in runner.call_args_list:
            self.assertGreater(call.args[3], 0)
            self.assertLessEqual(call.args[3], geometry.PARSER_TIMEOUT)
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", return_value=value):
            snapshot = inspect_model(self.fixture())
        self.assertTrue(any("隔离重试" in item for item in snapshot["warnings"]))

    def test_invalid_or_incomplete_step_rejected(self):
        for data in (b"not a STEP file", STEP.replace(b"END-ISO-10303-21;", b""), b"ISO-10303-21;HEADER;ENDSEC;END-ISO-10303-21;"):
            with self.subTest(data=data[:30]), self.assertRaises(ModelInspectionError):
                self.inspect_native(self.fixture(data))

    def test_file_limit_checked_before_external_parser(self):
        path = self.fixture()
        with patch.object(geometry, "MAX_FILE_BYTES", 20), patch.object(geometry, "_scan_step") as scan:
            with self.assertRaisesRegex(ModelInspectionError, "inspection limit"):
                inspect_model(path)
        scan.assert_not_called()

    def test_native_metadata_limit_is_explicitly_partial(self):
        with patch.object(geometry, "MAX_METADATA_BYTES", 300):
            snapshot = self.inspect_native(self.fixture())
        self.assertEqual(snapshot["geometry"]["metadata_status"], "partial")
        self.assertEqual(snapshot["geometry"]["raw_entity_counts_status"], "partial")
        self.assertTrue(snapshot["warnings"])

    def test_change_during_inspection_rejected(self):
        path = self.fixture()
        original = path.stat()
        def change(_):
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 10_000_000))
            return OBSERVATION
        with patch.object(geometry, "parser_status", return_value=NO_CLI), patch.object(geometry, "geometry_status", return_value=YES_KERNEL), patch.object(geometry, "_kernel_geometry", side_effect=change):
            with self.assertRaisesRegex(ModelInspectionError, "changed during inspection"):
                inspect_model(path)


OFFICIAL = Path("D:/comsol/COMSOL63/Multiphysics/applications/Design_Module/Tutorial_Examples/bracket_import_optimization_geom.step")
EXTERNAL_STEP_VALUE = os.environ.get("SIM_ADVISOR_TEST_STEP")
EXTERNAL_STEP = Path(EXTERNAL_STEP_VALUE) if EXTERNAL_STEP_VALUE else None


class ActualGeometryTests(unittest.TestCase):
    def test_actual_official_and_workspace_step_read_only(self):
        if EXTERNAL_STEP is None:
            self.skipTest("set SIM_ADVISOR_TEST_STEP to an external single-solid STEP fixture")
        if not OFFICIAL.is_file() or not EXTERNAL_STEP.is_file() or not geometry.geometry_status()["available"] or not geometry.parser_status()["available"]:
            self.skipTest("actual STEP fixtures or isolated real engines unavailable")
        for path, faces in ((OFFICIAL, 38), (EXTERNAL_STEP, None)):
            with self.subTest(file=path.name):
                before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                snapshot = inspect_model(path)
                self.assertTrue(snapshot["parser_detail"]["scan_succeeded"])
                self.assertEqual(snapshot["geometry"]["kernel"]["status"], "imported")
                self.assertEqual(snapshot["geometry"]["kernel"]["topology"]["solids"], 1)
                observed_faces = snapshot["geometry"]["kernel"]["topology"]["faces"]
                if faces is not None:
                    self.assertEqual(observed_faces, faces)
                self.assertGreater(observed_faces, 0)
                self.assertEqual(snapshot["geometry"]["bodies"][0]["surface_count"], observed_faces)
                self.assertEqual(snapshot["geometry"]["capabilities"]["body_summaries"], "observed")
                self.assertGreater(snapshot["geometry"]["kernel"]["volume"]["value"], 0)
                self.assertEqual(snapshot["simulation_state"], "not_defined")
                self.assertEqual(snapshot["physics"], [])
                self.assertEqual(before, (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns))
                self.assertEqual(snapshot["fingerprint"], before[0])

    def test_analytic_box_mm_to_m_kernel_dimensions_and_volume(self):
        if not geometry.geometry_status()["available"]:
            self.skipTest("isolated Gmsh/OpenCASCADE unavailable")
        with tempfile.TemporaryDirectory(dir=geometry.PROJECT_ROOT) as directory:
            path = Path(directory) / "box.step"
            creator = "import gmsh,sys;gmsh.initialize([],readConfigFiles=False,run=False);gmsh.option.setNumber('General.Terminal',0);gmsh.option.setString('Geometry.OCCTargetUnit','MM');gmsh.model.add('analytic test');gmsh.model.occ.addBox(0,0,0,10,20,30);gmsh.model.occ.synchronize();gmsh.write(sys.argv[1]);gmsh.finalize()"
            result = geometry.run_text([geometry._python_command()[0], "-c", creator, str(path)], "", geometry.PROJECT_ROOT, 15)
            self.assertEqual(result.returncode, 0)
            snapshot = inspect_model(path)
            kernel = snapshot["geometry"]["kernel"]
            self.assertEqual(kernel["topology"]["solids"], 1)
            self.assertEqual(kernel["topology"]["faces"], 6)
            body = snapshot["geometry"]["bodies"][0]
            self.assertEqual(body["surface_count"], 6)
            self.assertEqual(body["physical_role"], "unknown")
            self.assertEqual(snapshot["geometry"]["capabilities"]["minimum_gap"], "not_implemented")
            self.assertEqual(snapshot["geometry"]["capabilities"]["wall_thickness"], "not_implemented")
            self.assertEqual(snapshot["geometry"]["capabilities"]["cross_edit_persistent_ids"], "not_implemented")
            for observed, expected in zip(kernel["bounding_box"]["size"], (.01, .02, .03)):
                self.assertAlmostEqual(observed, expected, delta=1e-6)
            self.assertAlmostEqual(kernel["volume"]["value"], .01 * .02 * .03, delta=1e-12)


if __name__ == "__main__":
    unittest.main()
