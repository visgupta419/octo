from ctxgraph.config import ChunkingConfig
from ctxgraph.ingest.chunk import chunk_code
from ctxgraph.ingest.code import describe_header, detect_language, identifier_terms, mask_source

SMALL = ChunkingConfig(target_tokens=60, max_tokens=80, min_tokens=5)
# Large enough that a single Apex method is never windowed, small enough that
# the class as a whole must be split by member.
APEX_CFG = ChunkingConfig(target_tokens=120, max_tokens=150, min_tokens=5)

APEX = """\
/**
 * Service layer for Account operations.
 */
public with sharing class AccountService {
    private static final String DEFAULT_TYPE = 'Customer'; // it's got a { brace
    public String Name { get; set; }

    @AuraEnabled(cacheable=true)
    public static List<Account> getAccounts(String industry) {
        List<Account> accs = [SELECT Id, Name FROM Account WHERE Industry = :industry];
        if (accs.isEmpty()) {
            throw new AuraHandledException('No accounts for ' + industry);
        }
        return accs;
    }

    /* Multi-line
       comment with } brace */
    public static void upsertAccounts(List<Account> accounts,
                                      Boolean allOrNone) {
        try {
            Database.upsert(accounts, allOrNone);
        } catch (DmlException e) {
            System.debug('failed: ' + e.getMessage());
        }
    }

    public class AccountWrapper {
        public Account record;
        public AccountWrapper(Account record) { this.record = record; }
    }
}
"""


def test_detect_language():
    assert detect_language("force-app/main/default/classes/A.cls") == "apex"
    assert detect_language("x/AccountTrigger.trigger") == "apex"
    assert detect_language("lwc/accountList/accountList.js") == "javascript"
    assert detect_language("a/b.py") == "python"
    assert detect_language("README") is None
    assert detect_language("weird.xyz") is None


def test_mask_source_hides_strings_and_comments():
    masked = mask_source(["String s = 'a{b'; // c}d", "/* x { */ int y = 1;"], "apex")
    assert "{" not in masked[0] and "}" not in masked[0]
    assert masked[0].startswith("String s = '")
    assert "{" not in masked[1] and masked[1].endswith("int y = 1;")


def test_apex_class_splits_by_member_with_breadcrumbs():
    chunks = chunk_code("classes/AccountService.cls", APEX, APEX_CFG)
    crumbs = [(c.heading, c.meta["kind"]) for c in chunks]
    assert crumbs == [
        ("AccountService", "class"),
        ("AccountService > Name", "property"),
        ("AccountService > getAccounts", "function"),
        ("AccountService > upsertAccounts", "function"),
        ("AccountService > AccountWrapper", "class"),
    ]
    # the javadoc stays with the class header; the block comment with the method
    assert chunks[0].start_line == 1 and "Service layer" in chunks[0].text
    assert chunks[3].text.splitlines()[1].strip().startswith("/* Multi-line")
    assert chunks[2].start_line == 8 and chunks[2].end_line == 15
    assert all(c.meta["language"] == "apex" for c in chunks)
    assert chunks[0].text.startswith("[classes/AccountService.cls > AccountService]\n")
    # every source line is covered exactly once (no overlap between members)
    covered = sorted((c.start_line, c.end_line) for c in chunks)
    assert covered[-1][1] == len(APEX.splitlines())
    for (s1, e1), (s2, _) in zip(covered, covered[1:]):
        assert s2 > e1


def test_small_class_stays_whole():
    chunks = chunk_code("A.cls", APEX)  # default budget: whole class fits
    assert len(chunks) == 1
    assert chunks[0].heading == "AccountService"


def test_apex_trigger_is_named():
    src = "trigger AccountTrigger on Account (before insert) {\n    AccountHandler.run(Trigger.new);\n}\n"
    chunks = chunk_code("AccountTrigger.trigger", src)
    assert chunks[0].heading == "AccountTrigger"
    assert chunks[0].meta["kind"] == "trigger"


def test_javascript_class_methods_and_arrow_functions():
    src = """\
import { LightningElement, api } from 'lwc';

export default class AccountList extends LightningElement {
    @api industry = 'Technology';

    handleRefresh() {
        this.template.querySelector('c-child').refresh();
    }

    get hasRows() {
        return this.rows && this.rows.length > 0;
    }

    handleRowAction = (event) => {
        this.dispatchEvent(new CustomEvent('select', { detail: event.detail.row }));
    };
}

export function helper(x) {
    return `template ${x} with } brace
    spanning lines`;
}
"""
    tiny = ChunkingConfig(target_tokens=30, max_tokens=40, min_tokens=3)
    crumbs = [c.heading for c in chunk_code("accountList.js", src, tiny)]
    assert crumbs == [
        "",
        "AccountList",
        "AccountList > handleRefresh",
        "AccountList > hasRows",
        "AccountList > handleRowAction",
        "helper",
    ]


def test_python_splits_by_def_and_class():
    src = """\
import os


def top_level(a, b):
    return a + b


class Service:
    retries = 3

    def __init__(self, db):
        self.db = db

    @staticmethod
    def helper(x):
        return x * 2
"""
    tiny = ChunkingConfig(target_tokens=20, max_tokens=25, min_tokens=3)
    chunks = chunk_code("svc.py", src, tiny)
    crumbs = [c.heading for c in chunks]
    assert crumbs == ["", "top_level", "Service", "Service > __init__", "Service > helper"]
    helper = chunks[-1]
    assert helper.text.splitlines()[1].strip() == "@staticmethod"
    assert chunks[1].start_line == 4


def test_unknown_language_falls_back_to_text():
    chunks = chunk_code("notes.unknown", "just some text\n\nmore\n")
    assert len(chunks) == 1 and chunks[0].heading == ""
    assert "language" not in chunks[0].meta


def test_describe_header_cases():
    cases = {
        "public with sharing class AccountService": ("class", "AccountService"),
        "trigger T on Account (before insert)": ("trigger", "T"),
        "if (foo(x))": (None, None),
        "} else if (bar())": (None, None),
        "@AuraEnabled(cacheable=true) public static void go()": ("function", "go"),
        "public String Name": ("property", "Name"),
        "switch on x": (None, None),
        "static": (None, None),
        "func (r *Repo) Get(ctx context.Context) error": ("function", "Get"),
        "fn parse(input: &str) -> Result<T>": ("function", "parse"),
        "export default class AccountList extends LightningElement": ("class", "AccountList"),
        "const handler = async (e) =>": ("function", "handler"),
        "this.dispatchEvent(new CustomEvent('select', ": (None, None),
        "return new Runnable()": (None, None),
        "for (Account a : accs)": (None, None),
        "accounts.forEach(a ->": (None, None),
        "public AccountWrapper(Account record)": ("function", "AccountWrapper"),
        "@wire(getAccounts, ": (None, None),
        "get hasRows()": ("function", "hasRows"),
        "def run(self)": ("function", "run"),
        "int main(void)": ("function", "main"),
        "import ": (None, None),
    }
    for header, expected in cases.items():
        assert describe_header(header) == expected, header


def test_identifier_terms_split_camel_and_snake():
    terms = identifier_terms("AccountService.getAccounts(ad_decision, HTTPServer, plain)")
    assert terms.split() == ["account", "service", "get", "accounts", "ad", "decision", "http", "server"]
