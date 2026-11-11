"""Source syntax and complete bodies, without Java/COMSOL/model execution."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import zipfile

from advisor import case_structure as structure
from advisor import library_model as library


def java(tmp_path, body, *, methods=None, newline="\n"):
    text = "import com.comsol.model.*;\npublic class Example {\n"
    text += methods if methods is not None else " public static Model run() {\n" + body + "\n  return model;\n }\n"
    text += "}\n"
    text = text.replace("\n", newline)
    path = tmp_path / "example.java"
    path.write_bytes(text.encode("utf-8"))
    return path, text


def fragments(tmp_path, body, **kwargs):
    path, source = java(tmp_path, body, **kwargs)
    objects = structure.java_objects(path, exported=True)
    assert "".join(item["data"]["code"] for item in objects) == source
    assert [item["data"]["source_order"] for item in objects] == list(range(1, len(objects)+1))
    for item in objects:
        data = item["data"]
        assert data["execution_status"] == "not_executed"
        assert data["execution_order"] == "not_proven"
        assert data["standalone"] is False
        assert data["code_origin"] == "exported_reconstruction"
        span = data["source_spans"][0]
        assert data["code"] == "".join(source.splitlines(keepends=True)[span["line_start"]-1:span["line_end"]])
        assert data["code"].replace("\r\n", "\n").replace("\r", "\n") in item["text"]
        assert item["evidence"][0]["locator"] == f"lines:{span['line_start']}-{span['line_end']}"
    return objects


INITIAL = '  Model model = ModelUtil.create("Model");\n  model.component().create("comp1", true);\n'


def created(objects, path):
    return next(item for item in objects if any(value["path"] == path for value in item["data"]["objects_created"]))


def test_object_paths_types_and_property_summary_are_source_located(tmp_path):
    objects = fragments(tmp_path, INITIAL + '''  model.component("comp1").geom().create("geom1", 3);
  model.component("comp1").geom("geom1").create("blk1", "Block");
  model.component("comp1").geom("geom1").feature("blk1").set("size", new String[]{"1", "2", "3"});
  model.component("comp1").physics().create("ht", "HeatTransfer", "geom1");
  model.component("comp1").physics("ht").create("temp1", "TemperatureBoundary", 2);
  model.component("comp1").physics("ht").feature("temp1").set("T0", "300[K]");''')
    block = created(objects, "model/component(comp1)/geom(geom1)/feature(blk1)")
    assert block["data"]["feature_branch"] == "geometry"
    assert block["data"]["feature_type"] == "Block"
    assert block["data"]["properties_set"][0]["name"] == "size"
    assert "type=Block" in block["data"]["summary"]
    assert "Property names: size" in block["data"]["summary"]
    heat = created(objects, "model/component(comp1)/physics(ht)")
    geometry = created(objects, "model/component(comp1)/geom(geom1)")
    assert any(ref["kind"] == "physics_geometry_argument" and ref["target_local_id"] == geometry["local_id"] for ref in heat["data"]["references"])
    assert {"type": "requires", "target_local_id": geometry["local_id"], "basis": "explicit_api_receiver_in_same_function", "execution_order": "not_proven"} in heat["relations"]


def test_same_tag_in_different_components_never_crosses_dependency(tmp_path):
    objects = fragments(tmp_path, INITIAL + '''  model.component().create("comp2", true);
  model.component("comp1").geom().create("g", 3);
  model.component("comp2").geom().create("g", 3);
  model.component("comp1").geom("g").create("b", "Block");
  model.component("comp2").geom("g").create("b", "Sphere");''')
    first = created(objects, "model/component(comp1)/geom(g)")
    second = created(objects, "model/component(comp2)/geom(g)")
    block = created(objects, "model/component(comp1)/geom(g)/feature(b)")
    sphere = created(objects, "model/component(comp2)/geom(g)/feature(b)")
    assert any(rel["target_local_id"] == first["local_id"] for rel in block["relations"])
    assert not any(rel["target_local_id"] == second["local_id"] for rel in block["relations"])
    assert any(rel["target_local_id"] == second["local_id"] for rel in sphere["relations"])


def test_long_material_and_mesh_bodies_keep_every_character(tmp_path):
    properties = "\n".join(f'  model.component("comp1").material("mat1").propertyGroup("def").set("property{i}", "' + "x"*140 + '");' for i in range(120))
    objects = fragments(tmp_path, INITIAL + '''  model.component("comp1").material().create("mat1", "Common");
''' + properties + '''
  model.component("comp1").mesh().create("mesh1");
  model.component("comp1").mesh("mesh1").create("ftet1", "FreeTet");
  model.component("comp1").mesh("mesh1").feature("ftet1").set("custom", "on");''')
    material = next(item for item in objects if len(item["data"]["code"]) > 16000)
    assert material["data"]["feature_branch"] == "material"
    assert 'set("property119"' in material["data"]["code"]
    assert material["data"]["index_text_truncated"] is False
    mesh = created(objects, "model/component(comp1)/mesh(mesh1)/feature(ftet1)")
    assert mesh["data"]["feature_type"] == "FreeTet"
    assert mesh["data"]["feature_branch"] == "mesh"


def test_strings_comments_chars_and_text_blocks_are_not_code(tmp_path):
    objects = fragments(tmp_path, INITIAL + r'''  // model.geom().create("fake1", "Block");
  /* model.physics().create("fake2", "HeatTransfer"); */
  String note = "model.material().create(\"fake3\", \"Common\");";
  char brace = '}';
  String block = """
      model.study().create("fake4");
  """;
  model.study().create("real");''')
    paths = [value["path"] for item in objects for value in item["data"]["objects_created"]]
    assert all("fake" not in path for path in paths)
    assert "model/study(real)" in paths


def test_textual_tag_mention_does_not_produce_requires(tmp_path):
    objects = fragments(tmp_path, INITIAL + '''  model.component("comp1").geom().create("g", 3);
  model.component("comp1").geom("g").create("a", "Block");
  model.component("comp1").geom("g").create("b", "Sphere");
  model.component("comp1").geom("g").feature("a").set("description", "b is mentioned as plain text");''')
    sphere = created(objects, "model/component(comp1)/geom(g)/feature(b)")
    setting = next(item for item in objects if '"description"' in item["data"]["code"])
    assert all(rel["target_local_id"] != sphere["local_id"] for rel in setting["relations"])
    assert any(ref.get("path") == "model/component(comp1)/geom(g)/feature(a)" and ref["status"] == "same_function_preceding_source_definition" for ref in setting["data"]["references"])


def test_multi_run_source_and_call_order_are_distinct(tmp_path):
    methods = ''' public static Model run() {
  Model model = ModelUtil.create("Model");
  model.component().create("comp1", true);
  return model;
 }
 public static Model run2(Model model) {
  model.component("comp1").mesh().create("mesh1");
  return model;
 }
 public static void main(String[] args) {
  Model model = run();
  run2(model);
  run2(model);
 }
'''
    objects = fragments(tmp_path, "", methods=methods)
    calls = [call["name"] for item in objects for call in item["data"]["function_calls"]]
    assert calls == ["run", "run2", "run2"]
    run2 = created(objects, "model/component(comp1)/mesh(mesh1)")
    assert run2["data"]["function"]["name"] == "run2"
    component = next(ref for ref in run2["data"]["references"] if ref.get("path") == "model/component(comp1)")
    assert component["status"] == "unresolved"
    caller = next(item for item in objects if any(call["name"] == "run2" for call in item["data"]["function_calls"]))
    assert any(rel["basis"] == "explicit_same_file_function_call" and rel["target_local_id"] == run2["local_id"] for rel in caller["relations"])


def test_unresolved_missing_dynamic_conditional_and_removed_objects(tmp_path):
    objects = fragments(tmp_path, INITIAL + '''  model.component("comp1").geom().create("g", 3);
  if (enabled) {
    model.component("comp1").geom("g").create("maybe", "Block");
  }
  model.component("comp1").geom("g").feature("maybe").set("size", "2");
  model.component("comp1").geom("g").create(prefix + "1", "Block");
  model.component("comp1").geom("g").feature("missing").set("size", "3");
  model.component("comp1").geom("g").feature().remove("maybe");
  model.component("comp1").geom("g").feature("maybe").set("size", "4");''')
    refs = [ref for item in objects for ref in item["data"]["references"] if ref.get("path", "").endswith("feature(maybe)")]
    assert refs and all(ref["status"] == "unresolved" for ref in refs)
    assert any(ref["reason"] == "controlled_execution_not_proven" for ref in refs)
    assert any(item["kind"] == "create_target" for obj in objects for item in obj["data"]["unresolved"])
    assert any(ref.get("path", "").endswith("feature(missing)") and ref["status"] == "unresolved" for item in objects for ref in item["data"]["references"])


def test_model_reassignment_does_not_reuse_old_receiver_definitions(tmp_path):
    objects = fragments(tmp_path, INITIAL + '''  model.component("comp1").geom().create("g", 3);
  model = otherModel;
  model.component("comp1").geom("g").create("b", "Block");''')
    block = created(objects, "model/component(comp1)/geom(g)/feature(b)")
    assert all(ref["status"] == "unresolved" for ref in block["data"]["references"])


def test_same_line_statements_and_crlf_preserve_source_once(tmp_path):
    objects = fragments(tmp_path, INITIAL + '  model.study().create("s"); model.sol().create("sol");', newline="\r\n")
    paths = [value["path"] for item in objects for value in item["data"]["objects_created"]]
    assert "model/study(s)" in paths and "model/sol(sol)" in paths
    assert all(item["data"]["code"].endswith("\r\n") for item in objects)


def test_ids_are_deterministic_and_absent_methods_are_not_invented(tmp_path):
    path, source = java(tmp_path, INITIAL)
    assert structure.java_objects(path, exported=False) == structure.java_objects(path, exported=False)
    path.write_text('// model.geom().create("x", "Block");\nclass Empty {}', encoding="utf-8")
    objects = structure.java_objects(path, exported=False)
    assert len(objects) == 1
    assert not objects[0]["data"]["objects_created"]
    assert objects[0]["data"]["parse_warnings"]


def test_explicit_selection_solver_references_and_opaque_receivers(tmp_path):
    objects = fragments(tmp_path, INITIAL + '''  model.component("comp1").selection().create("sel1", "Explicit");
  model.component("comp1").geom().create("g", 3);
  model.component("comp1").physics().create("p", "HeatTransfer", "g");
  model.component("comp1").physics("p").selection().named("sel1");
  model.component("comp1").physics("p").prop("TransientBehavior").set("mode", "Transient");
  model.study().create("std1");
  model.sol().create("s1");
  model.sol("s1").attach("std1");
  model.component("comp1").unknownAccessor("opaque").set("unknownProperty", "value");''')
    selection = created(objects, "model/component(comp1)/selection(sel1)")
    refs = [ref for obj in objects for ref in obj["data"]["references"]]
    assert any(ref["kind"] == "named_selection" and ref["target_local_id"] == selection["local_id"] for ref in refs)
    assert any(ref["kind"] == "solver_study_attachment" and ref["status"] == "same_function_preceding_source_definition" for ref in refs)
    properties = [item for obj in objects for item in obj["data"]["properties_set"]]
    assert next(item for item in properties if item["name"] == "mode")["path"].endswith("/prop(TransientBehavior)")
    assert next(item for item in properties if item["name"] == "unknownProperty")["path"] is None
    assert any(item["kind"] == "unknown_intermediate_receiver" for obj in objects for item in obj["data"]["unresolved"])


def test_tutorial_source_context_is_an_external_reference_without_body_copies(tmp_path):
    model = tmp_path / "sample.mph"
    with zipfile.ZipFile(model, "w") as out:
        out.writestr("modelinfo.xml", '<modelInfo comsolVersion="6.3" title="Fixture"/>')
        out.writestr("dmodel.xml", '<Model tag="m"><Study tag="s1"/></Model>')
    tutorial = tmp_path / "tutorial.html"
    tutorial.write_text('''<html><title>Source context</title><body>
<div class="Head2"><a name="intro">Introduction</a></div>
<div class="Body_text"><a name="objective">Documented objective from the source.</a></div>
<div class="Head2">Model Definition</div>
<div class="Body_text"><a name="definition">Documented physical definition; agreement not established.</a></div>
<div class="Head2">Modeling Instructions</div>
<div class="Head3"><a name="geometry">Geometry</a></div>
<div class="Body_text"><a name="step">Create the documented geometry.</a></div>
</body></html>''', encoding="utf-8")
    with patch("advisor.model_reader.parser_status", return_value={"available": False, "detail": "Fixture"}):
        result = library.extract_case(model, docs_root=tutorial, artifact_dir=tmp_path / "artifacts", deep=False)
    assert not any(obj["type"] in {"case_context", "method_step"} for obj in result["objects"])
    reference = result["objects"][0]["data"]["tutorial_refs"][0]
    assert reference["relation_status"] == "reference_not_verified_model_agreement"
    assert any(item["locator"] == "html:#intro" for item in reference["chapters"])
    assert any(item["category"] == "geometry" for item in reference["chapters"])
    body = str(result)
    assert "Documented objective from the source." not in body
    assert "Create the documented geometry." not in body
    assert not any("annotation" in obj["data"] for obj in result["objects"])


def test_python_structure_version_does_not_invalidate_native_api_cache(tmp_path):
    home = tmp_path / "comsol"
    binary = home / "java/win64/jre/bin"
    binary.mkdir(parents=True)
    for name in ("java.exe", "javac.exe"):
        (binary / name).write_bytes(b"fixture executable marker")
    output = tmp_path / "cache"
    output.mkdir()
    source = tmp_path / "input.mph"
    source.write_bytes(b"fixture")
    payload = {"schema": "mph-api-state/v1", "read_contract": library.API_READ_CONTRACT, "read_errors": 0,
               "nodes": [], "geometry_run": False, "mesh_run": False, "solver_run": False, "defaults_generated": False}
    def runner(command, *_):
        if command[0] != str(binary / "javac.exe"):
            java_command = json.loads(command[3])
            Path(java_command[-2]).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")
    with patch.object(library, "_comsol_root", return_value=home), patch.object(library, "run_text", side_effect=runner) as run:
        assert not library._deep_state(source, output, "same-source")[2]["cached"]
        assert run.call_count == 2
        with patch.object(library, "EXTRACTOR_VERSION", "structure-only-next"):
            assert library._deep_state(source, output, "same-source")[2]["cached"]
        assert run.call_count == 2

