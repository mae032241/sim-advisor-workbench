import copy
import json
import unittest

from advisor.knowledge_views import candidate_view, related_sources_view, source_context_view


class KnowledgeViewTests(unittest.TestCase):
    def test_candidates_keep_structure_and_provenance_without_authoritative_bodies(self):
        code = "FULL_CODE_SENTINEL" * 10000
        source = {"id": "lo-code", "type": "implementation_snippet", "kind": "code", "title": "Mesh", "text": code,
            "excerpt": code, "path": "private-path", "parser_detail": code, "unexpected_raw": code,
            "feature_branch": "mesh", "evidence": [{"root_id": "derived", "relative_path": "code.java", "locator": "lines:1-800", "method": "provided_java_source", "sha256": "a" * 64}],
            "retrieval": {"backend": "native", "generation": "g1", "scope": {"kind": "code", "feature_branch": "mesh", "filter_stage": "before_retrieval"}},
            "data": {"code": code, "model_snapshot": {"nodes": [code]}, "instructions": [code], "raw": code,
                "feature_branch": "mesh", "summary": "Deterministic mesh fragment summary", "function": {"name": "run", "ordinal": 2, "signature": "Model run(Model model)"},
                "objects_created": [{"path": "comp1/mesh1", "type": "Mesh", "status": "observed_source"}], "execution_status": "not_executed"}}
        before = copy.deepcopy(source)
        result = candidate_view(source)
        self.assertEqual(source, before)
        self.assertNotIn("FULL_CODE_SENTINEL", json.dumps(result))
        for key in ["text", "path", "parser_detail", "unexpected_raw"]: self.assertNotIn(key, result)
        self.assertEqual(result["data"]["function"], source["data"]["function"])
        self.assertEqual(result["data"]["objects_created"], source["data"]["objects_created"])
        self.assertEqual(result["retrieval"], source["retrieval"])
        self.assertEqual(result["evidence"][0]["sha256"], "a" * 64)
        self.assertTrue(result["candidate_only"] and result["read_required"])
        result["retrieval"]["scope"]["kind"] = "changed"
        self.assertEqual(source["retrieval"]["scope"]["kind"], "code")
        self.assertLess(len(json.dumps(result)), 5000)

    def test_bounded_arrays_and_legacy_excerpt_do_not_hide_that_candidates_require_a_read(self):
        source = {"id": "manual-a", "kind": "manual", "title": "Documentation", "text": "x" * 100000,
            "data": {"instructions": ["body"], "model_snapshot": {"large": "raw"}, "objects_created": [{"path": "x" * 2000}] * 1000}}
        result = candidate_view(source)
        self.assertEqual(len(result["excerpt"]), 700)
        self.assertEqual(result["data"]["objects_created"][-1], {"remaining_items": 988})
        self.assertNotIn("instructions", result["data"])
        self.assertLess(len(json.dumps(result)), 6500)

    def test_generic_tutorial_heading_keeps_case_identity_and_unverified_scope(self):
        source = {"id": "tutorial-section", "title": "Notes About the COMSOL Implementation", "kind": "manual",
                  "type": "case_context", "package": "cs63-documents", "physics": [],
                  "data": {"tutorial_identity": "models.nsm.parameter_estimation_plasticity",
                           "canonical_tutorial": {"ordinal": 8, "source_sha256": "hash"},
                           "section": "Notes About the COMSOL Implementation", "status": "documented",
                           "model_agreement": "not_verified"}}
        before = copy.deepcopy(source)
        result = candidate_view(source)
        context = result["source_context"]
        self.assertEqual(context["role"], "case_tutorial")
        self.assertEqual(context["case_identity"], source["data"]["tutorial_identity"])
        self.assertEqual(context["case_name"], "parameter_estimation_plasticity")
        self.assertEqual(context["package"], "models.nsm")
        self.assertEqual(context["model_agreement"], "not_verified")
        self.assertEqual(context["applicability"], "not_verified")
        self.assertNotIn("physics", context)
        self.assertNotIn("feature_branch", context)
        self.assertEqual(result["package"], "cs63-documents")
        self.assertEqual(source, before)

    def test_saved_api_and_general_manual_roles_do_not_claim_effective_configuration(self):
        for type_, role in (("saved_configuration", "saved_configuration"), ("api_configuration", "api_observation")):
            source = {"type": type_, "kind": "case", "asset_key": "models:Examples/shielding.mph",
                      "package": "acdc-models", "physics": [],
                      "data": {"feature_branch": "physics", "feature_type": "CurrentConservation",
                               "effective_activation": "not_evaluated", "execution_status": "not_run"}}
            context = candidate_view(source)["source_context"]
            self.assertEqual(context["role"], role)
            self.assertEqual(context["case_identity"], source["asset_key"])
            self.assertEqual(context["case_name"], "shielding")
            self.assertEqual(context["feature_branch"], "physics")
            self.assertEqual(context["effective_activation"], "not_evaluated")
            self.assertEqual(context["execution_status"], "not_run")
            self.assertNotIn("physics", context)
        manual = candidate_view({"kind": "manual", "type": "manual_topic", "title": "Current Conservation"})
        self.assertEqual(manual["source_context"]["role"], "general_manual")
        self.assertNotIn("case_identity", manual["source_context"])

    def test_source_context_is_allowlisted_bounded_and_cannot_copy_nested_bodies(self):
        body = "BODY_SENTINEL" * 10000
        source = {"type": "method_step", "kind": "manual", "text": body, "data": {
            "tutorial_identity": "models.test." + "case" * 10000, "canonical_tutorial": {"text": body},
            "feature_branch": {"nodes": [body]}, "section": "S" * 10000,
            "status": [body], "instructions": [body], "model_snapshot": {"nodes": [body]}},
            "physics": ["Known interface " + "x" * 10000] * 1000}
        context = candidate_view(source)["source_context"]
        self.assertLess(len(json.dumps(context)), 1700)
        self.assertNotIn("BODY_SENTINEL", json.dumps(context))
        self.assertEqual(len(context["physics"]), 6)
        self.assertNotIn("feature_branch", context)
        self.assertNotIn("status", context)
        self.assertNotIn("canonical_tutorial", context)

    def test_related_filter_keeps_alignment_and_unresolved_hints_without_bodies(self):
        values = [{"id": "lo-tutorial", "relation": "documented_by", "status": "candidate_same_branch", "alignment": "not_verified", "text": "body", "data": {"code": "raw"}},
            {"title": "unknown-function", "relation": "requires", "status": "unresolved", "path": "private"}]
        before = copy.deepcopy(values)
        result = related_sources_view(values)
        self.assertEqual(result[0]["alignment"], "not_verified")
        self.assertNotIn("text", result[0]); self.assertNotIn("data", result[0]); self.assertNotIn("path", result[1])
        self.assertNotIn("id", result[1]); self.assertEqual(values, before)

    def test_pinned_source_context_preserves_identity_but_closed_sources_have_no_content_hints(self):
        source = {"id": "lo-pinned", "revision_id": "immutable-revision", "library_id": "closed-library",
                  "title": "Historical source", "kind": "manual", "version": "6.3", "url": "library://closed-library/lo-pinned",
                  "text": "BODY_SENTINEL" * 100000, "excerpt": "BODY_SENTINEL", "coverage": {"details": "BODY_SENTINEL"},
                  "data": {"summary": "BODY_SENTINEL"}}
        before = copy.deepcopy(source)
        opened = source_context_view(source)
        closed = source_context_view(source, available=False)
        self.assertTrue(opened["availability"]["readable"])
        self.assertNotIn("candidate_only", opened)
        self.assertEqual(closed["id"], source["id"])
        self.assertEqual(closed["revision_id"], source["revision_id"])
        self.assertFalse(closed["availability"]["readable"])
        self.assertFalse({"text", "excerpt", "data", "coverage", "source_context"} & set(closed))
        self.assertNotIn("BODY_SENTINEL", json.dumps(closed))
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
