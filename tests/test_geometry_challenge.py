"""Neutral geometry preparation evidence, privacy boundary, and pure-model checks."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

APP = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("geometry_challenge_prepare", APP / "scripts/geometry_challenge/prepare.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class GeometryChallengeTests(unittest.TestCase):
    def setUp(self):
        self.allowed = (APP / "data/geometry-challenge-02/private").resolve()
        self.allowed.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="preparation-tests-", dir=self.allowed)
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.assertEqual(self.root.parent, self.allowed)
        self.temporary.cleanup()

    def test_neutral_header_removes_identity_without_modifying_cad_records(self):
        text = "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Secret analysis'),'2;1');\nFILE_NAME('private-model.step','date',('author'),('organization'),'tool','original-case','');\nFILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));\nENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(1.,2.,3.));\nENDSEC;\nEND-ISO-10303-21;"
        neutral = prepare.neutral_header(text)
        self.assertNotIn("Secret", neutral)
        self.assertNotIn("private-model", neutral)
        self.assertNotIn("author", neutral)
        self.assertIn("part_01.step", neutral)
        self.assertEqual(text.split("DATA;", 1)[1], neutral.split("DATA;", 1)[1])

    def mph(self, xml, extra=None):
        path = self.root / "geometry_only.mph"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dmodel.xml", xml)
            for name, value in (extra or {}).items():
                archive.writestr(name, value)
        return path

    def test_pure_geometry_model_accepted_empty_container_lists_not_physics(self):
        proof = prepare.verify_geometry_only_mph(self.mph('<Model><PhysicsList/><MaterialList/><StudyList/><SolverSequenceList/><GeomSequence tag="geom1"/></Model>'))
        self.assertEqual(proof["state"], "geometry_only")
        self.assertFalse(any(proof["forbidden_simulation_node_counts"].values()))

    def test_any_simulation_nodes_or_solutions_rejected(self):
        for node in ("Physics", "Material", "Study", "SolverSequence", "MeshSequence", "MultiphysicsCoupling"):
            with self.subTest(node=node), self.assertRaisesRegex(ValueError, "forbidden"):
                prepare.verify_geometry_only_mph(self.mph(f'<Model><{node} tag="x"/></Model>'))
        with self.assertRaisesRegex(ValueError, "forbidden"):
            prepare.verify_geometry_only_mph(self.mph('<Model/>', {"solution1.mphbin": b"data"}))

    def test_source_case_label_rejected(self):
        with self.assertRaisesRegex(ValueError, "leaks"):
            prepare.verify_geometry_only_mph(self.mph('<Model><label>bracket_contact</label></Model>'))

    def test_custom_source_terms_rejected_in_all_text_metadata(self):
        path = self.mph('<Model><GeomSequence/></Model>', {"modelinfo.xml": '<info title="Source.Name+02"/>'})
        with self.assertRaisesRegex(ValueError, "leaks"):
            prepare.verify_geometry_only_mph(path, ("source.name+02",))
        self.assertTrue(prepare.contains_forbidden_term("PRIVATE 微混合器", ("微混合器",)))
        self.assertFalse(prepare.contains_forbidden_term("sourceXnameeeee02", ("source.name+02",)))

    def test_source_dimension_or_preview_rejected_before_native_runtime(self):
        for dimension, node_type, message in (("2", "compact", "3D"), ("3", "preview", "preview-only")):
            source = self.root / "source.mph"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("modelinfo.xml", f'<modelInfo nodeType="{node_type}"><geometryInfo><geom dimension="{dimension}"/></geometryInfo></modelInfo>')
                archive.writestr("dmodel.xml", "<Model/>")
            source_hash = prepare.file_hash(source)
            with self.subTest(dimension=dimension, node_type=node_type), self.assertRaisesRegex(ValueError, message):
                prepare.prepare(source, self.root / "output")
            self.assertEqual(prepare.file_hash(source), source_hash)
            self.assertFalse((self.root / "output/private/preparation-01/source.mph").exists())

    def test_public_report_does_not_copy_private_identity(self):
        summary = {"assets": {"part_01.step": {"sha256": "a" * 64}}, "format": "step", "topology": {"solids": 2}}
        private = {"original_path": "D:/private/original-case.mph", "model_metadata": {"modelTitle": "private_title"},
                   "original_sha256": "PRIVATE_SOURCE_HASH", "original_unchanged": True,
                   "fresh_model": {"created_from_empty_model": True, "source_model_loaded": False}}
        report = prepare.write_public_report(self.root, summary, private, {"state": "geometry_only"})
        text = json.dumps(report)
        for secret in ("original-case", "private_title", "PRIVATE_SOURCE_HASH"):
            self.assertNotIn(secret, text)
        self.assertFalse(report["mesh_run"])
        self.assertFalse(report["solver_run"])
        self.assertTrue(report["created_from_empty_model"])


class ActualPreparedGeometryTests(unittest.TestCase):
    def test_actual_prepared_neutral_assets_and_proof(self):
        root = APP / "data/geometry-challenge"
        if not (root / "preparation-report.json").is_file():
            self.skipTest("held-out preparation not available")
        report = json.loads((root / "preparation-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "complete")
        self.assertFalse(report["mesh_run"])
        self.assertFalse(report["solver_run"])
        proof = prepare.verify_geometry_only_mph(root / "inputs/geometry_only.mph")
        self.assertFalse(any(proof["forbidden_simulation_node_counts"].values()))
        for name, metadata in report["input_files"].items():
            self.assertEqual(prepare.file_hash(root / "inputs" / name), metadata["sha256"])
        step = (root / "inputs/part_01.step").read_text(encoding="utf-8")
        self.assertNotRegex(step.lower(), r"bracket|contact|spring|thermal|solidmechanics|heattransfer")
        snapshot = json.loads((root / "preparation-neutral-inspection.json").read_text(encoding="utf-8"))
        self.assertTrue(snapshot["parser_detail"]["scan_succeeded"])
        self.assertEqual(snapshot["geometry"]["kernel"]["status"], "imported")
        self.assertEqual(snapshot["physics"], [])
        self.assertEqual(snapshot["geometry"]["kernel"]["topology"]["solids"], 18)

    def test_second_prepared_assets_are_neutral_and_geometry_only(self):
        root = APP / "data/geometry-challenge-02"
        if not (root / "preparation-report.json").is_file():
            self.skipTest("second geometry preparation not available")
        report = json.loads((root / "preparation-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["created_from_empty_model"])
        self.assertFalse(report["fresh_phase_loaded_source_model"])
        self.assertFalse(report["mesh_run"])
        self.assertFalse(report["solver_run"])
        self.assertTrue(report["original_hash_mtime_size_verified_unchanged"])
        self.assertTrue(report["exchange_geometry_checks"]["solid_count_preserved"])
        terms = (*prepare.DEFAULT_FORBIDDEN_TERMS, "micromixer", "微混合器", "laminarflow", "transportofdilutedspecies")
        proof = prepare.verify_geometry_only_mph(root / "inputs/geometry_only.mph", terms)
        self.assertFalse(any(proof["forbidden_simulation_node_counts"].values()))
        for name, metadata in report["input_files"].items():
            self.assertEqual(prepare.file_hash(root / "inputs" / name), metadata["sha256"])
        self.assertFalse(prepare.contains_forbidden_term((root / "inputs/part_01.step").read_text(encoding="utf-8"), terms))
        snapshot = json.loads((root / "preparation-neutral-inspection.json").read_text(encoding="utf-8"))
        self.assertTrue(snapshot["parser_detail"]["scan_succeeded"])
        self.assertEqual(snapshot["geometry"]["kernel"]["status"], "imported")
        self.assertGreater(snapshot["geometry"]["kernel"]["topology"]["solids"], 0)
        summary = report["neutral_geometry"]
        self.assertEqual(summary["cad_dimension"], 3)
        self.assertEqual(summary["unit"], "m")
        self.assertEqual(summary["step_length_unit"], "mm")
        self.assertEqual(summary["topology"], snapshot["geometry"]["kernel"]["topology"])
        actual_bbox = snapshot["geometry"]["kernel"]["bounding_box"]
        # OCC includes different default tolerance padding when importing in M vs MM.
        for endpoint in ("min", "max"):
            for actual, prepared in zip(actual_bbox[endpoint], summary["bounding_box_m"][endpoint]):
                self.assertLessEqual(abs(actual-prepared), 5e-7)
        actual_volume = snapshot["geometry"]["kernel"]["volume"]["value"]
        self.assertLessEqual(abs(actual_volume-summary["volume_sum_m3"]), summary["volume_sum_m3"] * 1e-6)
        self.assertEqual(snapshot["physics"], [])
        self.assertEqual(snapshot["materials"], [])
        self.assertEqual(snapshot["studies"], [])


if __name__ == "__main__":
    unittest.main()
