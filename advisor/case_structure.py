"""Conservative, source-located structure of COMSOL Java reconstruction code.

This is a lexical source reader, not a Java interpreter or COMSOL API resolver.
It preserves every source line. Dependencies describe explicit receiver syntax
and unambiguous same-file function calls, never effective runtime state.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .build_control import check_cancelled
from .case_metadata import normalize_feature_branch

MAX_JAVA_BYTES = 8 * 1024 * 1024
STRUCTURE_VERSION = "java-source-structure/v1"
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_ACCESSORS = {"component", "geom", "mesh", "physics", "material", "multiphysics", "selection", "study", "sol",
              "result", "param", "func", "variable", "view", "probe", "feature", "propertyGroup", "pair", "cpl",
              "coordinateSystem", "coordSystem", "common", "dataset", "numerical", "table", "export", "prop", "evaluationGroup"}
_BRANCHES = {"geom", "mesh", "physics", "material", "multiphysics", "selection", "study", "sol", "result", "param", "func", "variable"}
_FEATURE_OWNERS = {"geom", "mesh", "physics", "study", "sol", "result", "func", "feature"}
_CONTROL = {"if", "else", "for", "while", "switch", "catch", "try", "synchronized", "do"}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int


def _tokens(source):
    tokens, warnings, cursor = [], [], 0
    while cursor < len(source):
        if cursor % 65536 == 0:
            check_cancelled()
        start = cursor
        ch = source[cursor]
        if ch.isspace() or ch == "\ufeff":
            cursor += 1
            continue
        if source.startswith("//", cursor):
            cursor = source.find("\n", cursor)
            if cursor < 0:
                break
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            if end < 0:
                warnings.append("Unterminated block comment; remaining source retained without interpreting it")
                break
            cursor = end + 2
            continue
        if source.startswith('"""', cursor):
            end = source.find('"""', cursor + 3)
            cursor = len(source) if end < 0 else end + 3
            if end < 0:
                warnings.append("Unterminated Java text block")
            tokens.append(Token("text_block", source[start:cursor], start, cursor))
            continue
        if ch in {'"', "'"}:
            cursor += 1
            closed = False
            while cursor < len(source):
                if source[cursor] == "\\":
                    cursor += 2
                elif source[cursor] == ch:
                    cursor += 1
                    closed = True
                    break
                else:
                    cursor += 1
            cursor = min(cursor, len(source))
            if not closed:
                warnings.append("Unterminated quoted literal; remaining source was not interpreted as API calls")
            tokens.append(Token("string" if ch == '"' else "char", source[start:cursor], start, cursor))
            continue
        match = _IDENT.match(source, cursor)
        if match:
            cursor = match.end()
            tokens.append(Token("identifier", match.group(), start, cursor))
        else:
            cursor += 1
            tokens.append(Token("punctuation", ch, start, cursor))
    return tokens, warnings


def _pairs(tokens):
    stack, pairs, depths = [], {}, []
    for i, token in enumerate(tokens):
        depths.append(sum(tokens[j].value == "{" for j in stack))
        if token.kind != "punctuation":
            continue
        if token.value in {"(", "[", "{"}:
            stack.append(i)
        elif token.value in {")", "]", "}"} and stack and tokens[stack[-1]].value == {")": "(", "]": "[", "}": "{"}[token.value]:
            start = stack.pop()
            pairs[start] = i
            pairs[i] = start
    return pairs, depths


def _literal(tokens):
    if len(tokens) != 1 or tokens[0].kind != "string":
        return None
    raw = tokens[0].value
    try:
        return json.loads(raw.replace("\\'", "'"))
    except (ValueError, TypeError):
        return None


def _arguments(tokens, start, end, pairs):
    result, cursor, beginning = [], start + 1, start + 1
    while cursor < end:
        if tokens[cursor].value in {"(", "[", "{"} and cursor in pairs:
            cursor = pairs[cursor] + 1
        elif tokens[cursor].value == ",":
            result.append(tokens[beginning:cursor])
            cursor += 1
            beginning = cursor
        else:
            cursor += 1
    if beginning < end:
        result.append(tokens[beginning:end])
    return result


def _functions(tokens, pairs, depths, source):
    result = []
    for i, token in enumerate(tokens):
        if token.value != "{" or i not in pairs or i < 2 or tokens[i-1].value != ")" or depths[i] > 1:
            continue
        opening = pairs.get(i - 1)
        if opening is None or opening < 1:
            continue
        name = tokens[opening-1]
        if name.kind != "identifier" or name.value in _CONTROL or (opening > 1 and tokens[opening-2].value == "."):
            continue
        begin = opening - 2
        while begin >= 0 and tokens[begin].value not in {";", "{", "}"}:
            begin -= 1
        begin += 1
        if begin == opening - 1:  # A bare call followed by a block is not a declaration.
            continue
        signature = source[tokens[begin].start:token.start].strip()
        roots = {tokens[j+1].value for j in range(opening+1, i-1) if tokens[j].value == "Model" and tokens[j+1].kind == "identifier"}
        result.append({"name": name.value, "ordinal": len(result) + 1, "signature": signature,
                       "start": tokens[begin].start, "body_start": i+1, "body_end": pairs[i],
                       "end": tokens[pairs[i]].end, "brace_depth": depths[i]+1, "roots": roots})
    return result


def _path(root, segments):
    return root + "".join("/" + name + ("(" + tag + ")" if tag is not None else "") for name, tag in segments)


def _chain(tokens, start, pairs):
    calls, cursor = [], start + 1
    while (cursor + 2 < len(tokens) and tokens[cursor].value == "."
           and tokens[cursor+1].kind == "identifier" and tokens[cursor+2].value == "(" and cursor+2 in pairs):
        end = pairs[cursor+2]
        calls.append((tokens[cursor+1].value, _arguments(tokens, cursor+2, end, pairs)))
        cursor = end + 1
    return calls, cursor


def _statement(tokens, start, end, pairs, depths, function, source):
    selected = tokens[start:end+1]
    result = {"start": selected[0].start, "end": selected[-1].end, "created": [], "references": [], "removed": [],
              "properties": [], "operations": [], "paths": [], "branches": [], "unresolved": [], "calls": [], "function": function,
              "token_start": start, "token_end": end, "invalidated": []}
    roots = function["roots"] if function else set()
    if function:
        for i in range(start, end):
            if tokens[i].value == "Model" and tokens[i+1].kind == "identifier":
                roots.add(tokens[i+1].value)
        # Only the explicit ModelUtil factory proves a source initialization.
        for i in range(start, end):
            if tokens[i].value in roots and tokens[i+1].value == "=":
                result["invalidated"].append(tokens[i].value)
                if [t.value for t in tokens[i+2:i+6]] == ["ModelUtil", ".", "create", "("]:
                    result["created"].append({"path": tokens[i].value, "type": "Model", "status": "observed_source",
                                              "controlled": depths[i] > function["brace_depth"]})
                else:
                    result["unresolved"].append({"kind": "model_assignment", "variable": tokens[i].value, "reason": "Assigned model origin and alias identity are not resolved"})
    controlled = (any(t.value in _CONTROL for t in selected if t.kind == "identifier")
                  or any(t.value == "?" for t in selected)
                  or any(selected[i].value == selected[i+1].value and selected[i].value in {"&", "|"} for i in range(len(selected)-1)))
    result["controlled"] = controlled
    for created in result["created"]:
        created["controlled"] = created.get("controlled", False) or controlled
    cursor = start
    while cursor <= end:
        token = tokens[cursor]
        if token.kind != "identifier" or cursor > start and tokens[cursor-1].value == ".":
            cursor += 1
            continue
        calls, _ = _chain(tokens, cursor, pairs)
        if not calls or token.value not in roots:
            cursor += 1
            continue
        chain_controlled = controlled or function is not None and depths[cursor] > function["brace_depth"]
        segments, root, chain_branch, opaque_receiver = [], token.value, None, False
        result["references"].append({"path": root, "kind": "api_receiver", "controlled": chain_controlled})
        for call_index, (method, args) in enumerate(calls):
            result["operations"].append(method)
            values = [_literal(arg) for arg in args]
            if method == "study" and any(name == "sol" and tag is not None for name, tag in segments):
                if values and values[0] is not None:
                    result["references"].append({"path": root + "/study(" + values[0] + ")", "kind": "solver_study_attachment", "controlled": chain_controlled})
                continue
            if method in _ACCESSORS:
                tag = values[0] if values else None
                if args and tag is None:
                    result["unresolved"].append({"kind": "dynamic_accessor", "method": method, "reason": "Object tag is not a single literal string"})
                    tag = "?"
                segments.append((method, tag))
                if method in _BRANCHES and chain_branch is None:
                    chain_branch = normalize_feature_branch(method)
                if tag is not None and not opaque_receiver:
                    result["references"].append({"path": _path(root, segments), "kind": "api_receiver", "controlled": chain_controlled})
            elif method == "create" and values and values[0] is not None and segments and not opaque_receiver:
                name, tag = segments[-1]
                if tag is None:
                    segments[-1] = name, values[0]
                elif name in _FEATURE_OWNERS:
                    segments.append(("feature", values[0]))
                else:
                    result["unresolved"].append({"kind": "create_target", "reason": "Creation on this receiver is not resolved", "receiver": _path(root, segments)})
                    continue
                target = _path(root, segments)
                result["created"].append({"path": target, "type": values[1] if len(values) > 1 else None,
                                          "status": "observed_source", "controlled": chain_controlled})
                if name == "physics" and len(values) >= 3 and values[2] is not None:
                    parent = segments[:-1]
                    result["references"].append({"path": _path(root, parent + [("geom", values[2])]), "kind": "physics_geometry_argument", "controlled": chain_controlled})
            elif method == "create":
                result["unresolved"].append({"kind": "create_target", "reason": "Creation tag or receiver is not statically resolved"})
            elif method in {"set", "setIndex"} and values and values[0] is not None:
                result["properties"].append({"path": None if opaque_receiver else _path(root, segments), "name": values[0], "operation": method})
            elif method in {"set", "setIndex"} and segments and segments[-1][0] == "selection":
                result["unresolved"].append({"kind": "selection_binding", "reason": "Selection entity/input expressions are retained in code but not resolved into topology dependencies"})
            elif method == "named" and values and values[0] is not None and segments and segments[-1][0] == "selection":
                component = [segment for segment in segments if segment[0] == "component"]
                result["references"].append({"path": _path(root, component + [("selection", values[0])]), "kind": "named_selection", "controlled": chain_controlled})
            elif method == "attach" and values and values[0] is not None and any(name == "sol" for name, _ in segments):
                result["references"].append({"path": root + "/study(" + values[0] + ")", "kind": "solver_study_attachment", "controlled": chain_controlled})
            elif method == "remove" and values and values[0] is not None and segments:
                name, tag = segments[-1]
                target = segments[:-1] + [(name, values[0])] if tag is None else segments + [("feature", values[0])]
                result["removed"].append(_path(root, target))
            if method in {"importData", "load", "read"}:
                result["unresolved"].append({"kind": "external_resource", "operation": method, "reason": "External resource closure is not resolved by this lexical reader"})
            if method not in _ACCESSORS and method != "create" and call_index < len(calls)-1:
                opaque_receiver = True
                result["unresolved"].append({"kind": "unknown_intermediate_receiver", "method": method,
                                             "reason": "A return-object identity cannot be proved for this method; subsequent creations/properties are not assigned to its parent"})
        result["paths"].append(_path(root, segments))
        if chain_branch and chain_branch not in result["branches"]:
            result["branches"].append(chain_branch)
        cursor += 1  # Also inspect explicit receivers nested in call arguments.
    # Receiver traversal for the newly created object is not a prerequisite to
    # its own creation. Parent receiver requirements remain explicit.
    created = {item["path"] for item in result["created"]}
    result["references"] = [item for item in result["references"] if item["path"] not in created]
    return result


def _records(tokens, pairs, depths, functions, source):
    result = []
    for function in functions:
        start = function["body_start"]
        parentheses = 0
        for i in range(start, function["body_end"]):
            token = tokens[i]
            if token.kind == "punctuation":
                if token.value == "(":
                    parentheses += 1
                elif token.value == ")":
                    parentheses -= 1
                elif token.value == ";" and parentheses == 0:
                    result.append(_statement(tokens, start, i, pairs, depths, function, source))
                    start = i + 1
    return result


def _unique(values):
    result, seen = [], set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def java_objects(path: Path, *, exported: bool):
    path = Path(path)
    if path.stat().st_size > MAX_JAVA_BYTES:
        return []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        source = stream.read()
    if not source:
        return []
    tokens, warnings = _tokens(source)
    token_starts = [token.start for token in tokens]
    pairs, depths = _pairs(tokens)
    if any(token.kind == "punctuation" and token.value in {"(", ")", "[", "]", "{", "}"} and i not in pairs for i, token in enumerate(tokens)):
        warnings.append("Unbalanced source delimiters; supported structures are partial and Java validity was not checked")
    functions = _functions(tokens, pairs, depths, source)
    if not functions:
        warnings.append("No supported method bodies identified; complete source retained without object/dependency inference")
    records = _records(tokens, pairs, depths, functions, source)
    starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", source)]
    lines = source.splitlines(keepends=True)
    line = lambda offset: bisect_right(starts, offset)
    # Methods are kept in declaration order. Calls are recorded in source order;
    # that is not a claim that declaration order is runtime execution order.
    by_name = {}
    for function in functions:
        by_name.setdefault(function["name"], []).append(function)
    for record in records:
        record["line_start"], record["line_end"] = line(record["start"]), line(record["end"]-1)
        for i in range(record["token_start"], min(record["token_end"]+1, len(tokens)-1)):
            token = tokens[i]
            if (token.kind == "identifier" and token.value in by_name and tokens[i+1].value == "("
                    and (i == 0 or tokens[i-1].value != ".")):
                record["calls"].append({"name": token.value, "offset": token.start, "line": line(token.start)})
    # Non-overlapping line units prevent splitting one line into misleading
    # half-statements, including when several API calls share a source line.
    units = []
    for record in records:
        if units and record["line_start"] <= units[-1]["end"]:
            units[-1]["records"].append(record)
            units[-1]["end"] = max(units[-1]["end"], record["line_end"])
        else:
            units.append({"start": record["line_start"], "end": record["line_end"], "records": [record]})
    groups, next_line = [], 1

    def add(first, last, members, function=None):
        if first > last:
            return
        branches = list(dict.fromkeys(branch for record in members for branch in record["branches"]))
        branch = branches[0] if len(branches) == 1 else "misc"
        paths = list(dict.fromkeys(p for record in members for p in record["paths"]))
        focus = paths[0] if len(paths) == 1 else "mixed" if paths else "context"
        member_functions = list(dict.fromkeys(record["function"]["ordinal"] for record in members))
        if len(member_functions) > 1:
            function = None
        key = (function["ordinal"] if function else None, branch, focus)
        if groups and groups[-1]["key"] == key and groups[-1]["end"] + 1 == first:
            groups[-1]["end"] = last
            groups[-1]["records"].extend(members)
        else:
            groups.append({"start": first, "end": last, "records": members, "key": key, "function": function, "branch": branch})

    for unit in units:
        first = unit["start"]
        function = unit["records"][0]["function"]
        if next_line < first:
            # Include only whitespace/comments between adjacent statements in
            # the following fragment. Class and function scaffolding stays misc.
            gap_start, gap_end = starts[next_line-1], starts[first-1]
            gap_tokens = tokens[bisect_left(token_starts, gap_start):bisect_left(token_starts, gap_end)]
            if not gap_tokens:
                first = next_line
            else:
                add(next_line, first-1, [])
        add(first, unit["end"], unit["records"], function)
        next_line = unit["end"] + 1
    add(next_line, len(lines), [])
    objects = []
    for ordinal, group in enumerate(groups, 1):
        members, function = group["records"], group["function"]
        code = "".join(lines[group["start"]-1:group["end"]])
        signature = function["signature"] if function else "file scaffolding"
        local_id = "code-" + hashlib.sha256(f"{path.name}:{signature}:{group['start']}:{group['end']}".encode()).hexdigest()[:20]
        created = _unique([item for record in members for item in record["created"]])
        properties = _unique([item for record in members for item in record["properties"]])
        operations = list(dict.fromkeys(item for record in members for item in record["operations"]))
        api_paths = list(dict.fromkeys(item for record in members for item in record["paths"]))
        kinds = list(dict.fromkeys(item["type"] for item in created if item["type"]))
        summary = f"Java source fragment {ordinal}; function {function['name'] if function else 'file scaffolding'}; branch {group['branch']}."
        if created:
            summary += " Creates: " + "; ".join(item["path"] + (" type=" + item["type"] if item["type"] else " type not stated as a literal") for item in created) + "."
        if properties:
            summary += " Property names: " + ", ".join(dict.fromkeys(item["name"] for item in properties)) + "."
        if operations:
            summary += " API operations: " + ", ".join(operations) + "."
        span = {"line_start": group["start"], "line_end": group["end"]}
        data = {"language": "java", "feature_branch": group["branch"], "feature_branch_origin": "explicit_java_receiver_syntax",
                "feature_type": kinds[0] if len(kinds) == 1 else None, "api_object_paths": api_paths,
                "summary": summary, "summary_origin": "deterministic_source_syntax_not_semantic_annotation",
                "code": code, "scope_hint": group["branch"], "source_spans": [span], "source_order": ordinal,
                "function": {k: function[k] for k in ("name", "ordinal", "signature")} if function else None,
                "functions": _unique([{k: record["function"][k] for k in ("name", "ordinal", "signature")} for record in members]),
                "function_calls": [call for record in members for call in record["calls"]],
                "statement_spans": [{"line_start": record["line_start"], "line_end": record["line_end"],
                                     "char_start": record["start"], "char_end": record["end"]} for record in members],
                "objects_created": created, "properties_set": properties, "operations": operations,
                "references": [], "prerequisites": [], "unresolved": [item for record in members for item in record["unresolved"]],
                "code_origin": "exported_reconstruction" if exported else "provided_source", "execution_status": "not_executed",
                "execution_order": "not_proven", "standalone": False, "index_text_truncated": False,
                "text_line_endings": "normalized_lf_code_body_preserves_source_line_endings",
                "workflow_history": "not_reconstructed", "structure_version": STRUCTURE_VERSION,
                "dependency_coverage": {"status": "explicit_source_syntax_only", "execution_validated": False,
                                        "not_resolved": ["Expression-string references", "Selection/topology input arrays", "Implicit/default-created objects",
                                                         "Model aliases and cross-function execution state", "External resource closure"]},
                "creation_semantics": "Create calls are present in source; successful execution and current model agreement are not established",
                "analysis_scope": "lexical_receivers_and_literal_create_arguments_only", "parse_warnings": warnings}
        objects.append({"local_id": local_id, "type": "implementation_snippet", "title": f"Java · {function['name'] if function else 'context'} · {group['branch']} · {ordinal}",
                        "text": summary + "\n\n" + code.replace("\r\n", "\n").replace("\r", "\n"), "data": data,
                        "evidence": [{"asset_path": str(path), "locator": f"lines:{group['start']}-{group['end']}",
                                      "method": "comsol_java_export" if exported else "provided_java_source", "status": "observed"}],
                        "relations": [], "kind": "code"})
        group["object"] = objects[-1]
        for record in members:
            record["object"] = objects[-1]
    _dependencies(records, groups, functions, by_name)
    return objects


def _dependencies(records, groups, functions, by_name):
    definitions = {}
    function_fragments = {function["ordinal"]: [group["object"]["local_id"] for group in groups if any(record["function"] is function for record in group["records"])] for function in functions}
    for record in records:
        obj, function = record["object"], record["function"]
        key = function["ordinal"]
        for reference in record["references"]:
            item = {k: v for k, v in reference.items() if k != "controlled"}
            target = definitions.get((key, reference["path"]))
            if target and not reference["controlled"] and not target["controlled"]:
                item.update(status="same_function_preceding_source_definition", target_local_id=target["local_id"])
                if target["local_id"] != obj["local_id"]:
                    obj["relations"].append({"type": "requires", "target_local_id": target["local_id"], "basis": "explicit_api_receiver_in_same_function", "execution_order": "not_proven"})
            else:
                item.update(status="unresolved", reason="controlled_execution_not_proven" if reference["controlled"] or target and target["controlled"] else "no_preceding_definition_in_same_function")
            obj["data"]["references"].append(item)
            obj["data"]["prerequisites"].append(dict(item))
        for created in record["created"]:
            if created["path"] in record["invalidated"]:
                for identity in list(definitions):
                    if identity[0] == key and (identity[1] == created["path"] or identity[1].startswith(created["path"] + "/")):
                        definitions.pop(identity)
            definitions[key, created["path"]] = {"local_id": obj["local_id"], "controlled": created.get("controlled", False)}
        for root in record["invalidated"]:
            if any(created["path"] == root for created in record["created"]):
                continue
            for identity in list(definitions):
                if identity[0] == key and (identity[1] == root or identity[1].startswith(root + "/")):
                    definitions.pop(identity)
        for path in record["removed"]:
            for identity in list(definitions):
                if identity[0] == key and (identity[1] == path or identity[1].startswith(path + "/")):
                    definitions.pop(identity)
        for call in record["calls"]:
            candidates = by_name[call["name"]]
            item = {"kind": "function_call", "function": call["name"], "line": call["line"]}
            if len(candidates) == 1 and candidates[0] is not function:
                targets = function_fragments[candidates[0]["ordinal"]]
                item.update(status="same_file_function_definition", target_local_ids=targets, execution_order="not_proven")
                obj["relations"].extend({"type": "requires", "target_local_id": target, "basis": "explicit_same_file_function_call", "execution_order": "not_proven"} for target in targets if target != obj["local_id"])
            else:
                item.update(status="unresolved", reason="recursive_or_overloaded_function")
            obj["data"]["references"].append(item)
            obj["data"]["prerequisites"].append(dict(item))
    for group in groups:
        obj = group["object"]
        obj["relations"] = _unique(obj["relations"])
        obj["data"]["references"] = _unique(obj["data"]["references"])
        obj["data"]["prerequisites"] = _unique(obj["data"]["prerequisites"])
