"""Bounded acceptance-schema subset. No network/file resolution or regex execution.

Not a general arbitrary JSON Schema execution service. The conservative subset
is deliberate until a separately sandboxed validator is designed and reviewed.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator, validators
from referencing import Registry
from referencing.exceptions import NoSuchResource


class UnsafeSchema(ValueError):
    pass


_ALLOWED = frozenset({
    "$schema", "$defs", "$ref", "$comment", "title", "description",
    "type", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "minProperties", "maxProperties",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "const",
})
_budget: ContextVar[list[int] | None] = ContextVar("result_schema_budget", default=None)


def _deny_retrieve(uri):
    raise NoSuchResource(ref=uri)


def _bounded(keyword):
    def validate(validator, constraint, instance, schema):
        budget = _budget.get()
        if budget is None or budget[0] <= 0:
            raise UnsafeSchema("validation budget exceeded")
        budget[0] -= 1
        yield from keyword(validator, constraint, instance, schema)
    return validate


_BoundedValidator = validators.extend(Draft202012Validator, validators={
    name: _bounded(fn) for name, fn in Draft202012Validator.VALIDATORS.items()
})


def _bounded_json(value: Any, max_bytes: int, max_nodes: int, max_depth: int):
    stack = [(value, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise UnsafeSchema("document complexity exceeded")
        if isinstance(node, dict):
            if len(node) > 128 or any(not isinstance(k, str) or len(k) > 4096 for k in node):
                raise UnsafeSchema("object complexity exceeded")
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            if len(node) > 256:
                raise UnsafeSchema("array complexity exceeded")
            stack.extend((v, depth + 1) for v in node)
        elif isinstance(node, str) and len(node) > max_bytes:
            raise UnsafeSchema("string size exceeded")
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > max_bytes:
            raise UnsafeSchema("document size exceeded")
    except (ValueError, TypeError, RecursionError) as exc:
        raise UnsafeSchema("invalid document") from exc


def check_result_schema(schema: Any) -> None:
    _bounded_json(schema, 16_384, 512, 16)
    if not isinstance(schema, (dict, bool)):
        raise UnsafeSchema("schema must be an object or boolean")
    visits = [0]

    def visit(node, ancestors=(), depth=0):
        visits[0] += 1
        if visits[0] > 512 or depth > 16 or not isinstance(node, (dict, bool)):
            raise UnsafeSchema("unsupported schema complexity")
        if isinstance(node, bool):
            return
        if id(node) in ancestors or not set(node).issubset(_ALLOWED):
            raise UnsafeSchema("unsupported or recursive schema")
        ancestors = (*ancestors, id(node))
        if "$schema" in node:
            # jsonschema.evolve chooses the stock validator when descending into
            # a subschema declaring a dialect, bypassing our keyword wrappers.
            # Permit the declaration only at the root explicitly instantiated
            # as _BoundedValidator. Literal const/enum data is not traversed here.
            if node is not schema or node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
                raise UnsafeSchema("only the root may declare the supported schema dialect")
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise UnsafeSchema("only local JSON Pointer references are supported")
            target = schema
            try:
                for part in unquote(ref[2:]).split("/"):
                    key = part.replace("~1", "/").replace("~0", "~")
                    target = target[key] if isinstance(target, dict) else target[int(key)]
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise UnsafeSchema("invalid local reference") from exc
            visit(target, ancestors, depth + 1)
        for key in ("$defs", "properties"):
            if key in node:
                if not isinstance(node[key], dict):
                    raise UnsafeSchema("invalid schema map")
                for child in node[key].values():
                    visit(child, ancestors, depth + 1)
        for key in ("items", "additionalProperties"):
            if key in node:
                visit(node[key], ancestors, depth + 1)

    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise UnsafeSchema("invalid result schema") from exc


def validate_result_schema(result: dict[str, Any], schema: Any) -> tuple[bool, str]:
    try:
        check_result_schema(schema)
        _bounded_json(result, 262_144, 4096, 32)
        token = _budget.set([4096])
        try:
            validator = _BoundedValidator(schema, registry=Registry(retrieve=_deny_retrieve))
            error = next(validator.iter_errors(result), None)
        finally:
            _budget.reset(token)
        # jsonschema's full error messages may contain private instance data.
        return (True, "") if error is None else (False, "result does not satisfy acceptance schema")
    except Exception:
        return False, "unsupported, invalid or overly complex result schema/data"
