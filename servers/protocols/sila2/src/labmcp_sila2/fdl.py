"""SiLA 2 Feature Definition Language (FDL) parsing, JSON-schema rendering and value validation.

Implements the data types and constraints of the SiLA 2 standard as defined by the official schemas
``FeatureDefinition.xsd``, ``DataTypes.xsd`` and ``Constraints.xsd`` (gitlab.com/SiLA2/sila_base,
``schema/``; also bundled with the ``sila2`` package) and "SiLA 2 Part A - Overview, Concepts and Core
Specification" (https://sila-standard.com/standards/):

* Basic types: String, Integer (64-bit), Real (double), Boolean, Binary, Date, Time, Timestamp, Any.
* Derived types: List, Structure, Constrained, and DataTypeIdentifier (a DataTypeDefinition).
* Constraints: Length, MinimalLength, MaximalLength, Set, Pattern, MaximalExclusive/Inclusive,
  MinimalExclusive/Inclusive, Unit, ContentType, ElementCount, MinimalElementCount,
  MaximalElementCount, FullyQualifiedIdentifier, Schema, AllowedTypes.

Values arrive as JSON from the MCP client and are converted to the native Python values the
``sila2`` client library expects (``str/int/float/bool/bytes``, ``SilaDateType``, ``datetime.time``,
``datetime.datetime``, ``SilaAnyType``, lists and dicts). Every constraint that can be checked on the
client side is checked *before* anything is sent. No MCP code and no network code in here.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

NS = "{http://www.sila-standard.org}"
INT64 = (-(2**63), 2**63 - 1)
BASIC_TYPES = ("String", "Integer", "Real", "Boolean", "Binary", "Date", "Time", "Timestamp", "Any")


class FDLValidationError(ValueError):
    """A value does not match the FDL type. Nothing was sent."""


# --------------------------------------------------------------------------- IR


@dataclass
class TypeIR:
    """Intermediate representation of a SiLA data type."""

    kind: str  # basic | list | structure | constrained | ref
    basic: str | None = None
    item: TypeIR | None = None
    elements: list[ElementIR] = field(default_factory=list)
    base: TypeIR | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
    ref: str | None = None


@dataclass
class ElementIR:
    identifier: str
    display_name: str
    description: str
    type: TypeIR


@dataclass
class CommandIR:
    identifier: str
    display_name: str
    description: str
    observable: bool
    parameters: list[ElementIR]
    responses: list[ElementIR]
    intermediate_responses: list[ElementIR]
    errors: list[str]


@dataclass
class PropertyIR:
    identifier: str
    display_name: str
    description: str
    observable: bool
    type: TypeIR
    errors: list[str]


@dataclass
class FeatureIR:
    identifier: str
    display_name: str
    description: str
    fully_qualified_identifier: str
    feature_version: str
    maturity_level: str
    sila2_version: str
    originator: str
    category: str
    commands: dict[str, CommandIR]
    properties: dict[str, PropertyIR]
    metadata: dict[str, ElementIR]
    errors: dict[str, tuple[str, str]]  # identifier -> (display name, description)
    data_types: dict[str, ElementIR]
    xml: str

    def data_type(self, identifier: str) -> TypeIR:
        try:
            return self.data_types[identifier].type
        except KeyError as exc:
            raise FDLValidationError(f"Feature {self.identifier} has no DataTypeDefinition {identifier!r}") from exc


# --------------------------------------------------------------------------- parsing


def _t(node: ET.Element | None, tag: str, default: str = "") -> str:
    child = node.find(NS + tag) if node is not None else None
    return " ".join((child.text or "").split()) if child is not None else default


def _parse_type(node: ET.Element) -> TypeIR:
    """Parse a ``<DataType>`` element."""
    child = next(iter(node), None)
    if child is None:
        raise ValueError("Empty <DataType>")
    tag = child.tag.replace(NS, "")
    if tag == "Basic":
        return TypeIR("basic", basic=(child.text or "").strip())
    if tag == "List":
        return TypeIR("list", item=_parse_type(child.find(NS + "DataType")))  # type: ignore[arg-type]
    if tag == "Structure":
        return TypeIR("structure", elements=[_parse_element(e) for e in child.findall(NS + "Element")])
    if tag == "DataTypeIdentifier":
        return TypeIR("ref", ref=(child.text or "").strip())
    if tag == "Constrained":
        base = _parse_type(child.find(NS + "DataType"))  # type: ignore[arg-type]
        return TypeIR("constrained", base=base, constraints=_parse_constraints(child.find(NS + "Constraints")))
    raise ValueError(f"Unknown SiLA data type element <{tag}>")


def _parse_element(node: ET.Element) -> ElementIR:
    return ElementIR(
        identifier=_t(node, "Identifier"),
        display_name=_t(node, "DisplayName"),
        description=_t(node, "Description"),
        type=_parse_type(node.find(NS + "DataType")),  # type: ignore[arg-type]
    )


def _parse_constraints(node: ET.Element | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if node is None:
        return out
    for c in node:
        tag = c.tag.replace(NS, "")
        text = (c.text or "").strip()
        if tag in {"Length", "MinimalLength", "MaximalLength", "ElementCount", "MinimalElementCount",
                   "MaximalElementCount"}:
            out[tag] = int(text)
        elif tag in {"MaximalExclusive", "MaximalInclusive", "MinimalExclusive", "MinimalInclusive", "Pattern",
                     "FullyQualifiedIdentifier"}:
            out[tag] = text
        elif tag == "Set":
            out[tag] = [(v.text or "") for v in c.findall(NS + "Value")]
        elif tag == "Unit":
            out[tag] = {
                "label": _t(c, "Label"),
                "factor": float(_t(c, "Factor", "1")),
                "offset": float(_t(c, "Offset", "0")),
                "si_components": [
                    {"unit": _t(u, "SIUnit"), "exponent": int(_t(u, "Exponent", "1"))}
                    for u in c.findall(NS + "UnitComponent")
                ],
            }
        elif tag == "ContentType":
            params = c.find(NS + "Parameters")
            out[tag] = {
                "type": _t(c, "Type"),
                "subtype": _t(c, "Subtype"),
                "parameters": {
                    _t(p, "Attribute"): _t(p, "Value") for p in (params.findall(NS + "Parameter") if params is not None else [])
                },
            }
        elif tag == "Schema":
            out[tag] = {"type": _t(c, "Type"), "url": _t(c, "Url") or None, "inline": _t(c, "Inline") or None}
        elif tag == "AllowedTypes":
            out[tag] = [_parse_type(d) for d in c.findall(NS + "DataType")]
    return out


def parse_feature(xml: str) -> FeatureIR:
    """Parse a Feature Definition (the XML string returned by SiLAService.GetFeatureDefinition)."""
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    if root.tag != NS + "Feature":
        raise ValueError(f"Not a SiLA Feature Definition (root element {root.tag!r})")
    ident = _t(root, "Identifier")
    originator = root.attrib.get("Originator", "")
    category = root.attrib.get("Category", "none")
    version = root.attrib.get("FeatureVersion", "1.0")
    commands: dict[str, CommandIR] = {}
    for c in root.findall(NS + "Command"):
        errs = c.find(NS + "DefinedExecutionErrors")
        commands[_t(c, "Identifier")] = CommandIR(
            identifier=_t(c, "Identifier"),
            display_name=_t(c, "DisplayName"),
            description=_t(c, "Description"),
            observable=_t(c, "Observable") == "Yes",
            parameters=[_parse_element(p) for p in c.findall(NS + "Parameter")],
            responses=[_parse_element(p) for p in c.findall(NS + "Response")],
            intermediate_responses=[_parse_element(p) for p in c.findall(NS + "IntermediateResponse")],
            errors=[(e.text or "").strip() for e in errs.findall(NS + "Identifier")] if errs is not None else [],
        )
    properties: dict[str, PropertyIR] = {}
    for p in root.findall(NS + "Property"):
        errs = p.find(NS + "DefinedExecutionErrors")
        properties[_t(p, "Identifier")] = PropertyIR(
            identifier=_t(p, "Identifier"),
            display_name=_t(p, "DisplayName"),
            description=_t(p, "Description"),
            observable=_t(p, "Observable") == "Yes",
            type=_parse_type(p.find(NS + "DataType")),  # type: ignore[arg-type]
            errors=[(e.text or "").strip() for e in errs.findall(NS + "Identifier")] if errs is not None else [],
        )
    return FeatureIR(
        identifier=ident,
        display_name=_t(root, "DisplayName"),
        description=_t(root, "Description"),
        fully_qualified_identifier=f"{originator}/{category}/{ident}/v{version.split('.')[0]}",
        feature_version=version,
        maturity_level=root.attrib.get("MaturityLevel", "Draft"),
        sila2_version=root.attrib.get("SiLA2Version", ""),
        originator=originator,
        category=category,
        commands=commands,
        properties=properties,
        metadata={_t(m, "Identifier"): _parse_element(m) for m in root.findall(NS + "Metadata")},
        errors={
            _t(e, "Identifier"): (_t(e, "DisplayName"), _t(e, "Description"))
            for e in root.findall(NS + "DefinedExecutionError")
        },
        data_types={_t(d, "Identifier"): _parse_element(d) for d in root.findall(NS + "DataTypeDefinition")},
        xml=xml,
    )


# --------------------------------------------------------------------------- JSON schema rendering


def _num(text: str, basic: str) -> float | int | str:
    try:
        return int(text) if basic == "Integer" else float(text)
    except ValueError:
        return text


def _root_basic(ir: TypeIR, feature: FeatureIR, depth: int = 0) -> str | None:
    if depth > 32:
        return None
    if ir.kind == "basic":
        return ir.basic
    if ir.kind == "constrained" and ir.base is not None:
        return _root_basic(ir.base, feature, depth + 1)
    if ir.kind == "ref" and ir.ref:
        return _root_basic(feature.data_type(ir.ref), feature, depth + 1)
    return None


def type_label(ir: TypeIR, feature: FeatureIR) -> str:
    """Short human label, e.g. ``Constrained<Real>`` or ``List<ProgramStep>``."""
    if ir.kind == "basic":
        return str(ir.basic)
    if ir.kind == "list":
        return f"List<{type_label(ir.item, feature)}>"  # type: ignore[arg-type]
    if ir.kind == "structure":
        return "Structure"
    if ir.kind == "ref":
        return str(ir.ref)
    return f"Constrained<{type_label(ir.base, feature)}>"  # type: ignore[arg-type]


def to_json_schema(ir: TypeIR, feature: FeatureIR, depth: int = 0) -> dict[str, Any]:
    """Render a SiLA type as a JSON-schema-like dict (with ``x-sila-*`` extensions)."""
    if depth > 32:
        return {"description": "recursive type (truncated)"}
    if ir.kind == "basic":
        return _basic_schema(ir.basic or "")
    if ir.kind == "list":
        return {"type": "array", "items": to_json_schema(ir.item, feature, depth + 1)}  # type: ignore[arg-type]
    if ir.kind == "structure":
        return {
            "type": "object",
            "properties": {
                e.identifier: {**to_json_schema(e.type, feature, depth + 1), "title": e.display_name,
                               "description": e.description}
                for e in ir.elements
            },
            "required": [e.identifier for e in ir.elements],
            "additionalProperties": False,
        }
    if ir.kind == "ref":
        dt = feature.data_types.get(ir.ref or "")
        if dt is None:
            return {"x-sila-type": ir.ref, "description": "unknown data type"}
        schema = to_json_schema(dt.type, feature, depth + 1)
        schema.setdefault("x-sila-type", ir.ref)
        if dt.description and "description" not in schema:
            schema["description"] = dt.description
        return schema
    # constrained
    schema = to_json_schema(ir.base, feature, depth + 1)  # type: ignore[arg-type]
    basic = _root_basic(ir.base, feature) or ""  # type: ignore[arg-type]
    c = ir.constraints
    if "Length" in c:
        key = "minItems" if schema.get("type") == "array" else "minLength"
        schema[key] = schema["max" + key[3:]] = c["Length"]
    for sila, js in (("MinimalLength", "minLength"), ("MaximalLength", "maxLength")):
        if sila in c:
            schema[js] = c[sila]
    for sila, js in (("ElementCount", None), ("MinimalElementCount", "minItems"), ("MaximalElementCount", "maxItems")):
        if sila in c:
            if js is None:
                schema["minItems"] = schema["maxItems"] = c[sila]
            else:
                schema[js] = c[sila]
    if "Set" in c:
        schema["enum"] = [_num(v, basic) if basic in {"Integer", "Real"} else v for v in c["Set"]]
    if "Pattern" in c:
        schema["pattern"] = c["Pattern"]
    for sila, js in (("MinimalInclusive", "minimum"), ("MaximalInclusive", "maximum"),
                     ("MinimalExclusive", "exclusiveMinimum"), ("MaximalExclusive", "exclusiveMaximum")):
        if sila in c:
            schema[js] = _num(c[sila], basic) if basic in {"Integer", "Real"} else c[sila]
    if "Unit" in c:
        u = c["Unit"]
        si = " ".join(f"{p['unit']}^{p['exponent']}" for p in u["si_components"])
        schema["x-unit"] = u["label"]
        schema["x-unit-si"] = f"SI value = value x {u['factor']:g} + {u['offset']:g} [{si}]"
    if "ContentType" in c:
        ct = c["ContentType"]
        schema["contentMediaType"] = f"{ct['type']}/{ct['subtype']}"
    if "FullyQualifiedIdentifier" in c:
        schema["x-sila-fully-qualified-identifier"] = c["FullyQualifiedIdentifier"]
    if "Schema" in c:
        schema["x-sila-schema"] = c["Schema"]
    if "AllowedTypes" in c:
        schema["x-sila-allowed-types"] = [type_label(t, feature) for t in c["AllowedTypes"]]
    return schema


def _basic_schema(basic: str) -> dict[str, Any]:
    return {
        "String": {"type": "string"},
        "Integer": {"type": "integer", "minimum": INT64[0], "maximum": INT64[1]},
        "Real": {"type": "number"},
        "Boolean": {"type": "boolean"},
        "Binary": {"type": "string", "contentEncoding": "base64"},
        "Date": {"type": "string", "format": "date", "description": "YYYY-MM-DD, optional UTC offset (default UTC)"},
        "Time": {"type": "string", "format": "time", "description": "HH:MM:SS[.ffffff], optional UTC offset"},
        "Timestamp": {"type": "string", "format": "date-time", "description": "ISO 8601, default UTC"},
        "Any": {
            "type": "object",
            "properties": {"type": {"enum": list(BASIC_TYPES[:-1])}, "value": {}},
            "required": ["type", "value"],
            "description": "SiLA Any: {'type': <basic type>, 'value': <value>}",
        },
    }.get(basic, {"x-sila-type": basic})


def element_schema(elements: list[ElementIR], feature: FeatureIR) -> dict[str, Any]:
    return {
        e.identifier: {**to_json_schema(e.type, feature), "title": e.display_name, "description": e.description,
                       "x-sila-type-label": type_label(e.type, feature)}
        for e in elements
    }


# --------------------------------------------------------------------------- validation / conversion


_TZ_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")
_FQI_PATTERNS = {
    "FeatureIdentifier": r"[a-z][a-z.]*/[a-z][a-z.]*/[A-Z][a-zA-Z0-9]*/v\d+",
    "CommandIdentifier": r"{f}/Command/[A-Z][a-zA-Z0-9]*",
    "CommandParameterIdentifier": r"{f}/Command/[A-Z][a-zA-Z0-9]*/Parameter/[A-Z][a-zA-Z0-9]*",
    "CommandResponseIdentifier": r"{f}/Command/[A-Z][a-zA-Z0-9]*/Response/[A-Z][a-zA-Z0-9]*",
    "IntermediateCommandResponseIdentifier": r"{f}/Command/[A-Z][a-zA-Z0-9]*/IntermediateResponse/[A-Z][a-zA-Z0-9]*",
    "DefinedExecutionErrorIdentifier": r"{f}/DefinedExecutionError/[A-Z][a-zA-Z0-9]*",
    "PropertyIdentifier": r"{f}/Property/[A-Z][a-zA-Z0-9]*",
    "TypeIdentifier": r"{f}/DataType/[A-Z][a-zA-Z0-9]*",
    "MetadataIdentifier": r"{f}/Metadata/[A-Z][a-zA-Z0-9]*",
}


def _tz(text: str) -> tuple[str, timezone]:
    m = _TZ_RE.search(text)
    if not m:
        return text, timezone.utc
    tz = m.group(1)
    if tz == "Z":
        return text[: m.start()], timezone.utc
    sign = 1 if tz[0] == "+" else -1
    return text[: m.start()], timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6])))


class Converter:
    """Validate JSON values against a feature's types and convert them to sila2 native values."""

    def __init__(self, feature: FeatureIR) -> None:
        self.feature = feature
        self.warnings: list[str] = []

    def convert(self, value: Any, ir: TypeIR, path: str, depth: int = 0) -> Any:
        if depth > 64:
            raise FDLValidationError(f"{path}: value nested too deeply")
        if ir.kind == "basic":
            return self._basic(value, ir.basic or "", path)
        if ir.kind == "ref":
            return self.convert(value, self.feature.data_type(ir.ref or ""), path, depth + 1)
        if ir.kind == "list":
            if not isinstance(value, list):
                raise FDLValidationError(f"{path}: expected a list, got {type(value).__name__}")
            return [self.convert(v, ir.item, f"{path}[{i}]", depth + 1) for i, v in enumerate(value)]  # type: ignore[arg-type]
        if ir.kind == "structure":
            if not isinstance(value, dict):
                raise FDLValidationError(f"{path}: expected an object with fields "
                                         f"{[e.identifier for e in ir.elements]}, got {type(value).__name__}")
            names = [e.identifier for e in ir.elements]
            missing = [n for n in names if n not in value]
            extra = [k for k in value if k not in names]
            if missing or extra:
                raise FDLValidationError(
                    f"{path}: structure fields must be exactly {names}"
                    + (f"; missing {missing}" if missing else "") + (f"; unknown {extra}" if extra else "")
                )
            return {e.identifier: self.convert(value[e.identifier], e.type, f"{path}.{e.identifier}", depth + 1)
                    for e in ir.elements}
        native = self.convert(value, ir.base, path, depth + 1)  # type: ignore[arg-type]
        self._check_constraints(value, native, ir, path)
        return native

    # -- basic types -------------------------------------------------------

    def _basic(self, value: Any, basic: str, path: str) -> Any:
        if basic == "String":
            if not isinstance(value, str):
                raise FDLValidationError(f"{path}: expected a string, got {value!r}")
            if len(value) > 2**21:
                raise FDLValidationError(f"{path}: SiLA strings are limited to 2^21 characters")
            return value
        if basic == "Integer":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FDLValidationError(f"{path}: expected an integer, got {value!r}")
            if isinstance(value, float) and not value.is_integer():
                raise FDLValidationError(f"{path}: expected an integer, got {value!r}")
            iv = int(value)
            if not INT64[0] <= iv <= INT64[1]:
                raise FDLValidationError(f"{path}: {iv} is outside the 64-bit integer range")
            return iv
        if basic == "Real":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FDLValidationError(f"{path}: expected a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise FDLValidationError(f"{path}: {value!r} is not a finite number")
            return float(value)
        if basic == "Boolean":
            if not isinstance(value, bool):
                raise FDLValidationError(f"{path}: expected true or false, got {value!r}")
            return value
        if basic == "Binary":
            if not isinstance(value, str):
                raise FDLValidationError(f"{path}: expected base64-encoded bytes as a string")
            try:
                return base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise FDLValidationError(f"{path}: not valid base64 ({exc})") from exc
        if basic == "Date":
            from sila2.framework import SilaDateType

            if not isinstance(value, str):
                raise FDLValidationError(f"{path}: expected a date string YYYY-MM-DD")
            text, tz = _tz(value.strip())
            try:
                return SilaDateType(date.fromisoformat(text), tz)
            except ValueError as exc:
                raise FDLValidationError(f"{path}: invalid date {value!r} (use YYYY-MM-DD[+HH:MM])") from exc
        if basic == "Time":
            if not isinstance(value, str):
                raise FDLValidationError(f"{path}: expected a time string HH:MM:SS")
            text, tz = _tz(value.strip())
            try:
                return time.fromisoformat(text).replace(tzinfo=tz)
            except ValueError as exc:
                raise FDLValidationError(f"{path}: invalid time {value!r} (use HH:MM:SS[+HH:MM])") from exc
        if basic == "Timestamp":
            if not isinstance(value, str):
                raise FDLValidationError(f"{path}: expected an ISO 8601 timestamp string")
            text, tz = _tz(value.strip())
            try:
                dt = datetime.fromisoformat(text)
            except ValueError as exc:
                raise FDLValidationError(f"{path}: invalid timestamp {value!r}") from exc
            return dt.replace(tzinfo=dt.tzinfo or tz)
        if basic == "Any":
            from sila2.framework import SilaAnyType

            if not isinstance(value, dict) or set(value) != {"type", "value"} or value["type"] not in BASIC_TYPES[:-1]:
                raise FDLValidationError(
                    f"{path}: SiLA Any values must look like {{'type': <one of {list(BASIC_TYPES[:-1])}>, 'value': ...}}"
                )
            native = self._basic(value["value"], value["type"], f"{path}.value")
            return SilaAnyType(f"<DataType><Basic>{value['type']}</Basic></DataType>", native)
        raise FDLValidationError(f"{path}: unsupported SiLA basic type {basic!r}")

    # -- constraints -------------------------------------------------------

    def _check_constraints(self, raw: Any, native: Any, ir: TypeIR, path: str) -> None:
        c = ir.constraints
        basic = _root_basic(ir.base, self.feature)  # type: ignore[arg-type]
        sized = native if isinstance(native, (str, bytes)) else None
        if sized is not None:
            n = len(sized)
            unit = "characters" if isinstance(sized, str) else "bytes"
            if "Length" in c and n != c["Length"]:
                raise FDLValidationError(f"{path}: must be exactly {c['Length']} {unit} long (got {n})")
            if "MinimalLength" in c and n < c["MinimalLength"]:
                raise FDLValidationError(f"{path}: must be at least {c['MinimalLength']} {unit} (got {n})")
            if "MaximalLength" in c and n > c["MaximalLength"]:
                raise FDLValidationError(f"{path}: must be at most {c['MaximalLength']} {unit} (got {n})")
        if isinstance(native, list):
            n = len(native)
            if "ElementCount" in c and n != c["ElementCount"]:
                raise FDLValidationError(f"{path}: needs exactly {c['ElementCount']} elements (got {n})")
            if "MinimalElementCount" in c and n < c["MinimalElementCount"]:
                raise FDLValidationError(f"{path}: needs at least {c['MinimalElementCount']} elements (got {n})")
            if "MaximalElementCount" in c and n > c["MaximalElementCount"]:
                raise FDLValidationError(f"{path}: allows at most {c['MaximalElementCount']} elements (got {n})")
        if "Set" in c:
            allowed = c["Set"]
            if basic in {"Integer", "Real"}:
                ok = any(float(native) == float(v) for v in allowed if _is_number(v))
            else:
                ok = raw in allowed
            if not ok:
                raise FDLValidationError(f"{path}: {raw!r} is not one of the allowed values {allowed}")
        if "Pattern" in c and isinstance(native, str):
            try:
                pattern = re.compile(c["Pattern"])
            except re.error:
                self.warnings.append(f"{path}: pattern {c['Pattern']!r} could not be checked locally (server will).")
            else:
                if not pattern.fullmatch(native):
                    raise FDLValidationError(f"{path}: {native!r} does not match the pattern {c['Pattern']!r}")
        bounds = [(k, c[k]) for k in ("MinimalInclusive", "MinimalExclusive", "MaximalInclusive", "MaximalExclusive") if k in c]
        if bounds:
            value = self._comparable(native, basic)
            for key, text in bounds:
                limit = self._comparable(self._parse_bound(text, basic, path), basic)
                bad = {
                    "MinimalInclusive": value < limit,
                    "MinimalExclusive": value <= limit,
                    "MaximalInclusive": value > limit,
                    "MaximalExclusive": value >= limit,
                }[key]
                if bad:
                    op = {"MinimalInclusive": ">=", "MinimalExclusive": ">", "MaximalInclusive": "<=",
                          "MaximalExclusive": "<"}[key]
                    unit = c.get("Unit", {}).get("label", "")
                    raise FDLValidationError(f"{path}: {raw!r} violates constraint {op} {text}{' ' + unit if unit else ''}")
        if "FullyQualifiedIdentifier" in c and isinstance(native, str):
            kind = c["FullyQualifiedIdentifier"]
            feature_re = _FQI_PATTERNS["FeatureIdentifier"]
            pattern = _FQI_PATTERNS.get(kind, ".*").replace("{f}", feature_re)
            if not re.fullmatch(pattern, native, re.IGNORECASE):
                raise FDLValidationError(f"{path}: {native!r} is not a valid fully qualified {kind}")
        if "AllowedTypes" in c and isinstance(raw, dict) and "type" in raw:
            allowed = [type_label(t, self.feature) for t in c["AllowedTypes"]]
            if raw["type"] not in allowed:
                raise FDLValidationError(f"{path}: Any type {raw['type']!r} is not allowed here (allowed: {allowed})")
        if "Schema" in c or "ContentType" in c:
            self.warnings.append(f"{path}: Schema/ContentType constraints are checked by the server, not locally.")

    def _parse_bound(self, text: str, basic: str | None, path: str) -> Any:
        if basic == "Integer":
            return int(text)
        if basic == "Real":
            return float(text)
        if basic in {"Date", "Time", "Timestamp"}:
            return self._basic(text, basic, path + " (constraint)")
        return text

    @staticmethod
    def _comparable(v: Any, basic: str | None) -> Any:
        if basic == "Date":
            return v.date  # SilaDateType
        if basic == "Time":
            return (v.hour, v.minute, v.second, v.microsecond)
        return v


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def convert_parameters(feature: FeatureIR, command: CommandIR, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate the parameters of ``command`` and convert them to sila2 native values."""
    names = [p.identifier for p in command.parameters]
    unknown = [k for k in params if k not in names]
    missing = [n for n in names if n not in params]
    if unknown or missing:
        raise FDLValidationError(
            f"{feature.identifier}.{command.identifier} takes parameters {names}"
            + (f"; missing {missing}" if missing else "") + (f"; unknown {unknown}" if unknown else "")
        )
    conv = Converter(feature)
    native = {p.identifier: conv.convert(params[p.identifier], p.type, p.identifier) for p in command.parameters}
    return native, conv.warnings


# --------------------------------------------------------------------------- responses -> JSON


def to_jsonable(value: Any) -> Any:
    """Convert sila2 native values (NamedTuples, SilaDateType, datetimes, bytes, SilaAnyType) to JSON."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii"), "length": len(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    cls = type(value).__name__
    if cls == "SilaDateType":
        offset = value.timezone.utcoffset(None)
        return value.date.isoformat() + _fmt_offset(offset)
    if cls == "SilaAnyType":
        return {"type_xml": value.type_xml, "value": to_jsonable(value.value)}
    if isinstance(value, tuple) and hasattr(value, "_fields"):
        return {k: to_jsonable(getattr(value, k)) for k in value._fields}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return str(value)


def _fmt_offset(offset: timedelta | None) -> str:
    if offset is None or offset == timedelta(0):
        return "Z"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"
