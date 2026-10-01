from pathlib import Path

from ctxgraph.paths import PathMatcher, glob_to_regex, list_repo_files, resolve_files


def test_double_star_matches_any_depth():
    r = glob_to_regex("docs/**/*.md")
    assert r.match("docs/a.md")
    assert r.match("docs/x/y/a.md")
    assert not r.match("a.md")
    assert not r.match("docs/a.txt")


def test_leading_double_star_matches_root_files():
    r = glob_to_regex("**/*.md")
    assert r.match("README.md")
    assert r.match("a/b/README.md")


def test_trailing_double_star():
    r = glob_to_regex("src/**")
    assert r.match("src/a.py")
    assert r.match("src/a/b/c.py")
    assert not r.match("srcx/a.py")


def test_single_star_is_segment_bound():
    r = glob_to_regex("docs/*.md")
    assert r.match("docs/a.md")
    assert not r.match("docs/x/a.md")


def test_literal_directory_matches_contents():
    r = glob_to_regex("docs")
    assert r.match("docs")
    assert r.match("docs/a/b.md")
    assert not r.match("docs2/a.md")


def test_literal_file_and_dot_prefix():
    r = glob_to_regex("./CLAUDE.md")
    assert r.match("CLAUDE.md")
    assert not r.match("sub/CLAUDE.md")


def test_exclude_wins():
    m = PathMatcher(["**/*.md"], ["**/node_modules/**"])
    assert m.matches("docs/a.md")
    assert not m.matches("x/node_modules/pkg/README.md")


def test_resolve_files_preserves_order():
    files = ["b.md", "a.md", "c.txt"]
    assert resolve_files(files, ["*.md"]) == ["b.md", "a.md"]


def test_list_repo_files_honours_gitignore(repo: Path):
    files = list_repo_files(repo)
    assert "CONTRIBUTING.md" in files
    assert "docs/adr/0001-sqlite.md" in files
    assert "ignored/secret.md" not in files


def test_list_repo_files_without_git(tmp_path: Path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "junk").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.md").write_text("x")
    assert list_repo_files(tmp_path) == ["a.md"]
