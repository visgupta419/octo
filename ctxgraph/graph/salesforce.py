"""Salesforce metadata: objects, fields and flows from *-meta.xml files."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import FIELD, FILE, FLOW, OBJECT, GraphBuild, ent_id, file_id

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


def extract_metadata(repo_root: Path, b: GraphBuild, all_files: list[str]) -> None:
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
            if root is not None:
                attrs["label"] = _child_text(root, "label")
                attrs["process_type"] = _child_text(root, "processType")
                attrs["status"] = _child_text(root, "status")
                objs = sorted(set(_all_text(root, "object")))
                classes = sorted(set(_all_text(root, "apexClass")))
            flid = b.add_entity(FLOW, name, **attrs)
            for o in objs:
                oid = b.add_entity(OBJECT, o, standard=not o.endswith("__c"))
                b.add_edge(flid, oid, "references")
            for c in classes:
                b.defer(flid, c, "calls")
            if b.has(file_id(rel)):
                b.add_edge(file_id(rel), flid, "defines")
