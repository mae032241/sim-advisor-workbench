"""Build an administrator selection from explicit user-owned choices.

Reads existing catalog/compact SQLite files only. Does not parse MPH, run COMSOL,
embed, enable libraries, or rebuild indexes. No evaluation cases are bundled.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from advisor.catalog_storage import register_catalog_functions


def validate_choices(value):
    """Validate user metadata; exact library/asset identities are never inferred."""
    if not isinstance(value, dict) or set(value) != {"id", "name", "description", "choices"}:
        raise ValueError("Choices need exactly id, name, description and choices")
    for key, limit in (("id", 128), ("name", 200), ("description", 2000)):
        item = value[key]
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            raise ValueError(f"Choices {key} must be a nonempty string of at most {limit} characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value["id"]):
        raise ValueError("Choices id must contain only letters, digits, dots, underscores or hyphens")
    entries = value["choices"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise ValueError("choices must be an array of 1 to 1000 explicit entries")
    fields = {"library_id": 256, "asset_key": 8192, "direction": 200, "selection_reason": 2000}
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != set(fields):
            raise ValueError("Each choice needs exactly library_id, asset_key, direction and selection_reason")
        for key, limit in fields.items():
            item = entry[key]
            if (not isinstance(item, str) or not item.strip() or len(item) > limit
                    or re.search(r"[\x00-\x1f\x7f]", item)):
                raise ValueError(f"Choice {key} must be a nonempty string of at most {limit} characters without control characters")
        identity = (entry["library_id"], entry["asset_key"])
        if identity in seen:
            raise ValueError("Duplicate library_id/asset_key choice")
        seen.add(identity)
    return value


def load_choices(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Choices JSON exceeds 1 MiB")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Choices JSON has a duplicate object key")
            result[key] = value
        return result
    return validate_choices(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object))


def candidates(libraries_root: Path):
    with sqlite3.connect((libraries_root / "catalog.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        register_catalog_functions(db)
        rows = db.execute("""SELECT r.payload FROM revisions r
          JOIN case_members m ON m.citation_id=r.id AND m.local_id='model-case'
          JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id""")
        result = []
        for (raw,) in rows:
            obj = json.loads(raw)
            if obj.get("coverage", {}).get("source_container", {}).get("status") == "preview_only":
                continue
            compact = obj.get("data", {}).get("compact_case", {}).get("path")
            if compact:
                result.append({key: obj[key] for key in ("id", "object_id", "library_id", "asset_key", "asset_hash", "title")}
                              | {"compact": compact})
    return result


def features(libraries_root: Path, case):
    path = (libraries_root / case["compact"]).resolve()
    if not path.is_relative_to(libraries_root):
        raise ValueError("Compact case escapes the library directory")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        rows = list(db.execute("""SELECT k.value,t.value,b.value FROM nodes n
          LEFT JOIN symbols k ON n.kind_id=k.id LEFT JOIN symbols t ON n.type_id=t.id
          LEFT JOIN symbols b ON n.branch_id=b.id"""))
    return dict(case,
        physics_types=sorted({type_ for kind, type_, branch in rows if kind == "Physics" and type_}),
        study_types=sorted({type_ for kind, type_, branch in rows if branch == "study" and type_}),
        coupling_types=sorted({type_ for kind, type_, branch in rows if branch == "multiphysics" and type_}),
        physics_features=sorted({type_ for kind, type_, branch in rows if kind == "PhysicsFeature" and type_}))


def analyze(cases):
    tokens = [{("p", value) for value in case["physics_types"]} | {("s", value) for value in case["study_types"]}
              | {("c", value) for value in case["coupling_types"]} | {("f", value) for value in case["physics_features"]}
              for case in cases]
    frequency = Counter(token for case_tokens in tokens for token in case_tokens)
    weights = {token: (1 + math.log((len(cases) + 1) / (count + 1)))
               * {"p": 4, "s": 2, "c": 3, "f": 1}[token[0]] for token, count in frequency.items()}
    norms = [math.sqrt(sum(weights[token] ** 2 for token in case_tokens)) for case_tokens in tokens]
    postings = defaultdict(list)
    for index, case_tokens in enumerate(tokens):
        for token in case_tokens:
            postings[token].append(index)
    for index, case in enumerate(cases):
        scores = defaultdict(float)
        for token in tokens[index]:
            for other in postings[token]:
                if other != index:
                    scores[other] += weights[token] ** 2
        nearest = sorted(((score / (norms[index] * norms[other]), other)
                          for other, score in scores.items()), reverse=True)[:3]
        case["novelty"] = round(1 - nearest[0][0], 4) if nearest else 1.0
        case["nearest"] = [{"similarity": round(score, 4), "library_id": cases[other]["library_id"],
                            "asset_key": cases[other]["asset_key"]} for score, other in nearest]
    return cases


def select(cases, choices):
    choices = validate_choices(choices)
    models = []
    for choice in choices["choices"]:
        found = [case for case in cases if case["library_id"] == choice["library_id"]
                 and case["asset_key"] == choice["asset_key"]]
        if len(found) != 1:
            raise ValueError("Expected exactly one parsed candidate for explicit library_id/asset_key, "
                             f"found {len(found)}: {choice['library_id']}/{choice['asset_key']}")
        item = found[0]
        models.append({key: item[key] for key in ("library_id", "asset_key", "asset_hash", "object_id", "title",
                      "physics_types", "study_types", "coupling_types", "novelty", "nearest")}
                      | {"direction": choice["direction"], "selection_reason": choice["selection_reason"]})
    return {"schema": "evaluation-selection/v1", "id": choices["id"], "name": choices["name"],
            "description": choices["description"],
            "selection_method": {"candidate_count": len(cases), "metric": "1 - maximum weighted feature cosine similarity",
                "features": ["physics interface", "study type", "multiphysics coupling", "physics feature"],
                "weights": {"physics": 4, "study": 2, "coupling": 3, "feature": 1},
                "note": "Explicit user selection by exact library and asset identity; structural similarity is descriptive and does not establish physical correctness or independence."},
            "models": models}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--choices", type=Path, required=True, help="User-owned JSON: id, name, description and explicit choices array")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    choices = load_choices(args.choices)
    root = (args.data_dir / "libraries").resolve()
    output = args.output.resolve()
    if output.is_relative_to(root) or output == args.choices.resolve():
        raise ValueError("Output must be separate from the read-only library and choices input")
    raw = candidates(root)
    with ThreadPoolExecutor(max_workers=4) as pool:
        cases = list(pool.map(lambda case: features(root, case), raw))
    result = select(analyze(cases), choices)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"selected": len(result["models"]), "candidates": len(cases), "output": str(output),
                      "catalog_modified": False, "indexes_rebuilt": False}))


if __name__ == "__main__":
    main()
