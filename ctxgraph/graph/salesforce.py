"""Salesforce metadata: objects, fields and flows from *-meta.xml files."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import FIELD, FILE, FLOW, LAYOUT, OBJECT, PERMISSIONSET, RECORDTYPE, RULE, GraphBuild, ent_id, file_id

# Common standard sObjects, so Apex that uses them links to an object entity
# even when the repo holds no metadata for them.
STANDARD_OBJECTS = {
    "Account", "Contact", "Lead", "Opportunity", "OpportunityLineItem", "Case", "User", "Task",
    "Event", "Campaign", "CampaignMember", "Product2", "Pricebook2", "PricebookEntry", "Order",
    "OrderItem", "Contract", "Asset", "Quote", "QuoteLineItem", "ContentDocument",
    "ContentVersion", "ContentDocumentLink", "Attachment", "EmailMessage", "Group",
    "GroupMember", "Profile", "PermissionSet", "RecordType", "Note", "FeedItem",
    "Individual", "Organization", "AccountContactRelation", "Entitlement", "ServiceContract",
    "WorkOrder", "Knowledge__kav", "Dashboard", "Report", "Folder", "Document", "StaticResource",
}
_CUSTOM_OBJECT_RE = re.compile(r"\b[A-Za-z]\w*__(?:c|mdt|e|b|x)\b")


def is_object_name(name: str) -> bool:
    return name in STANDARD_OBJECTS or bool(_CUSTOM_OBJECT_RE.fullmatch(name))


OBJECT_RE = re.compile(r"(?:^|/)objects/([^/]+)/\1\.object-meta\.xml$")
FIELD_RE = re.compile(r"(?:^|/)objects/([^/]+)/fields/([^/]+)\.field-meta\.xml$")
FLOW_RE = re.compile(r"(?:^|/)flows/([^/]+)\.flow-meta\.xml$")
RULE_RE = re.compile(r"(?:^|/)objects/([^/]+)/validationRules/([^/]+)\.validationRule-meta\.xml$")
RECORDTYPE_RE = re.compile(r"(?:^|/)objects/([^/]+)/recordTypes/([^/]+)\.recordType-meta\.xml$")
PERMSET_RE = re.compile(r"(?:^|/)permissionsets/([^/]+)\.permissionset-meta\.xml$")
LAYOUT_RE = re.compile(r"(?:^|/)layouts/([^/]+)\.layout-meta\.xml$")
_FORMULA_FIELD_RE = re.compile(r"\b([A-Za-z_]\w*__c)\b")
_FLOW_RECORD_ELEMENTS = ("recordLookups", "recordUpdates", "recordCreates", "recordDeletes")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(path: Path) -> ET.Element | None:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def _child_text(root: ET.Element, name: str) -> str | None:
    for el in root:
        if _local(el.tag) == name and el.text:
            return el.text.strip()
    return None


def _all_text(root: ET.Element, name: str) -> list[str]:
    out: list[str] = []
    for el in root.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            out.append(el.text.strip())
    return out


def _children(root: ET.Element, name: str) -> list[ET.Element]:
    return [el for el in root if _local(el.tag) == name]


def _link_field(b: GraphBuild, src: str, obj: str, fld: str, kind: str = "references") -> None:
    fid = ent_id(FIELD, f"{obj}.{fld}")
    if b.has(fid):
        b.add_edge(src, fid, kind)


def extract_metadata(repo_root: Path, b: GraphBuild, all_files: list[str]) -> None:
    # objects and fields first, then flows (which link to fields), then the rest
    ordered = sorted(all_files, key=lambda p: (bool(FLOW_RE.search(p)), p))
    _extract_core(repo_root, b, ordered)
    _extract_secondary(repo_root, b, all_files)  # needs the fields from the first pass


def _extract_secondary(repo_root: Path, b: GraphBuild, all_files: list[str]) -> None:
    for rel in all_files:
        m = RULE_RE.search(rel)
        if m:
            obj, name = m.group(1), m.group(2)
            root = _parse(repo_root / rel)
            oid = b.add_entity(OBJECT, obj, standard=not obj.endswith("__c"))
            attrs = {"object": obj, "path": rel}
            formula = ""
            if root is not None:
                attrs["active"] = (_child_text(root, "active") or "").lower() == "true"
                attrs["error_message"] = _child_text(root, "errorMessage")
                formula = _child_text(root, "errorConditionFormula") or ""
                attrs["formula"] = formula[:300]
            rid = b.add_entity(RULE, f"{obj}.{name}", **attrs)
            b.add_edge(rid, oid, "belongs_to")
            for fld in sorted(set(_FORMULA_FIELD_RE.findall(formula))):
                _link_field(b, rid, obj, fld)
            continue
        m = RECORDTYPE_RE.search(rel)
        if m:
            obj, name = m.group(1), m.group(2)
            root = _parse(repo_root / rel)
            oid = b.add_entity(OBJECT, obj, standard=not obj.endswith("__c"))
            rid = b.add_entity(RECORDTYPE, f"{obj}.{name}", object=obj, path=rel,
                               label=_child_text(root, "label") if root is not None else None)
            b.add_edge(rid, oid, "belongs_to")
            continue
        m = PERMSET_RE.search(rel)
        if m:
            root = _parse(repo_root / rel)
            name = m.group(1)
            pid = b.add_entity(PERMISSIONSET, name, path=rel, label=_child_text(root, "label") if root is not None else None)
            if root is None:
                continue
            for el in _children(root, "objectPermissions"):
                obj = _child_text(el, "object")
                if obj:
                    oid = b.add_entity(OBJECT, obj, standard=not obj.endswith("__c"))
                    perms = [k for k in ("allowRead", "allowCreate", "allowEdit", "allowDelete") if (_child_text(el, k) or "").lower() == "true"]
                    b.add_edge(pid, oid, "grants", permissions=sorted(p[5:].lower() for p in perms))
            for el in _children(root, "fieldPermissions"):
                fld = _child_text(el, "field") or ""
                if "." in fld:
                    obj, f = fld.split(".", 1)
                    fid = b.add_entity(FIELD, fld, object=obj)
                    b.add_edge(fid, ent_id(OBJECT, b.add_entity(OBJECT, obj, standard=not obj.endswith("__c")).split(":", 1)[1]), "belongs_to")
                    b.add_edge(pid, fid, "grants", readable=(_child_text(el, "readable") or "").lower() == "true",
                               editable=(_child_text(el, "editable") or "").lower() == "true")
            for el in _children(root, "classAccesses"):
                cls = _child_text(el, "apexClass")
                if cls and (_child_text(el, "enabled") or "").lower() == "true":
                    b.defer(pid, cls, "grants")
            continue
        m = LAYOUT_RE.search(rel)
        if m:
            name = m.group(1)
            obj = name.split("-", 1)[0]
            root = _parse(repo_root / rel)
            oid = b.add_entity(OBJECT, obj, standard=not obj.endswith("__c"))
            lid = b.add_entity(LAYOUT, name, object=obj, path=rel)
            b.add_edge(lid, oid, "belongs_to")
            if root is not None:
                for fld in sorted(set(_all_text(root, "field"))):
                    _link_field(b, lid, obj, fld, "shows")
            continue


def _extract_core(repo_root: Path, b: GraphBuild, all_files: list[str]) -> None:
    for rel in all_files:
        m = OBJECT_RE.search(rel)
        if m:
            root = _parse(repo_root / rel)
            name = m.group(1)
            b.add_entity(
                OBJECT, name, standard=not name.endswith("__c"), path=rel,
                label=_child_text(root, "label") if root is not None else None,
            )
            if b.has(file_id(rel)):
                b.add_edge(file_id(rel), ent_id(OBJECT, name), "defines")
            continue
        m = FIELD_RE.search(rel)
        if m:
            obj, fld = m.group(1), m.group(2)
            root = _parse(repo_root / rel)
            oid = b.add_entity(OBJECT, obj, standard=not obj.endswith("__c"))
            attrs = {"object": obj, "path": rel}
            refs: list[str] = []
            if root is not None:
                attrs["type"] = _child_text(root, "type")
                attrs["label"] = _child_text(root, "label")
                refs = _all_text(root, "referenceTo")
            fid = b.add_entity(FIELD, f"{obj}.{fld}", **attrs)
            b.add_edge(fid, oid, "belongs_to")
            for r in refs:
                rid = b.add_entity(OBJECT, r, standard=not r.endswith("__c"))
                b.add_edge(fid, rid, "references")
            if b.has(file_id(rel)):
                b.add_edge(file_id(rel), fid, "defines")
            continue
        m = FLOW_RE.search(rel)
        if m:
            root = _parse(repo_root / rel)
            name = m.group(1)
            attrs = {"path": rel}
            objs: list[str] = []
            classes: list[str] = []
            subflows: list[str] = []
            field_refs: list[tuple[str, str]] = []
            trigger_obj: str | None = None
            if root is not None:
                attrs["label"] = _child_text(root, "label")
                attrs["process_type"] = _child_text(root, "processType")
                attrs["status"] = _child_text(root, "status")
                objs = sorted(set(_all_text(root, "object")))
                classes = sorted(set(_all_text(root, "apexClass")))
                subflows = sorted(set(_all_text(root, "flowName")))
                start = _children(root, "start")
                if start:
                    trigger_obj = _child_text(start[0], "object")
                    tt = _child_text(start[0], "triggerType") or _child_text(start[0], "recordTriggerType")
                    if tt:
                        attrs["trigger_type"] = tt
                for tag in _FLOW_RECORD_ELEMENTS:
                    for el in _children(root, tag):
                        o = _child_text(el, "object")
                        if not o:
                            continue
                        for f in _all_text(el, "field"):
                            field_refs.append((o, f))
            flid = b.add_entity(FLOW, name, **attrs)
            for o in objs:
                oid = b.add_entity(OBJECT, o, standard=not o.endswith("__c"))
                b.add_edge(flid, oid, "references")
            if trigger_obj:
                b.add_edge(flid, ent_id(OBJECT, trigger_obj), "triggers_on")
            for c in classes:
                b.defer(flid, c, "calls")
            for sf in subflows:
                b.defer(flid, sf, "calls")
            for o, f in sorted(set(field_refs)):
                _link_field(b, flid, o, f)
            if b.has(file_id(rel)):
                b.add_edge(file_id(rel), flid, "defines")
