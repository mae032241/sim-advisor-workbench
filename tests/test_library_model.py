"""Shared case extraction: locatable state, real procedures, no fake code."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from advisor import library_model as library
from advisor.build_control import BuildCancelled


XML = '''<Model tag="Model1"><modelTitle>modeldescr_test/model</modelTitle>
<ModelParam tag="param"><expressions name="x" expr="1[mm]"/></ModelParam>
<PhysicsList tag="physics"><Physics tag="ht" name="Heat transfer" op="HeatTransfer"><PhysicsFeatureList tag="feature">
<PhysicsFeature tag="hf1" op="HeatFluxBoundary" name="Convection"><param param="HeatFluxType" value="1|1,'ConvectiveHeatFlux'"/>
<param param="h" value="1|1,'10'"/><param param="q0_input" value="1|1,'0'"/>
<selection><explicit geom="geom1" dim="2" entities="-5,-1,1,3"/></selection></PhysicsFeature></PhysicsFeatureList></Physics></PhysicsList>
<MultiphysicsCouplingList tag="multiphysics"><MultiphysicsCoupling tag="te1" op="ThermalExpansion"><param param="Heat_physics" value="ht"/><param param="Solid_physics" value="solid"/></MultiphysicsCoupling></MultiphysicsCouplingList>
<StudyList tag="study"><Study tag="std1"><StudyFeature tag="stat" op="Stationary"/></Study></StudyList>
<SolverSequenceList tag="sol"><SolverSequence tag="sol1"><study>std1</study><SolverFeatureList tag="feature"><SolverFeature tag="s1" op="Stationary"><propertyValue name="rtol" value="0.001"/></SolverFeature></SolverFeatureList></SolverSequence></SolverSequenceList>
<History tag="hist"><HistoryBlock>not interpreted as original GUI chronology</HistoryBlock></History></Model>'''
HTML = '''<html><head><title>Documented test</title></head><body>
<div class="Body_text">Application Library path: Heat_Module/Tutorials/model</div>
<div class="Head2"><a name="instructions">Modeling Instructions</a></div>
<div class="Head3">Geometry</div><div class="Body_text"><a name="geometry">Import the supplied geometry.</a></div>
<div class="Head3">Heat Transfer</div><div class="Head4">Heat Flux</div><div class="Num_list"><a name="hf">Set h to 10 W/(m^2*K).</a></div>
<div class="Head3">Study</div><div class="Body_text"><a name="study">Compute only when inputs are verified.</a></div>
<a href="manual.pdf">PDF</a><a href="../outside.txt">outside</a>
</body></html>'''
JAVA = '''import com.comsol.model.*;
public class model {
 public static Model run() {
  Model model = ModelUtil.create("Model");
  model.component().create("comp1", true);
  model.component("comp1").physics().create("ht", "HeatTransfer", "geom1");
  model.component("comp1").physics("ht").feature("hf1").set("h", "10");
  model.study().create("std1");
  model.sol().create("sol1");
  return model;
 }
}'''


class LibraryModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=library.PROJECT_ROOT)
        self.root = Path(self.temporary.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.artifacts = self.root / "artifacts"

    def tearDown(self):
        self.temporary.cleanup()

    def mph(self, path=None, title="modeldescr_test/model", xml=XML):
        path = path or self.root / "model.mph"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("modelinfo.xml", f'<modelInfo comsolVersion="6.3.0.285" title="Model" modelTitle="{title}"/>')
            z.writestr("dmodel.xml", xml)
        return path

    def tutorial(self, package="com.comsol.help.models.test.model", content=HTML, name="model"):
        directory = self.docs / package
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name + ".html")
        path.write_text(content, encoding="utf-8")
        (directory / "manual.pdf").write_bytes(b"test PDF attachment, not parsed")
        return path

    def extract(self, path, **kwargs):
        return library.extract_case(path, docs_root=self.docs, artifact_dir=self.artifacts, deep=False, **kwargs)

    def test_raw_scope_coupling_solver_and_unused_boundary_values_retained(self):
        path = self.mph()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        result = self.extract(path)
        raw_path = next(a["path"] for a in result["assets"] if a["role"] == "saved_xml_snapshot")
        raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        heat = next(n for n in raw["nodes"] if n["tag"] == "hf1")
        self.assertIn('Physics[@tag="ht"]', heat["path"])
        self.assertEqual(heat["effective_activation"], "not_evaluated")
        self.assertEqual(heat["selection_resolution"], "raw_encoding_not_resolved")
        self.assertIn("-5,-1,1,3", heat["selection_records"][0]["raw_xml"])
        values = {p["name"]: p for p in heat["normalized_properties"]["properties"]}
        self.assertEqual(values["h"]["value"], "10")
        self.assertEqual(values["HeatFluxType"]["value"], "ConvectiveHeatFlux")
        solver = next(n for n in raw["nodes"] if n["tag"] == "s1")
        solver_values = {p["name"]: p for p in solver["normalized_properties"]["properties"]}
        self.assertEqual(solver_values["rtol"]["value"], 0.001)
        self.assertIn("normalized_selections", heat)
        self.assertEqual(raw["coverage"]["normalization"]["default_vs_explicit"], "not_evaluated")
        self.assertTrue(any(p["attributes"].get("param") == "q0_input" for p in heat["properties"]))
        self.assertEqual(result["coverage"]["sections"]["couplings"]["saved_nodes"], 1)
        self.assertEqual(result["coverage"]["sections"]["solvers"]["saved_nodes"], 2)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(result["model_snapshot"]["fingerprint"], hashlib.sha256(before[0]).hexdigest())

    def test_external_tutorial_sections_are_refs_and_missing_code_honest(self):
        path = self.mph()
        html = self.tutorial()
        with patch.object(library, "_tutorial_objects", side_effect=AssertionError("No model-owned tutorial bodies")), \
             patch("advisor.case_metadata.tutorial_context_objects", side_effect=AssertionError("No model-owned tutorial bodies")):
            result = self.extract(path)
        self.assertFalse(any(o["type"] in {"method_step", "case_context"} for o in result["objects"]))
        reference = result["objects"][0]["data"]["tutorial_refs"][0]
        self.assertEqual(reference["source_uri"], html.as_uri())
        self.assertEqual(reference["document_identifier"], "models.test.model")
        self.assertEqual(reference["relation_status"], "reference_not_verified_model_agreement")
        self.assertEqual([chapter["title"] for chapter in reference["chapters"]],
                         ["Modeling Instructions", "Geometry", "Heat Transfer", "Heat Flux", "Study"])
        self.assertNotIn("Set h to 10", json.dumps(result))
        self.assertEqual(result["coverage"]["implementation"]["status"], "missing")
        self.assertFalse(any(o["type"] == "implementation_snippet" for o in result["objects"]))
        self.assertFalse(any(a["role"] in {"tutorial_pdf", "associated_tutorial_html"} for a in result["assets"]))

    def test_renamed_user_copy_uses_identifier_only_as_candidate(self):
        original = self.mph()
        copied = self.root / "user_modified_name.mph"
        shutil.copyfile(original, copied)
        self.tutorial()
        official, user = self.extract(original), self.extract(copied)
        self.assertEqual(official["model_snapshot"]["fingerprint"], user["model_snapshot"]["fingerprint"])
        self.assertEqual(user["coverage"]["tutorial"]["status"], "candidate_matched")
        self.assertEqual(user["coverage"]["tutorial"]["settings_agreement"], "not_verified")
        self.assertEqual([o["local_id"] for o in official["objects"]], [o["local_id"] for o in user["objects"]])

    def test_same_name_heat_sink_requires_complete_application_path(self):
        applications = self.root / "applications"
        path = self.mph(applications / "Heat_Module" / "Tutorials" / "heat_sink.mph", title="modeldescr_cfd/heat_sink")
        self.tutorial("com.comsol.help.models.heat.heat_sink", HTML.replace("Heat_Module/Tutorials/model", "Heat_Module/Tutorials/heat_sink"), "heat_sink")
        wrong = self.tutorial("com.comsol.help.models.cfd.heat_sink", HTML.replace("Heat_Module/Tutorials/model", "CFD_Module/Tutorials/heat_sink"), "heat_sink")
        with patch.dict(os.environ, {"COMSOL_APPLICATIONS": str(applications)}):
            result = self.extract(path)
        match = result["coverage"]["tutorial"]
        self.assertEqual(match["method"], "application_library_path_exact")
        self.assertEqual(match["application_library_path"], "Heat_Module/Tutorials/heat_sink")
        self.assertNotEqual(match["path"], str(wrong))

    def test_provided_java_grouped_and_never_marked_executed(self):
        path = self.mph()
        path.with_suffix(".java").write_text(JAVA, encoding="utf-8")
        result = self.extract(path)
        snippets = [o for o in result["objects"] if o["type"] == "implementation_snippet"]
        self.assertTrue(snippets)
        self.assertLessEqual(len(snippets), 12)
        source_lines = JAVA.splitlines()
        for snippet in snippets:
            self.assertEqual(snippet["data"]["code_origin"], "provided_source")
            self.assertEqual(snippet["data"]["execution_status"], "not_executed")
            self.assertFalse(snippet["data"]["standalone"])
            for span in snippet["data"]["source_spans"]:
                expected = "\n".join(source_lines[span["line_start"]-1:span["line_end"]])
                self.assertIn(expected, snippet["text"])

    def test_model_limits_are_explicit_without_owning_the_external_tutorial(self):
        path = self.mph()
        self.tutorial()
        with patch.object(library, "MAX_TUTORIAL_STEPS", 2), patch.object(library, "MAX_PROPERTIES", 1):
            result = self.extract(path)
        self.assertFalse(any(o["type"] == "method_step" for o in result["objects"]))
        self.assertEqual(result["coverage"]["tutorial"]["passage_count"], 0)
        self.assertTrue(result["coverage"]["raw_xml"]["truncated"])
        self.assertFalse(any(a["role"] == "associated_tutorial_html" for a in result["assets"]))
        self.assertTrue(result["objects"][0]["data"]["tutorial_refs"])

    def test_deep_failure_does_not_fake_api_or_code_and_input_changed_rejected(self):
        path = self.mph()
        with patch.object(library, "_deep_state", return_value=(None, [], {"status": "failed", "reason": "license unavailable", "node_count": 0})):
            result = library.extract_case(path, docs_root=self.docs, artifact_dir=self.artifacts, deep=True)
        self.assertEqual(result["coverage"]["deep_api"]["status"], "failed")
        self.assertFalse(result["coverage"]["validation"]["loaded_in_comsol"])
        self.assertEqual(result["coverage"]["implementation"]["status"], "missing")
        original_stat = path.stat()
        def mutate(*_, **_kwargs):
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 100_000_000))
            return None, None, {"status": "missing"}, []
        with patch.object(library, "_tutorial", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed during"):
                self.extract(path)

    def test_preview_is_not_mistaken_for_compact_or_missing_attachment(self):
        path = self.root / "preview.mph"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("modelinfo.xml", '<modelInfo comsolVersion="6.3" nodeType="preview" compactFileSize="2053834" modelTitle="modeldescr_test/model"/>')
            z.writestr("model.xml", '<Model/>')
            z.writestr("preview", b"preview marker")
        self.tutorial()
        with patch.object(library, "_deep_state") as deep:
            result = library.extract_case(path, docs_root=self.docs, artifact_dir=self.artifacts, deep=True)
        deep.assert_not_called()
        self.assertEqual(result["coverage"]["source_container"]["status"], "preview_only")
        self.assertEqual(result["coverage"]["deep_api"]["status"], "preview_only")
        self.assertFalse(result["coverage"]["validation"]["loaded_in_comsol"])
        self.assertIn("PREVIEW ONLY", result["objects"][0]["text"])
        self.assertEqual(result["coverage"]["implementation"]["status"], "missing")

    def test_user_without_tutorial_keeps_api_summary_and_real_export_only(self):
        path = self.mph(self.root / "user_copy.mph")
        exported = self.root / "model_export.java"
        exported.write_text(JAVA, encoding="utf-8")
        api_file = self.root / "api.json"
        node = {"path": "model/component(comp1)/physics(ht)/feature(hf1)", "type": "HeatFluxBoundary",
                "selection": {"dim": {"value": 2}}, "properties": [{"name": "h", "value": "10", "read_status": "observed"}]}
        api = {"nodes": [node], "warnings": [], "read_errors": 0}
        api_file.write_text(json.dumps(api), encoding="utf-8")
        assets = [library._asset(api_file, "api_snapshot", derived=True), library._asset(exported, "exported_reconstruction_java", derived=True)]
        with patch.object(library, "_deep_state", return_value=(api, assets, {"status": "observed", "node_count": 1})):
            result = library.extract_case(path, docs_root=self.root / "no-doc-binding", artifact_dir=self.artifacts, deep=True)
        self.assertFalse(any(o["type"] == "method_step" for o in result["objects"]))
        self.assertIn('h="10"', result["objects"][0]["text"])
        self.assertIn("not proven effective", result["objects"][0]["text"])
        self.assertTrue(any(o["type"] == "implementation_snippet" and o["data"]["code_origin"] == "exported_reconstruction" for o in result["objects"]))
        self.assertTrue(any(e["asset_path"] == str(api_file) for e in result["objects"][0]["evidence"]))

    def test_coverage_keeps_partial_selection_and_limits_even_with_zero_reported_errors(self):
        api = {"nodes": [{"path": "model/geom(g)", "properties": [
            {"name": "input", "value_type": "Selection", "read_status": "partial"}]}], "read_errors": 0,
            "truncated": True, "truncation_reasons": {"array_items": 1}, "limits": {"max_array_items": 5000},
            "read_coverage": {"properties": {"declared": 8}, "selections": {"unresolved": 1}, "traversal": {"unresolved": 0}}}
        coverage = library._api_coverage(api, cached=False)
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["properties"], {"total": 1, "declared": 8, "observed": 0, "partial": 1, "unresolved": 0})
        self.assertEqual(coverage["read_errors"], 1)
        self.assertEqual(coverage["truncation_reasons"], {"array_items": 1})
        self.assertIn("not evaluated", coverage["scope"])

    def test_nonfinite_json_and_prohibited_flags_are_not_accepted_from_cache(self):
        filename = self.root / "api-state.json"
        api = {"schema": "mph-api-state/v1", "read_contract": library.API_READ_CONTRACT, "read_errors": 0,
               "nodes": [], "geometry_run": False, "mesh_run": False, "solver_run": False, "defaults_generated": False}
        for value in [dict(api, nodes=[{"properties": [{"value": float("nan")}]}]), dict(api, solver_run=True), dict(api, read_contract="old")]:
            filename.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                library._read_api_state(filename)
        filename.write_text(json.dumps(api), encoding="utf-8")
        self.assertEqual(library._read_api_state(filename), api)

    def deep_environment(self):
        home = self.root / "comsol"
        binary = home / "java/win64/jre/bin"
        binary.mkdir(parents=True)
        for name in ("java.exe", "javac.exe"):
            (binary / name).write_bytes(b"test executable marker")
        source = self.mph()
        output = self.root / "deep"
        payload = {"schema": "mph-api-state/v1", "read_contract": library.API_READ_CONTRACT, "read_errors": 0,
                   "nodes": [], "geometry_run": False, "mesh_run": False, "solver_run": False, "defaults_generated": False}
        return home, binary, source, output, payload

    def test_version_and_read_contract_invalidate_api_cache_without_running_comsol(self):
        home, binary, source, output, payload = self.deep_environment()
        before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
        copied = output / "runtime/input.mph"
        def runner(command, *_):
            self.assertTrue(copied.is_file())
            self.assertEqual(copied.read_bytes(), before[0])
            if command[0] != str(binary / "javac.exe"):
                java = json.loads(command[3])
                self.assertEqual(Path(java[-3]), copied)
                Path(java[-2]).write_text(json.dumps(payload), encoding="utf-8")
                Path(java[-1]).write_text(JAVA, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(library, "_comsol_root", return_value=home), patch.object(library, "run_text", side_effect=runner) as run:
            first = library._deep_state(source, output, "source-hash")[2]
            self.assertFalse(copied.exists())
            cached = library._deep_state(source, output, "source-hash")[2]
            self.assertFalse(first["cached"])
            self.assertTrue(cached["cached"])
            self.assertEqual(run.call_count, 2)
            with patch.object(library, "API_EXTRACTOR_VERSION", "test-new-version"):
                refreshed = library._deep_state(source, output, "source-hash")[2]
                self.assertFalse(refreshed["cached"])
                self.assertEqual(run.call_count, 4)
            manifest = json.loads((output / "api-manifest.json").read_text())
            self.assertEqual(manifest["read_contract"], library.API_READ_CONTRACT)
            self.assertEqual(manifest["extractor_version"], "test-new-version")
            self.assertTrue((output / "api-state.json").is_file())
            self.assertTrue((output / "model_export.java").is_file())
            self.assertFalse(copied.exists())
        self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode))

    def test_temporary_input_removed_after_failure_timeout_or_cancellation(self):
        home, binary, source, output, _ = self.deep_environment()
        before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
        for outcome in ("compile_failure", "read_failure", "timeout", "cancelled"):
            with self.subTest(outcome=outcome):
                destination = output / outcome
                copied = destination / "runtime/input.mph"
                def runner(command, *_):
                    self.assertEqual(copied.read_bytes(), before[0])
                    (copied.parent / "classes/ReadMph.class").write_bytes(b"compiled")
                    if command[0] == str(binary / "javac.exe"):
                        return subprocess.CompletedProcess(command, int(outcome == "compile_failure"), "", "")
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(command, 1)
                    if outcome == "cancelled":
                        raise BuildCancelled()
                    return subprocess.CompletedProcess(command, 2, "", "")
                with patch.object(library, "_comsol_root", return_value=home), patch.object(library, "run_text", side_effect=runner):
                    if outcome == "cancelled":
                        with self.assertRaises(BuildCancelled):
                            library._deep_state(source, destination, "source-hash")
                    else:
                        data, assets, coverage = library._deep_state(source, destination, "source-hash")
                        self.assertIsNone(data)
                        self.assertEqual(assets, [])
                        self.assertEqual(coverage["status"], "failed")
                self.assertFalse(copied.exists())
                self.assertFalse((copied.parent / "classes/ReadMph.class").exists())
                self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode))

    def test_partial_temporary_input_removed_when_copy_fails(self):
        home, _, source, output, _ = self.deep_environment()
        before = source.read_bytes()
        def fail_copy(_, destination):
            Path(destination).write_bytes(b"partial copy")
            raise OSError("copy failed")
        with patch.object(library, "_comsol_root", return_value=home), patch.object(library.shutil, "copyfile", side_effect=fail_copy), patch.object(library, "run_text") as run:
            coverage = library._deep_state(source, output, "source-hash")[2]
        run.assert_not_called()
        self.assertEqual(coverage["status"], "failed")
        self.assertIn("copy failed", coverage["reason"])
        self.assertFalse((output / "runtime/input.mph").exists())
        self.assertEqual(source.read_bytes(), before)

    def test_cleanup_failure_reports_warning_without_masking_result_or_cancellation(self):
        home, binary, source, output, payload = self.deep_environment()
        unlink = Path.unlink
        for outcome in ("success", "failed", "cancelled"):
            with self.subTest(outcome=outcome):
                destination = output / outcome
                copied = destination / "runtime/input.mph"
                cancellation = BuildCancelled()
                def runner(command, *_):
                    if command[0] == str(binary / "javac.exe"):
                        return subprocess.CompletedProcess(command, 0, "", "")
                    if outcome == "cancelled":
                        raise cancellation
                    if outcome == "success":
                        (destination / "api-state.json").write_text(json.dumps(payload), encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0 if outcome == "success" else 2, "", "")
                def fail_unlink(path, *args, **kwargs):
                    if path == copied:
                        raise PermissionError("temporary input still locked")
                    return unlink(path, *args, **kwargs)
                with patch.object(library, "_comsol_root", return_value=home), patch.object(library, "run_text", side_effect=runner), patch.object(Path, "unlink", fail_unlink):
                    if outcome == "cancelled":
                        with self.assertRaises(BuildCancelled) as raised:
                            library._deep_state(source, destination, "source-hash")
                        self.assertIs(raised.exception, cancellation)
                        self.assertTrue(any("Temporary MPH input could not be removed" in note for note in cancellation.__notes__))
                    else:
                        coverage = library._deep_state(source, destination, "source-hash")[2]
                        self.assertEqual(coverage["status"], "observed" if outcome == "success" else "failed")
                        self.assertIn("temporary input still locked", coverage["cleanup_warning"])
                        if outcome == "failed":
                            self.assertIn("Standalone COMSOL extraction failed", coverage["reason"])
                self.assertTrue(copied.exists())
                self.assertTrue(copied.stat().st_mode & 0o200)

    def test_runtime_input_cannot_alias_the_original_source(self):
        home, _, _, output, _ = self.deep_environment()
        source = self.mph(output / "runtime/input.mph")
        before = (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode)
        with patch.object(library, "_comsol_root", return_value=home), patch.object(library, "run_text") as run:
            coverage = library._deep_state(source, output, "source-hash")[2]
        run.assert_not_called()
        self.assertEqual(coverage["status"], "failed")
        self.assertIn("must differ", coverage["reason"])
        self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns, source.stat().st_mode))


GETTER_PROBE = r'''
import java.util.*;
public class GetterProbe {
  public static class ObjectSelection {
    public String named(){return "configured_selection";}
    public String[] objects(){return new String[]{"blk1"};}
    public int[] entities(String name){return new int[]{3,8};}
  }
  public static class Node {
    public String[] properties(){return new String[]{"filename","rows","input","button","broken","future"};}
    public String getValueType(String name){
      if(name.equals("filename"))return "File";
      if(name.equals("rows"))return "DoubleRowMatrix";
      if(name.equals("input"))return "Selection";
      if(name.equals("button"))return "None";
      if(name.equals("future"))return "FutureType";
      return "String";
    }
    public String getString(String name){
      if(name.equals("broken"))throw new IllegalStateException("deliberate API getter failure");
      return "unchanged-file-path.dat";
    }
    public double[][] getDoubleMatrix(String name){return new double[][]{{1,Double.NaN},{Double.POSITIVE_INFINITY,Double.NEGATIVE_INFINITY}};}
    public ObjectSelection selection(String name){return new ObjectSelection();}
    public void run(){throw new AssertionError("must never execute geometry or solver");}
  }
  public static class Collection {
    public String[] tags(){return new String[]{"sel1"};}
  }
  public static class Container {
    public Collection selection(){return new Collection();}
  }
  public static void main(String[] args){
    List<Object> properties=ReadMph.properties(new Node());
    Object collection=ReadMph.selection(new Container());
    System.out.println(ReadMph.json(ReadMph.map("properties",properties,"collection",collection,"property_errors",ReadMph.propertyErrors,
      "selection_errors",ReadMph.selectionErrors,"non_finite_values",ReadMph.nonFinite,"truncated",ReadMph.truncated)));
  }
}
'''


class JavaGetterTests(unittest.TestCase):
    """Compile the actual reflection reader against API-shaped fake objects."""
    @classmethod
    def setUpClass(cls):
        binary = library._comsol_root() / "java/win64/jre/bin"
        cls.java, javac = binary / "java.exe", binary / "javac.exe"
        if not cls.java.is_file() or not javac.is_file():
            raise unittest.SkipTest("JDK unavailable; actual extraction is separately opt-in")
        cls.temporary = tempfile.TemporaryDirectory(dir=library.PROJECT_ROOT)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        shutil.copyfile(library.JAVA_SOURCE, cls.root / "ReadMph.java")
        (cls.root / "GetterProbe.java").write_text(GETTER_PROBE, encoding="utf-8")
        subprocess.run([str(javac), "-encoding", "UTF-8", "-d", str(cls.root), str(cls.root / "ReadMph.java"), str(cls.root / "GetterProbe.java")],
                       check=True, capture_output=True, timeout=25)
        run = subprocess.run([str(cls.java), "-cp", str(cls.root), "GetterProbe"], check=True, capture_output=True, timeout=10)
        def reject(value):
            raise AssertionError(f"Non-standard JSON token: {value}")
        cls.result = json.loads(run.stdout, parse_constant=reject)

    def test_special_types_use_the_correct_api_and_keep_selection_scope(self):
        properties = {p["name"]: p for p in self.result["properties"]}
        self.assertEqual(properties["filename"]["value"], "unchanged-file-path.dat")
        self.assertEqual(properties["filename"]["getter"], "getString")
        self.assertEqual(properties["rows"]["getter"], "getDoubleMatrix")
        selection = properties["input"]["value"]
        self.assertEqual(selection["named"]["value"], "configured_selection")
        self.assertEqual(selection["objects"]["value"], ["blk1"])
        self.assertEqual(selection["object_entities"][0]["entities"], [3, 8])
        self.assertEqual(selection["effective_membership"], "not_validated")
        self.assertFalse(selection["geometry_built_by_extractor"])
        self.assertEqual(self.result["collection"]["status"], "not_applicable")
        self.assertEqual(self.result["selection_errors"], 0)

    def test_real_getter_failures_unwrap_reflection_and_none_is_never_faked(self):
        properties = {p["name"]: p for p in self.result["properties"]}
        self.assertEqual(properties["broken"]["error_type"], "IllegalStateException")
        self.assertEqual(properties["broken"]["read_error"]["reason"], "api_getter_failed")
        self.assertEqual(properties["broken"]["read_error"]["origin"], "api_invocation")
        self.assertIn("deliberate API getter failure", properties["broken"]["read_error"]["message"])
        self.assertEqual(properties["button"]["read_error"]["reason"], "no_value_type")
        self.assertEqual(properties["button"]["read_error"]["origin"], "extractor_type_dispatch")
        self.assertNotIn("value", properties["button"])
        self.assertEqual(properties["future"]["read_error"]["reason"], "unsupported_value_type")
        self.assertEqual(self.result["property_errors"], 3)

    def test_nonfinite_values_are_lossless_tagged_facts_in_strict_json(self):
        rows = next(p for p in self.result["properties"] if p["name"] == "rows")["value"]
        self.assertEqual(rows[0][1], {"numeric_status": "non_finite", "representation": "NaN", "encoding": "tagged_api_number"})
        self.assertEqual([rows[1][i]["representation"] for i in range(2)], ["Infinity", "-Infinity"])
        self.assertEqual(self.result["non_finite_values"], 3)


class ActualLibraryModelTests(unittest.TestCase):
    def test_actual_bracket_saved_coupling_solver_and_tutorial(self):
        path = Path("D:/comsol/COMSOL63/Multiphysics/applications/Structural_Mechanics_Module/Tutorials/bracket_thermal.mph")
        if not path.is_file():
            self.skipTest("official sample unavailable")
        with tempfile.TemporaryDirectory(dir=library.PROJECT_ROOT) as temp:
            result = library.extract_case(path, artifact_dir=Path(temp), deep=False)
            self.assertEqual(result["coverage"]["tutorial"]["method"], "application_library_path_exact")
            self.assertEqual(result["coverage"]["sections"]["couplings"]["saved_nodes"], 1)
            self.assertEqual(result["coverage"]["sections"]["solvers"]["saved_nodes"], 38)
            self.assertFalse(any(o["type"] == "method_step" for o in result["objects"]))
            reference = result["objects"][0]["data"]["tutorial_refs"][0]
            self.assertGreater(len(reference["chapters"]), 5)
            self.assertTrue(any("Heat Transfer" in chapter["title"] for chapter in reference["chapters"]))


if __name__ == "__main__":
    unittest.main()
