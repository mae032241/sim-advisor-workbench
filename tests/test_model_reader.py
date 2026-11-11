"""Model evidence, malicious-container bounds, and actual solver-free CLI tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import warnings
import zipfile

from advisor import model_reader as reader
from advisor.build_control import BuildCancelled, BuildControl, build_control


INFO = b'<modelInfo comsolVersion="6.3.0.286" title="Test &amp; model" nodeType="compact"><geometryInfo><geom tag="geom1" dimension="3"/></geometryInfo></modelInfo>'
MODEL = b'''<Model><ModelParam tag="param"><expressions name="Fh" expr="800[N]"/></ModelParam>
<PhysicsList><Physics tag="solid" name="Solid mechanics" op="SolidMechanics">
<selection><explicit dim="3" geom="/geom/geom1"/></selection>
<PhysicsFeature tag="fix1" name="Fixed constraint" op="Fixed"/>
<propertyValue name="p:E" value="0"/></Physics></PhysicsList>
<ModelNodeList><ModelNode tag="comp1" name="Component 1"/></ModelNodeList>
<MaterialList><Material tag="mat1" name="Steel" op="Common"/></MaterialList>
<StudyList><Study tag="std1" name="Study 1"><StudyFeature tag="stat" name="Stationary" op="Stationary"/></Study></StudyList>
<SelectionList><SelectionFeature tag="sel1" name="Faces" op="Explicit"><outputSelection><explicit dim="2" entities="-4,-1,2"/></outputSelection></SelectionFeature></SelectionList>
<SolverSequenceList tag="sol"/></Model>'''
UNAVAILABLE = {"available": False, "mode": "native-mph-container (fallback)", "version": None,
               "detail": "sim-cli unavailable in test", "command": [sys.executable, "-m", "sim"]}
AVAILABLE = {"available": True, "mode": "sim-cli scan", "version": "test",
             "command": [sys.executable, "-m", "sim"]}


class ModelReaderTests(unittest.TestCase):
    def setUp(self):
        # Stay in the authorized workspace even if Windows TEMP is unavailable.
        self.temporary = tempfile.TemporaryDirectory(dir=reader.PROJECT_ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def model(self, name="test.mph", *, model=MODEL, info=INFO):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            if info is not None:
                archive.writestr("modelinfo.xml", info)
            if model is not None:
                archive.writestr("dmodel.xml", model)
        return path

    def inspect_native(self, path):
        with patch.object(reader, "parser_status", return_value=UNAVAILABLE):
            return reader.inspect_model(path)

    def test_native_scoped_values_provenance_and_read_only(self):
        path = self.model()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        result = self.inspect_native(path)
        self.assertEqual(result["parameters"], {"Fh": "800[N]"})
        self.assertEqual(result["physics"][0]["type"], "SolidMechanics")
        self.assertEqual(result["studies"][0]["steps"][0]["type"], "Stationary")
        self.assertEqual(result["selections"][0]["selection_record"]["entities"], "-4,-1,2")
        self.assertEqual(result["physics"][0]["activation"], "unknown")
        self.assertIn("fallback", result["parser"])
        self.assertIn("native XML", result["materials"][0]["source"])
        self.assertTrue(any("SolverSequenceList" in item for item in result["unknowns"]))
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(result["fingerprint"], hashlib.sha256(before[0]).hexdigest())
        json.dumps(result, ensure_ascii=False)

    def test_cli_used_but_unscoped_defaults_not_promoted_to_parameters(self):
        path = self.model()
        payload = {"schema_version": "sim.scan/v1", "engine": {"name": "simparse", "version": "test"},
                   "assets": [{"summary": {"data": {"comsol_version": "6.3.0.286", "title": "A &#38; B",
                                                           "parameters": [{"name": "E", "value": "0"}],
                                                           "physics_tags": ["solid"]}}}]}
        with patch.object(reader, "parser_status", return_value=AVAILABLE), patch.object(reader, "_scan", return_value=payload) as scan:
            result = reader.inspect_model(path)
        scan.assert_called_once()
        self.assertEqual(result["parameters"], {"Fh": "800[N]"})
        self.assertTrue(result["parser_detail"]["scan_succeeded"])
        self.assertEqual(next(x["value"] for x in result["facts"] if x["key"] == "scan.title"), "A & B")
        self.assertIn("sim-cli scan", result["parser"])

    def test_cli_failure_labelled_native_fallback(self):
        with patch.object(reader, "parser_status", return_value=AVAILABLE), patch.object(reader, "_scan", side_effect=reader._CLIError("deadline")):
            result = reader.inspect_model(self.model())
        self.assertIn("fallback", result["parser"])
        self.assertFalse(result["parser_detail"]["scan_succeeded"])
        self.assertTrue(any("deadline" in value for value in result["warnings"]))

    def test_empty_records_remain_unknown(self):
        result = self.inspect_native(self.model(model=b"<Model/>"))
        self.assertEqual(result["parameters"], {})
        self.assertEqual(result["studies"], [])
        self.assertTrue(any("空列表不证明" in value for value in result["unknowns"]))

    def test_malformed_xml_is_not_fabricated(self):
        result = self.inspect_native(self.model(model=b"<Model>"))
        self.assertEqual(result["physics"], [])
        self.assertTrue(result["warnings"])

    def test_wrong_extension_missing_directory_and_nonzip_rejected(self):
        with self.assertRaises(FileNotFoundError):
            reader.inspect_model(self.root / "missing.mph")
        with self.assertRaises(reader.ModelInspectionError):
            reader.inspect_model(self.root)
        with self.assertRaises(reader.ModelInspectionError):
            reader.inspect_model(self.model(name="test.zip"))
        path = self.root / "invalid.mph"
        path.write_bytes(b"not a COMSOL container")
        with self.assertRaises(reader.ModelInspectionError):
            reader.inspect_model(path)

    def test_file_size_checked_before_cli(self):
        path = self.model()
        with patch.object(reader, "MAX_FILE_BYTES", 10), patch.object(reader, "_scan") as scan:
            with self.assertRaisesRegex(reader.ModelInspectionError, "inspection limit"):
                reader.inspect_model(path)
        scan.assert_not_called()

    def test_metadata_size_and_expansion_limits(self):
        path = self.model()
        with patch.object(reader, "MAX_METADATA_BYTES", 20):
            with self.assertRaisesRegex(reader.ModelInspectionError, "metadata"):
                reader.inspect_model(path)
        with patch.object(reader, "MAX_CONTAINER_BYTES", 20):
            with self.assertRaisesRegex(reader.ModelInspectionError, "expanded size"):
                reader.inspect_model(path)

    def test_entity_and_doctype_rejected_before_cli(self):
        malicious = b'<!DOCTYPE Model [<!ENTITY x "malicious">]><Model>&x;</Model>'
        for data in (malicious, malicious.decode().encode("utf-16")):
            with self.subTest(data=data[:10]):
                with patch.object(reader, "_scan") as scan:
                    with self.assertRaisesRegex(reader.ModelInspectionError, "DOCTYPE"):
                        reader.inspect_model(self.model(model=data))
                scan.assert_not_called()

    def test_duplicate_members_rejected(self):
        path = self.model()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("dmodel.xml", MODEL)
        with self.assertRaisesRegex(reader.ModelInspectionError, "duplicate"):
            reader.inspect_model(path)

    def test_item_truncation_is_explicit(self):
        data = ('<Model><ModelNodeList>' + ''.join(f'<ModelNode tag="c{i}"/>' for i in range(5)) + '</ModelNodeList></Model>').encode()
        with patch.object(reader, "MAX_ITEMS", 2):
            result = self.inspect_native(self.model(model=data))
        self.assertEqual(len(result["components"]), 2)
        self.assertTrue(any("ModelNode" in value and "5" in value for value in result["warnings"]))

    def test_subprocess_byte_limit(self):
        with self.assertRaisesRegex(reader._CLIError, "output exceeded"):
            reader._run_bounded([sys.executable, "-c", "import sys; sys.stdout.write('x'*100000)"], max_output=1024)

    def test_subprocess_timeout(self):
        with self.assertRaisesRegex(reader._CLIError, "deadline"):
            reader._run_bounded([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)

    def test_cancelled_model_scan_never_starts_a_process(self):
        marker = self.root / "scan.cancel"; marker.touch()
        with build_control(BuildControl(marker)), patch.object(reader.subprocess, "Popen") as spawn:
            with self.assertRaises(BuildCancelled):
                reader._run_bounded([sys.executable, "-c", "pass"])
        spawn.assert_not_called()

    def test_real_model_scan_wait_is_interruptible_before_its_25_second_deadline(self):
        ready = self.root / "scan.pid"
        marker = self.root / "live.cancel"
        errors = []
        def cancel():
            try:
                until = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < until:
                    time.sleep(.01)
                if not ready.exists():
                    raise AssertionError("Long scan child did not start")
                marker.touch()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=cancel, daemon=True); thread.start()
        started = time.monotonic()
        try:
            with patch.object(reader, "PROJECT_ROOT", self.root), build_control(BuildControl(marker)), self.assertRaises(BuildCancelled):
                reader._run_bounded([sys.executable, "-c", "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)", str(ready)], timeout=25)
        finally:
            thread.join(timeout=4)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertLess(time.monotonic() - started, 4)
        with self.assertRaises(OSError):
            os.kill(int(ready.read_text()), 0)

    def test_cancellation_is_not_converted_into_unavailable_or_native_fallback(self):
        with patch.object(reader, "_run_bounded", side_effect=BuildCancelled()), self.assertRaises(BuildCancelled):
            reader._probe(("cancel-probe-interpreter", "-m", "sim"))
        with patch.object(reader, "parser_status", return_value=AVAILABLE), patch.object(reader, "_scan", side_effect=BuildCancelled()), self.assertRaises(BuildCancelled):
            reader.inspect_model(self.model())

    def test_failed_probe_is_honest(self):
        with patch.object(reader, "_run_bounded", return_value=(1, b"", b"missing")):
            result = reader._probe(("test-nonexistent-python", "-m", "sim"))
        self.assertFalse(result["available"])
        self.assertIn("fallback", result["mode"])


SAMPLES = Path("D:/comsol/COMSOL63/Multiphysics/applications/Structural_Mechanics_Module/Tutorials")


@unittest.skipUnless((SAMPLES / "bracket_basic.mph").is_file() and (SAMPLES / "bracket_thermal.mph").is_file(), "COMSOL tutorial samples unavailable")
class ActualModelTests(unittest.TestCase):
    def test_actual_cli_and_two_official_samples(self):
        status = reader.parser_status()
        if not status["available"]:
            self.skipTest("isolated sim-cli not installed: " + status["detail"])
        basic = reader.inspect_model(SAMPLES / "bracket_basic.mph")
        thermal = reader.inspect_model(SAMPLES / "bracket_thermal.mph")
        for snapshot in (basic, thermal):
            self.assertTrue(snapshot["parser_detail"]["scan_succeeded"])
            self.assertIn("sim-cli", snapshot["parser"])
            self.assertEqual(len(snapshot["fingerprint"]), 64)
            self.assertEqual(snapshot["components"][0]["tag"], "comp1")
            self.assertEqual(snapshot["materials"][0]["label"], "Structural steel")
        self.assertEqual(basic["parameters"]["Fh"], "800[N]")
        self.assertEqual({x["type"] for x in basic["physics"]}, {"SolidMechanics"})
        self.assertEqual({x["type"] for x in thermal["physics"]}, {"SolidMechanics", "HeatTransfer"})
        self.assertEqual(thermal["studies"][0]["steps"][0]["type"], "Stationary")
        self.assertNotIn("E", thermal["parameters"])
        self.assertTrue(any(x["source"].startswith("sim-cli scan") for x in basic["facts"]))


if __name__ == "__main__":
    unittest.main()
