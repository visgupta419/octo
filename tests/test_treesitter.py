from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, write

from ctxgraph.config import ChunkingConfig, parse_config
from ctxgraph.graph import build_graph
from ctxgraph.graph.facts import describe
from ctxgraph.ingest import run_ingest
from ctxgraph.ingest.chunk import chunk_code
from ctxgraph.ingest.code import list_declarations
from ctxgraph.ingest.treesitter import available, parse_file
from ctxgraph.store import Database
from test_code_chunk import APEX, APEX_CFG
from test_graph import APEX_BILLING, APEX_SERVICE, APEX_TRIGGER, CONFIG, LWC_JS, _sf_repo

pytestmark = pytest.mark.skipif(not available(), reason="tree-sitter not installed")


def test_apex_parse_declarations_annotations_soql_dml():
    p = parse_file(APEX_SERVICE, "apex")
    decls = [(d.kind, d.qualified) for d in p.declarations]
    assert decls == [("class", "AccountService"), ("function", "AccountService.getAccounts"), ("function", "AccountService.sync")]
    assert p.attrs["AccountService"]["modifiers"] == ["public", "with sharing"]
    assert p.attrs["AccountService.getAccounts"]["annotations"] == ["AuraEnabled"]
    assert [(s.caller, s.object, s.fields) for s in p.soql] == [("AccountService.getAccounts", "Account", ["Id", "Name", "Industry_Segment__c"])]
    assert ("AccountService.sync", "push", "BillingClient") in [(c.caller, c.callee, c.receiver) for c in p.calls]
    t = parse_file(APEX_TRIGGER, "apex")
    assert t.declarations[0].kind == "trigger" and t.attrs["AccountTrigger"]["object"] == "Account"
    assert t.attrs["AccountTrigger"]["events"] == ["before insert"]
    assert [(c.callee, c.receiver) for c in t.calls] == [("sync", "AccountService")]
    d = parse_file("public class X { void m() { insert accs; Database.update(a); delete b; } }", "apex")
    assert sorted(op for _, op in d.dml) == ["delete", "insert"]


def test_java_kotlin_python_js_declarations_and_calls():
    java = """\
/** doc */
@Service public class Foo extends Base implements Runnable, Closeable {
  Foo(int x) { this.x = x; }
  @Override public void run() { bar.go(1); helper(); Util.stat(); executor.submit(new Runnable() { public void run() {} }); }
  static <T> List<T> helper() { return null; }
  record R(int a) {}
  @interface Marker {}
}
"""
    p = parse_file(java, "java")
    assert [(d.kind, d.qualified) for d in p.declarations] == [
        ("class", "Foo"), ("function", "Foo.Foo"), ("function", "Foo.run"), ("function", "Foo.helper"),
        ("record", "Foo.R"), ("annotation", "Foo.Marker"),
    ]
    assert p.attrs["Foo"] == {"annotations": ["Service"], "modifiers": ["public"], "implements": ["Runnable", "Closeable"], "extends": "Base"}
    assert {(c.callee, c.receiver) for c in p.calls if c.caller == "Foo.run"} >= {("go", "bar"), ("helper", None), ("stat", "Util")}

    kotlin = """\
class Handler(private val repo: Repo) : Base(), Iface {
  companion object { const val X = 1 }
  override fun handle(m: Msg) { m.withLocking { repo.store(it) }; helper(); a.b.c(1) }
  private fun helper() = 1
  object Nested { fun f() {} }
}
fun topLevel() {}
"""
    p = parse_file(kotlin, "kotlin")
    assert [(d.kind, d.qualified) for d in p.declarations] == [
        ("class", "Handler"), ("object", "Handler.companion"), ("function", "Handler.handle"),
        ("function", "Handler.helper"), ("object", "Handler.Nested"), ("function", "Handler.Nested.f"), ("function", "topLevel"),
    ]
    assert p.attrs["Handler"]["extends"] == "Base" and p.attrs["Handler"]["implements"] == ["Iface"]
    assert {(c.callee, c.receiver) for c in p.calls if c.caller == "Handler.handle"} >= {("withLocking", "m"), ("store", "repo"), ("helper", None), ("c", "a")}

    py = "import os\n\n@dec\nclass S:\n    @staticmethod\n    def helper(x):\n        return go(x) + obj.m(1)\n\ndef top():\n    S.helper(2)\n"
    p = parse_file(py, "python")
    assert [(d.kind, d.qualified, d.start_line) for d in p.declarations] == [("class", "S", 3), ("function", "S.helper", 5), ("function", "top", 9)]
    assert {(c.caller, c.callee, c.receiver) for c in p.calls} == {("S.helper", "go", None), ("S.helper", "m", "obj"), ("top", "helper", "S")}

    p = parse_file(LWC_JS, "javascript")
    assert [(d.kind, d.qualified) for d in p.declarations] == [("class", "AccountList")]
    js = "class A { handleClick() { items.forEach((i) => { use(i); }); } f = (e) => { go(e) } }\nconst arrow = async (a) => { helper(a) }\n"
    p = parse_file(js, "javascript")
    assert [(d.kind, d.qualified) for d in p.declarations] == [("class", "A"), ("function", "A.handleClick"), ("function", "A.f"), ("function", "arrow")]


def test_parser_and_heuristic_chunk_the_same_apex_class():
    ts = chunk_code("classes/AccountService.cls", APEX, ChunkingConfig(**{**APEX_CFG.__dict__, "parser": "treesitter"}))
    heur = chunk_code("classes/AccountService.cls", APEX, ChunkingConfig(**{**APEX_CFG.__dict__, "parser": "heuristic"}))
    assert [(c.heading, c.start_line, c.end_line) for c in ts] == [(c.heading, c.start_line, c.end_line) for c in heur]
    assert ts[0].start_line == 1 and "Service layer" in ts[0].text  # javadoc travels with the class
    assert list_declarations(APEX.splitlines(), "apex", "treesitter")[0].kind == "class"


def test_graph_call_edges_soql_and_attrs(tmp_path: Path):
    root = _sf_repo(tmp_path)
    cfg = parse_config(dict(CONFIG), root / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    build_graph(cfg, db)
    trig = db.find_entities("AccountTrigger", "symbol")[0]
    out = {(e.kind, n.name) for e, n in db.edges_from(trig.id)}
    assert ("calls", "AccountService.sync") in out and ("triggers_on", "Account") in out
    assert trig.attrs["events"] == ["before insert"]
    sync = db.find_entities("AccountService.sync", "symbol")[0]
    assert ("calls", "BillingClient.push") in {(e.kind, n.name) for e, n in db.edges_from(sync.id)}
    get = db.find_entities("AccountService.getAccounts", "symbol")[0]
    gout = {(e.kind, n.name, e.attrs.get("via")) for e, n in db.edges_from(get.id)}
    assert ("references", "Account", "soql") in gout and ("references", "Account.Industry_Segment__c", "soql") in gout
    assert get.attrs["annotations"] == ["AuraEnabled"]
    svc = db.find_entities("AccountService", "symbol")[0]
    line = describe(db, svc)
    assert "(with sharing)" in line and "members: getAccounts, sync" in line
    gline = describe(db, get)
    assert "annotations: @AuraEnabled" in gline and "references: Account" in gline
    tline = describe(db, trig)
    assert "events: before insert" in tline and "calls: AccountService.sync" in tline
    assert "called by: AccountTrigger" in describe(db, sync)


def test_salesforce_metadata_rules_permsets_layouts_flows(tmp_path: Path):
    root = _sf_repo(tmp_path)
    base = "force-app/main/default"
    write(root, f"{base}/objects/Account/validationRules/Segment_Required.validationRule-meta.xml",
          '<?xml version="1.0"?><ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata"><fullName>Segment_Required</fullName>'
          '<active>true</active><errorConditionFormula>ISBLANK(TEXT(Industry_Segment__c)) &amp;&amp; NOT(ISBLANK(Other__c))</errorConditionFormula>'
          '<errorMessage>Segment is required</errorMessage></ValidationRule>')
    write(root, f"{base}/objects/Account/recordTypes/Partner.recordType-meta.xml",
          '<?xml version="1.0"?><RecordType xmlns="http://soap.sforce.com/2006/04/metadata"><fullName>Partner</fullName><label>Partner Account</label></RecordType>')
    write(root, f"{base}/permissionsets/Sales_User.permissionset-meta.xml",
          '<?xml version="1.0"?><PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata"><label>Sales User</label>'
          '<classAccesses><apexClass>AccountService</apexClass><enabled>true</enabled></classAccesses>'
          '<fieldPermissions><editable>true</editable><field>Account.Industry_Segment__c</field><readable>true</readable></fieldPermissions>'
          '<objectPermissions><allowCreate>true</allowCreate><allowRead>true</allowRead><allowEdit>false</allowEdit><object>Invoice__c</object></objectPermissions></PermissionSet>')
    write(root, f"{base}/layouts/Account-Account Layout.layout-meta.xml",
          '<?xml version="1.0"?><Layout xmlns="http://soap.sforce.com/2006/04/metadata"><layoutSections><layoutColumns>'
          '<layoutItems><field>Industry_Segment__c</field></layoutItems><layoutItems><field>Name</field></layoutItems></layoutColumns></layoutSections></Layout>')
    write(root, f"{base}/flows/Account_After_Save.flow-meta.xml",
          '<?xml version="1.0"?><Flow xmlns="http://soap.sforce.com/2006/04/metadata"><label>Account After Save</label><processType>AutoLaunchedFlow</processType>'
          '<status>Active</status><start><object>Account</object><recordTriggerType>Update</recordTriggerType><triggerType>RecordAfterSave</triggerType></start>'
          '<recordUpdates><object>Account</object><inputAssignments><field>Industry_Segment__c</field></inputAssignments></recordUpdates>'
          '<subflows><flowName>Invoice_Approval</flowName></subflows></Flow>')
    git(root, "add", "-A")
    git(root, "commit", "-qm", "metadata")
    cfg = parse_config(dict(CONFIG), root / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    build_graph(cfg, db)

    rule = db.find_entities("Account.Segment_Required", "rule")[0]
    rout = {(e.kind, n.name) for e, n in db.edges_from(rule.id)}
    assert ("belongs_to", "Account") in rout and ("references", "Account.Industry_Segment__c") in rout
    assert rule.attrs["active"] is True and "Segment is required" in describe(db, rule)
    ps = db.find_entities("Sales_User", "permissionset")[0]
    pout = [(e.kind, n.name, e.attrs) for e, n in db.edges_from(ps.id)]
    assert ("grants", "Invoice__c", {"permissions": ["create", "read"]}) in pout
    assert any(k == "grants" and name == "Account.Industry_Segment__c" and attrs.get("editable") for k, name, attrs in pout)
    assert any(k == "grants" and name == "AccountService" for k, name, _ in pout)
    layout = db.find_entities("Account-Account Layout", "layout")[0]
    assert ("shows", "Account.Industry_Segment__c") in {(e.kind, n.name) for e, n in db.edges_from(layout.id)}
    flow = db.find_entities("Account_After_Save", "flow")[0]
    fout = {(e.kind, n.name) for e, n in db.edges_from(flow.id)}
    assert ("triggers_on", "Account") in fout and ("calls", "Invoice_Approval") in fout
    assert ("references", "Account.Industry_Segment__c") in fout
    assert flow.attrs["trigger_type"] == "RecordAfterSave"
    acct = describe(db, db.find_entities("Account", "object")[0])
    assert "validation rules: Segment_Required" in acct and "record types: Partner" in acct and "layouts: Account-Account Layout" in acct
    fld = describe(db, db.find_entities("Account.Industry_Segment__c", "field")[0])
    assert "on layouts:" in fld and "permission sets: Sales_User" in fld


def test_kotlin_heritage_with_function_type_parameters():
    src = """\
class InMemoryQueue(
  private val clock: Clock,
  override val deadMessageHandlers: List<(Queue, Message) -> Unit>,
  override val publisher: EventPublisher
) : MonitorableQueue, Closeable {
  override fun poll(callback: (Message, () -> Unit) -> Unit) {}
}
"""
    p = parse_file(src, "kotlin")
    assert p.attrs["InMemoryQueue"]["implements"] == ["MonitorableQueue", "Closeable"]
    assert "extends" not in p.attrs["InMemoryQueue"]


def test_production_calls_never_resolve_to_test_methods(tmp_path: Path):
    root = tmp_path / "calls"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "src/main/java/com/x/Handler.java", "package com.x;\npublic class Handler {\n  void run() { log.error(\"x\"); Util.go(); }\n}\n")
    write(root, "src/main/java/com/x/Util.java", "package com.x;\npublic class Util {\n  static void go() {}\n}\n")
    write(root, "src/test/java/com/x/SomeSpec.java", "package com.x;\npublic class SomeSpec {\n  void error() {}\n}\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    cfg = parse_config({"version": 1, "sources": [{"id": "code", "type": "code", "bucket": "knowledge", "paths": ["**/*.java"]}]}, root / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    build_graph(cfg, db)
    run = db.find_entities("Handler.run", "symbol")[0]
    calls = {n.name for e, n in db.edges_from(run.id) if e.kind == "calls"}
    assert calls == {"Util.go"}
