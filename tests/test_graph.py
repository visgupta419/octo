from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from conftest import git, write

from ctxgraph.cli import main
from ctxgraph.compile import compile_pack, render_markdown
from ctxgraph.config import parse_config
from ctxgraph.graph import build_graph
from ctxgraph.graph.facts import describe, entities_for_query, facts_for_query
from ctxgraph.ingest import run_ingest
from ctxgraph.retrieve import search
from ctxgraph.store import Database

APEX_SERVICE = """\
public with sharing class AccountService {
    @AuraEnabled(cacheable=true)
    public static List<Account> getAccounts(String industry) {
        return [SELECT Id, Name, Industry_Segment__c FROM Account WHERE Industry = :industry];
    }
    public static void sync(List<Account> accs) {
        BillingClient.push(accs);
        Contact c = new Contact();
    }
}
"""
APEX_BILLING = """\
public class BillingClient {
    public static void push(List<Account> accs) { System.debug(accs); }
}
"""
APEX_TRIGGER = """\
trigger AccountTrigger on Account (before insert) {
    AccountService.sync(Trigger.new);
}
"""
LWC_JS = """\
import { LightningElement, wire } from 'lwc';
import getAccounts from '@salesforce/apex/AccountService.getAccounts';
import NAME_FIELD from '@salesforce/schema/Account.Industry_Segment__c';
export default class AccountList extends LightningElement {
    @wire(getAccounts, { industry: 'Tech' }) accounts;
}
"""
LWC_HTML = "<template><c-account-row></c-account-row></template>\n"
FIELD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>Industry_Segment__c</fullName>
    <label>Industry Segment</label>
    <type>Picklist</type>
</CustomField>
"""
LOOKUP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">
    <fullName>Parent_Account__c</fullName>
    <label>Parent Account</label>
    <type>Lookup</type>
    <referenceTo>Account</referenceTo>
</CustomField>
"""
OBJECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Invoice</label>
</CustomObject>
"""
FLOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Invoice Approval</label>
    <processType>AutoLaunchedFlow</processType>
    <status>Active</status>
    <start><object>Invoice__c</object></start>
    <actionCalls><actionName>BillingClient</actionName><apexClass>BillingClient</apexClass></actionCalls>
</Flow>
"""
OWNERSHIP = """\
teams:
  - name: platform
    oncall: "#platform-oncall"
    members: [alice]
services:
  - name: account-service
    team: platform
    paths: ["force-app/main/default/classes/Account*.cls", "force-app/main/default/triggers/**"]
    runbook: docs/runbooks/accounts.md
"""
DEPS = "edges:\n  - from: account-service\n    to: billing-service\n    kind: apex\n"
CONFIG = {
    "version": 1,
    "sources": [
        {"id": "docs", "type": "markdown", "bucket": "knowledge", "paths": ["**/*.md"]},
        {"id": "apex", "type": "code", "bucket": "knowledge", "paths": ["**/*.cls", "**/*.trigger"]},
        {"id": "lwc", "type": "code", "bucket": "knowledge", "paths": ["**/lwc/**/*.js", "**/lwc/**/*.html"]},
        {"id": "meta", "type": "text", "bucket": "knowledge", "paths": ["**/objects/**/*.xml", "**/*.flow-meta.xml"]},
    ],
}


def _sf_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sf"
    root.mkdir()
    git(root, "init", "-q")
    base = "force-app/main/default"
    write(root, "sfdx-project.json", "{}")
    write(root, f"{base}/classes/AccountService.cls", APEX_SERVICE)
    write(root, f"{base}/classes/BillingClient.cls", APEX_BILLING)
    write(root, f"{base}/triggers/AccountTrigger.trigger", APEX_TRIGGER)
    write(root, f"{base}/lwc/accountList/accountList.js", LWC_JS)
    write(root, f"{base}/lwc/accountList/accountList.html", LWC_HTML)
    write(root, f"{base}/lwc/accountRow/accountRow.js", "import { LightningElement } from 'lwc';\nexport default class AccountRow extends LightningElement {}\n")
    write(root, f"{base}/objects/Account/fields/Industry_Segment__c.field-meta.xml", FIELD_XML)
    write(root, f"{base}/objects/Invoice__c/Invoice__c.object-meta.xml", OBJECT_XML)
    write(root, f"{base}/objects/Invoice__c/fields/Parent_Account__c.field-meta.xml", LOOKUP_XML)
    write(root, f"{base}/flows/Invoice_Approval.flow-meta.xml", FLOW_XML)
    write(root, "ctx/ownership.yaml", OWNERSHIP)
    write(root, "ctx/dependencies.yaml", DEPS)
    write(root, "CODEOWNERS", "*.trigger @triggers-team\n")
    write(root, "README.md", "# Org\n\nAccountService feeds BillingClient. The Invoice__c object tracks invoices.\n")
    git(root, "add", "-A")
    git(root, "-c", "user.name=alice", "-c", "user.email=alice@x.com", "commit", "-qm", "init")
    # a second commit touching service + trigger together (co-change)
    write(root, f"{base}/classes/AccountService.cls", APEX_SERVICE + "// v2\n")
    write(root, f"{base}/triggers/AccountTrigger.trigger", APEX_TRIGGER + "// v2\n")
    git(root, "add", "-A")
    git(root, "-c", "user.name=bob", "-c", "user.email=bob@x.com", "commit", "-qm", "sync change")
    write(root, f"{base}/classes/AccountService.cls", APEX_SERVICE + "// v3\n")
    write(root, f"{base}/triggers/AccountTrigger.trigger", APEX_TRIGGER + "// v3\n")
    git(root, "add", "-A")
    git(root, "-c", "user.name=bob", "-c", "user.email=bob@x.com", "commit", "-qm", "again")
    return root


def _build(tmp_path: Path):
    root = _sf_repo(tmp_path)
    cfg = parse_config(dict(CONFIG), root / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    report = build_graph(cfg, db)
    return root, cfg, db, report


def test_graph_entities_and_edges(tmp_path: Path):
    _, cfg, db, report = _build(tmp_path)
    assert report.by_type["symbol"] >= 6 and report.by_type["object"] >= 3
    svc = db.find_entities("AccountService", "symbol")[0]
    out = {(e.kind, n.name) for e, n in db.edges_from(svc.id)}
    assert ("references", "Account") in out
    assert ("references", "BillingClient") in out
    assert ("references", "Contact") in out
    assert ("references", "Account.Industry_Segment__c") in out
    assert ("contains", "AccountService.getAccounts") in out
    trig = db.find_entities("AccountTrigger", "symbol")[0]
    assert ("triggers_on", "Account") in {(e.kind, n.name) for e, n in db.edges_from(trig.id)}
    assert ("references", "AccountService") in {(e.kind, n.name) for e, n in db.edges_from(trig.id)}
    # LWC component calls Apex and references the field via schema import
    comp = db.find_entities("accountList", "component")[0]
    cout = {(e.kind, n.name) for e, n in db.edges_from(comp.id)}
    assert ("calls", "AccountService.getAccounts") in cout
    assert ("references", "Account.Industry_Segment__c") in cout
    assert ("uses", "accountRow") in cout
    # metadata
    inv = db.find_entities("Invoice__c", "object")[0]
    assert inv.attrs["label"] == "Invoice" and inv.attrs["standard"] is False
    fld = db.find_entities("Invoice__c.Parent_Account__c", "field")[0]
    fout = {(e.kind, n.name) for e, n in db.edges_from(fld.id)}
    assert ("belongs_to", "Invoice__c") in fout and ("references", "Account") in fout
    flow = db.find_entities("Invoice_Approval", "flow")[0]
    flout = {(e.kind, n.name) for e, n in db.edges_from(flow.id)}
    assert ("references", "Invoice__c") in flout and ("calls", "BillingClient") in flout
    assert flow.attrs["status"] == "Active"


def test_ownership_codeowners_dependencies_and_history(tmp_path: Path):
    _, cfg, db, report = _build(tmp_path)
    svc = db.find_entities("account-service", "service")[0]
    out = {(e.kind, n.name) for e, n in db.edges_from(svc.id)}
    assert ("depends_on", "billing-service") in out
    assert ("contains", "force-app/main/default/classes/AccountService.cls") in out
    team = db.find_entities("platform", "team")[0]
    assert ("owns", "account-service") in {(e.kind, n.name) for e, n in db.edges_from(team.id)}
    f = db.get_entity("file:force-app/main/default/classes/AccountService.cls")
    assert f.attrs["owner"] == "platform" and f.attrs["service"] == "account-service"
    assert f.attrs["commits"] == 3 and f.attrs["last_author"] == "bob"
    trig_file = db.get_entity("file:force-app/main/default/triggers/AccountTrigger.trigger")
    owners = {n.name for e, n in db.edges_to(trig_file.id) if e.kind == "owns"}
    assert "triggers-team" in owners
    co = {n.name: e.attrs["count"] for e, n in db.edges_from(f.id) + db.edges_to(f.id) if e.kind == "co_changed"}
    assert co["force-app/main/default/triggers/AccountTrigger.trigger"] == 3  # init + two edits
    bob = db.find_entities("bob", "person")[0]
    assert {n.name for e, n in db.edges_from(bob.id) if e.kind == "authored"} >= {f.name, trig_file.name}
    assert report.commits == 3


def test_mentions_and_describe(tmp_path: Path):
    _, cfg, db, _ = _build(tmp_path)
    svc = db.find_entities("AccountService", "symbol")[0]
    assert db.mentions_of(svc.id) == ["README.md"]
    line = describe(db, svc)
    assert line.startswith("AccountService — apex class, force-app/main/default/classes/AccountService.cls#L1-")
    assert "members: getAccounts, sync" in line
    assert "references:" in line and "BillingClient" in line
    assert "referenced by:" in line and "AccountTrigger" in line
    assert "owner: platform" in line
    assert "last changed" in line and "by bob (3 commits)" in line
    assert "co-changes with: AccountTrigger.trigger" in line
    assert "mentioned in: README.md" in line
    obj = describe(db, db.find_entities("Account", "object")[0])
    assert obj.startswith("Account — standard sObject")
    assert "fields: Industry_Segment__c" in obj and "triggers: AccountTrigger" in obj


def test_facts_in_pack_from_query_and_hits(tmp_path: Path):
    _, cfg, db, _ = _build(tmp_path)
    hits = search(db, "how does the account trigger sync accounts")
    ents = entities_for_query(db, "how does the account trigger sync accounts", hits)
    names = [e.name for e in ents]
    assert "Account" in names  # query word matches the object
    assert any(n.startswith("AccountTrigger") or n.startswith("AccountService") for n in names)
    facts = facts_for_query(db, "AccountService.getAccounts", [])
    assert facts and facts[0].startswith("AccountService.getAccounts —")
    pack = compile_pack("q", hits, 4000, facts=facts_for_query(db, "AccountTrigger", hits))
    assert pack.facts and pack.used_tokens > sum(c.token_count for c in pack.chunks)
    md = render_markdown(pack)
    assert md.index("## Facts") < md.index("## Knowledge")
    tiny = compile_pack("q", hits, 60, facts=["x " * 200, "short fact"])
    assert tiny.facts == ["short fact"] or tiny.facts == []  # oversized fact lines are dropped


def test_cli_graph_stats_and_rebuild(tmp_path: Path):
    root = _sf_repo(tmp_path)
    runner = CliRunner()
    res = runner.invoke(main, ["init", "--path", str(root)])
    assert res.exit_code == 0 and "Salesforce" in res.output
    assert (root / "ctx" / "ownership.yaml").read_text() == OWNERSHIP  # existing file untouched
    cfg = str(root / "ctxgraph.yaml")
    import yaml
    data = yaml.safe_load(Path(cfg).read_text())
    data["embedding"] = {"provider": "none"}
    Path(cfg).write_text(yaml.safe_dump(data))
    res = runner.invoke(main, ["-c", cfg, "ingest"])
    assert res.exit_code == 0, res.output
    assert "graph:" in res.output and "symbol=" in res.output
    res = runner.invoke(main, ["-c", cfg, "query", "AccountService getAccounts", "--budget", "1500"])
    assert res.exit_code == 0, res.output
    assert "## Facts" in res.output and "AccountService —" in res.output
    res = runner.invoke(main, ["-c", cfg, "graph", "Account", "-t", "object"])
    assert res.exit_code == 0, res.output
    assert "object: Account — standard sObject" in res.output and "<- triggers_on: AccountTrigger" in res.output
    res = runner.invoke(main, ["-c", cfg, "graph", "nope"])
    assert res.exit_code != 0 and "no entity" in res.output
    res = runner.invoke(main, ["-c", cfg, "query", "zzzz qqqq"])
    assert res.exit_code == 0
    res = runner.invoke(main, ["-c", cfg, "stats"])
    assert res.exit_code == 0, res.output
    assert "queries logged: 2" in res.output and "zero_hits" in res.output

    # an index from an older schema is rebuilt by ingest and refused by query
    db_path = root / ".ctxgraph" / "index.db"
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    res = runner.invoke(main, ["-c", cfg, "stats"])
    assert res.exit_code != 0 and "schema version" in res.output
    res = runner.invoke(main, ["-c", cfg, "ingest"])
    assert res.exit_code == 0 and "rebuilding" in res.output


def test_references_disambiguate_by_import_and_package(tmp_path: Path):
    root = tmp_path / "jvm"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "api/src/main/java/com/x/api/Task.java", "package com.x.api;\npublic interface Task {\n  void run();\n}\n")
    write(root, "model/src/main/java/com/x/model/Task.java", "package com.x.model;\npublic class Task {\n  public String id;\n}\n")
    write(root, "core/src/main/java/com/x/core/ApiUser.java",
          "package com.x.core;\nimport com.x.api.Task;\npublic class ApiUser {\n  Task t;\n}\n")
    write(root, "core/src/main/java/com/x/core/ModelUser.java",
          "package com.x.core;\nimport com.x.model.*;\npublic class ModelUser {\n  Task t;\n}\n")
    write(root, "model/src/main/java/com/x/model/Sibling.java",
          "package com.x.model;\npublic class Sibling {\n  Task t;\n}\n")
    write(root, "core/src/main/java/com/x/core/Unknown.java",
          "package com.x.core;\npublic class Unknown {\n  Task t;\n}\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    cfg = parse_config({"version": 1, "sources": [{"id": "code", "type": "code", "bucket": "knowledge", "paths": ["**/*.java"]}]}, root / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    build_graph(cfg, db)

    def refs(name):
        e = db.find_entities(name, "symbol")[0]
        return {n.id for edge, n in db.edges_from(e.id) if edge.kind == "references"}

    assert refs("ApiUser") == {"symbol:Task@api/src/main/java/com/x/api/Task.java"}
    assert refs("ModelUser") == {"symbol:Task@model/src/main/java/com/x/model/Task.java"}
    assert refs("Sibling") == {"symbol:Task@model/src/main/java/com/x/model/Task.java"}
    assert refs("Unknown") == set()  # ambiguous with no import: no guess


def test_fact_selection_prefers_important_production_classes(tmp_path: Path):
    root = tmp_path / "jvm2"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "src/main/java/com/x/Stage.java", "package com.x;\npublic class Stage {\n  public void go() {}\n}\n")
    for i in range(3):
        write(root, f"src/main/java/com/x/User{i}.java", f"package com.x;\npublic class User{i} {{\n  Stage s;\n}}\n")
    write(root, "src/test/java/com/x/other/Stage.java", "package com.x.other;\npublic class Stage {\n  int unused;\n}\n")
    write(root, "src/main/java/com/x/Run.java", "package com.x;\npublic class Run {\n  int x;\n}\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    cfg = parse_config({"version": 1, "sources": [{"id": "code", "type": "code", "bucket": "knowledge", "paths": ["**/*.java"]}]}, root / "ctxgraph.yaml")
    db = Database(cfg.db_path)
    run_ingest(cfg, db)
    build_graph(cfg, db)
    ents = entities_for_query(db, "how does a stage run", [])
    assert [e.id for e in ents] == ["symbol:Stage@src/main/java/com/x/Stage.java"]  # test-only Stage and unreferenced Run skipped
    ents = entities_for_query(db, "Run", [])
    assert [e.name for e in ents] == ["Run"]  # identifier-like casing: exact match wins regardless
