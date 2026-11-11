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
        self.assertFalse({"text", "excerpt", "data", "coverage"} & set(closed))
        self.assertNotIn("BODY_SENTINEL", json.dumps(closed))
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
