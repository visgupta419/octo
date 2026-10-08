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


def test_kotlin_trailing_lambdas_are_not_declarations():
    src = """\
class RunTaskHandler(private val repo: ExecutionRepository) : OrcaMessageHandler<RunTask> {
    companion object {
        const val X = 1
    }

    override fun handle(message: RunTask) {
        message.withLocking {
            withTask(message) { stage, task ->
                stage.tasks.forEach { log.info(it.name) }
            }
        }
    }

    private fun trackResult(result: TaskResult) {
        val tags = hashMapOf("status" to result.status) { it }
    }

    val timeout: Long get() { return 1L }

    private val StageExecution.isRetryable: Boolean get() { return true }
}
"""
    from ctxgraph.ingest.code import list_declarations

    decls = [(d.kind, d.qualified) for d in list_declarations(src.splitlines(), "kotlin")]
    assert decls == [
        ("class", "RunTaskHandler"),
        ("object", "RunTaskHandler.companion"),
        ("function", "RunTaskHandler.handle"),
        ("function", "RunTaskHandler.trackResult"),
        ("property", "RunTaskHandler.timeout"),
        ("property", "RunTaskHandler.isRetryable"),
    ]


def test_groovy_spock_and_closures():
    src = """\
class ExecutionRepositorySpec extends Specification {
    @Subject ExecutionRepository repo = createRepo()

    def "stores and retrieves a pipeline"() {
        given:
        def pipeline = pipeline { stage { type = "deploy" } }
        when:
        repo.store(pipeline)
        then:
        pipeline.stages.each { assert it.id }
    }

    void helper(String name) {
        [1, 2].collect { it * 2 }
    }
}
"""
    from ctxgraph.ingest.code import list_declarations

    decls = [(d.kind, d.qualified) for d in list_declarations(src.splitlines(), "groovy")]
    assert decls == [
        ("class", "ExecutionRepositorySpec"),
        ("function", "ExecutionRepositorySpec.stores and retrieves a pipeline"),
        ("function", "ExecutionRepositorySpec.helper"),
    ]


def test_java_calls_with_blocks_are_not_methods():
    src = """\
public class Foo {
    private final List<String> names = new ArrayList<>();

    Foo(int x) {
        this.x = x;
    }

    public void run() {
        names.forEach(n -> {
            System.out.println(n);
        });
        executor.submit(new Runnable() {
            public void run() { }
        });
        synchronized (lock) { count++; }
    }

    static Map<String, Integer> build(List<String> in) {
        return in.stream().collect(toMap(a -> a, a -> 1, (a, b) -> {
            return a;
        }));
    }
}
"""
    from ctxgraph.ingest.code import list_declarations

    decls = [(d.kind, d.qualified) for d in list_declarations(src.splitlines(), "java")]
    assert decls == [
        ("class", "Foo"),
        ("function", "Foo.Foo"),
        ("function", "Foo.run"),
        ("function", "Foo.build"),
    ]


def test_javascript_methods_still_detected_without_types():
    from ctxgraph.ingest.code import list_declarations

    src = "class A {\n  handleClick() {\n    items.forEach((i) => { use(i); });\n  }\n}\n"
    decls = [(d.kind, d.qualified) for d in list_declarations(src.splitlines(), "javascript")]
    assert decls == [("class", "A"), ("function", "A.handleClick")]
