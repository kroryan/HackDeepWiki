"""Repository history in the evolution workspace.

Two defects these cover:
  * wikis are built from a ``git clone --depth=1`` (api/data_pipeline.py), so
    the history ingest saw ONE commit and the evolution workspace reported "1
    commit" for a 45-commit repository;
  * the history was only ever a flat list of commits -- nothing said how the
    project actually moved, which is what "evolution" is supposed to answer.

Everything here runs against a REAL git repository and a REAL Engraphis
service on temp files: no mocks, no network (the shallow clone is made from a
``file://`` URL), no model calls.
"""

import subprocess

import pytest

from api.engraphis_integration import (
    _checkpoint_body,
    _history_stride,
    _pick_milestones,
    ensure_deep_clone,
    is_shallow_clone,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@example.com",
    "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


# Disable git's background auto-maintenance for every command in this test.
# Building 45 commits back-to-back otherwise races `gc --auto` / `git
# maintenance`, which forks a detached process that repacks loose objects
# while the next commit is still reading them -- surfacing intermittently on
# CI as "invalid object <sha> ... error: Error building trees" mid-fixture
# (never locally, where the machine is faster). These `-c` overrides turn the
# race off deterministically. protocol.file.allow keeps the file:// shallow
# clone working on git >= 2.38.
_GIT_HARDENING = [
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "commit.gpgSign=false",
    "-c", "protocol.file.allow=always",
]


def _git(cwd, *args, env=None):
    import os

    full = dict(os.environ)
    for k in list(full.keys()):
        if k.startswith("GIT_"):
            full.pop(k)
    full.update(GIT_ENV)
    full.update(env or {})
    out = subprocess.run(["git", *_GIT_HARDENING, "-C", str(cwd), *args],
                         capture_output=True, text=True, env=full)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A 45-commit repository -- the size from the bug report."""
    path = tmp_path / "origin"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    for i in range(45):
        author = "Alice" if i % 3 else "Bob"
        directory = path / ("api" if i % 2 else "src")
        directory.mkdir(exist_ok=True)
        (directory / f"file_{i}.py").write_text(f"# change {i}\n" * (i + 1))
        subject = f"feat: milestone {i}" if i % 10 == 0 else f"chore: step {i}"
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", subject,
             env={"GIT_AUTHOR_NAME": author, "GIT_COMMITTER_NAME": author,
                  "GIT_AUTHOR_DATE": f"2024-01-{(i % 28) + 1:02d}T10:00:00",
                  "GIT_COMMITTER_DATE": f"2024-01-{(i % 28) + 1:02d}T10:00:00"})
    return path


@pytest.fixture
def memory(tmp_path, monkeypatch):
    """A real MemoryService on a throwaway DB, wired in like _ensure_started."""
    pytest.importorskip("engraphis")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "0")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("HACKDEEPWIKI_ENGRAPHIS_EMBEDDER", "0")
    monkeypatch.setenv("HACKDEEPWIKI_DATA_DIR", str(tmp_path / "data"))

    from api import data_root
    monkeypatch.setattr(data_root, "_cached_root", None, raising=False)

    from engraphis.service import MemoryService
    service = MemoryService.create(str(tmp_path / "memory.db"),
                                   embed_model="", embed_dim=384)

    from api import engraphis_integration as eng
    monkeypatch.setattr(eng, "_service", service)
    monkeypatch.setattr(eng, "_start_error", None)
    monkeypatch.setattr(eng, "_bootstrapped", True)
    eng.ensure_workspace("acme_widgets_evolution", "evolution workspace")
    try:
        yield eng
    finally:
        try:
            service.store.conn.close()
        except Exception:
            pass


def _memories(eng, kind):
    rows = eng._service.store.conn.execute(
        "SELECT id, content, metadata FROM memories ORDER BY rowid"
    ).fetchall()
    out = []
    for row in rows:
        if f'"kind": "{kind}"' in (row["metadata"] or "") or \
                f'"kind":"{kind}"' in (row["metadata"] or ""):
            out.append(dict(row))
    return out


def _edges(eng):
    return [(r["a"], r["b"], r["relation"]) for r in
            eng._service.store.conn.execute(
                "SELECT a, b, relation FROM mem_links").fetchall()]


# -- the stride -----------------------------------------------------------

@pytest.mark.parametrize("total,expected", [
    (0, 0), (1, 0), (10, 0),      # nothing to sample yet
    (45, 10), (200, 10),          # every 10 commits
    (400, 20), (600, 30), (1000, 50),
    (2000, 100), (9000, 400),     # capped: the window widens, not the count
])
def test_stride_scales_with_the_repository(total, expected):
    assert _history_stride(total) == expected


def test_checkpoint_count_stays_bounded():
    for total in (200, 400, 600, 1000, 2000, 9000):
        stride = _history_stride(total)
        assert 10 <= total // stride <= 24


# -- one checkpoint -------------------------------------------------------

def _window(n=10):
    return [{"sha": f"{i:040x}", "short": f"{i:07x}", "date": "2024-01-01",
             "author": "Alice" if i % 2 else "Bob",
             "subject": f"feat: thing {i}" if i == 3 else f"chore: step {i}"}
            for i in range(n)]


def test_checkpoint_body_reports_range_churn_and_authors():
    body = _checkpoint_body("acme", "widgets", 3, 20, 45, _window(),
                            "8 files changed, 40 insertions(+)\n60.0% api/")
    assert "checkpoint #3" in body
    assert "commits 21-30 of 45" in body
    assert "8 files changed" in body and "60.0% api/" in body
    assert "Alice (5)" in body and "Bob (5)" in body


def test_checkpoint_body_is_bounded_and_still_names_commits():
    """A wide repo emits a dirstat line per directory; that must not crowd out
    the milestones, which say more than the tail of that list."""
    from api.engraphis_integration import _MAX_CHECKPOINT_LINES

    body = _checkpoint_body("acme", "widgets", 1, 0, 500, _window(100),
                            "\n".join(f"{i}.0% dir_{i}/" for i in range(30)))
    assert len(body.splitlines()) <= _MAX_CHECKPOINT_LINES
    assert "Milestones" in body
    assert "feat: thing 3" in body


def test_milestones_prefer_features_then_spread():
    picks = _pick_milestones(_window(), 3)
    assert "feat: thing 3" in picks
    assert len(picks) == 3 and len(set(picks)) == 3


# -- the whole backfill on a real repo ------------------------------------

def test_backfill_records_every_commit_and_the_progress_arc(memory, repo):
    eng = memory
    head = _git(repo, "rev-parse", "HEAD")
    eng._backfill_history("acme_widgets_evolution", "acme", "widgets",
                          str(repo), head)

    marker = _memories(eng, "history_backfill")
    assert marker and "45 commits" in marker[0]["content"]

    batches = _memories(eng, "commit_history_batch")
    assert len(batches) == 2  # 45 commits at _HISTORY_BATCH=25

    # 45 commits / stride 10 -> four closed windows (the last 5 are still open)
    checkpoints = _memories(eng, "history_checkpoint")
    assert len(checkpoints) == 4
    assert "commits 1-10 of 45" in checkpoints[0]["content"]
    assert "files changed" in checkpoints[0]["content"]  # diffed vs empty tree
    assert "commits 31-40 of 45" in checkpoints[3]["content"]

    edges = _edges(eng)
    overview = _memories(eng, "repo_history_overview")[0]["id"]
    for i in range(1, 4):
        assert (checkpoints[i]["id"], checkpoints[i - 1]["id"], "follows") in edges
    for checkpoint in checkpoints:
        assert (checkpoint["id"], overview, "part_of") in edges


def test_history_limit_is_explicit_in_marker_and_state(memory, repo, monkeypatch):
    eng = memory
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(eng, "_git_history_count", lambda _repo_dir: 2500)

    eng._backfill_history(
        "acme_widgets_evolution", "acme", "widgets", str(repo), head
    )

    marker = _memories(eng, "history_backfill")[0]["content"]
    assert "45 commits (the newest of 2500 reachable commits)" in marker
    state = eng._ingest_state()["acme_widgets_evolution"]["history_backfill"]
    assert state["count"] == 45
    assert state["total_reachable"] == 2500
    assert state["truncated"] is True


def test_checkpoints_extend_instead_of_being_rewritten(memory, repo):
    eng = memory
    head = _git(repo, "rev-parse", "HEAD")
    eng._backfill_history("acme_widgets_evolution", "acme", "widgets",
                          str(repo), head)
    assert len(_memories(eng, "history_checkpoint")) == 4

    # A second save with no new commits must not write anything again.
    eng._backfill_history("acme_widgets_evolution", "acme", "widgets",
                          str(repo), head)
    assert len(_memories(eng, "history_checkpoint")) == 4

    # Five more commits close the fifth window.
    for i in range(45, 50):
        (repo / "api" / f"file_{i}.py").write_text(f"# later {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"feat: later work {i}")
    new_head = _git(repo, "rev-parse", "HEAD")
    eng._backfill_history("acme_widgets_evolution", "acme", "widgets",
                          str(repo), new_head)

    checkpoints = _memories(eng, "history_checkpoint")
    assert len(checkpoints) == 5
    assert "commits 41-50 of 50" in checkpoints[4]["content"]
    assert (checkpoints[4]["id"], checkpoints[3]["id"], "follows") in _edges(eng)
    state = eng._ingest_state()["acme_widgets_evolution"]["history_backfill"]
    assert state["count"] == 50
    assert state["total_reachable"] == 50
    assert state["truncated"] is False


def test_truncated_history_metadata_survives_incremental_updates(
    memory, repo, monkeypatch
):
    eng = memory
    ws = "acme_widgets_evolution"
    reachable = 2500
    monkeypatch.setattr(
        eng, "_git_history_count", lambda _repo_dir: reachable
    )
    eng._backfill_history(
        ws, "acme", "widgets", str(repo), _git(repo, "rev-parse", "HEAD")
    )

    (repo / "api" / "new_after_limit.py").write_text("# new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: after bounded history")
    reachable = 2501
    eng._backfill_history(
        ws, "acme", "widgets", str(repo), _git(repo, "rev-parse", "HEAD")
    )

    state = eng._ingest_state()[ws]["history_backfill"]
    assert state["count"] == 46
    assert state["total_reachable"] == 2501
    assert state["truncated"] is True
    marker = _memories(eng, "history_backfill")[-1]["content"]
    assert "46 ingested of 2501 reachable commits" in marker


def test_partial_checkpoint_stays_open_until_the_window_is_complete(memory, repo):
    """Regression for 74314d4: ceil(total/stride) both indexed past the record
    list and, if merely clamped, marked a partial window done forever."""
    eng = memory
    ws = "acme_widgets_evolution"
    head = _git(repo, "rev-parse", "HEAD")
    eng._backfill_history(ws, "acme", "widgets", str(repo), head)
    assert len(_memories(eng, "history_checkpoint")) == 4
    assert eng._ingest_state()[ws]["checkpoints"]["done"] == 4

    # 46-49 remain an open tail: no checkpoint and no out-of-range access.
    for i in range(45, 49):
        (repo / "api" / f"partial_{i}.py").write_text(f"# partial {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"work in progress {i}")
    eng._backfill_history(
        ws, "acme", "widgets", str(repo), _git(repo, "rev-parse", "HEAD")
    )
    assert len(_memories(eng, "history_checkpoint")) == 4
    assert eng._ingest_state()[ws]["checkpoints"]["done"] == 4

    # Commit 50 closes exactly one immutable 41-50 checkpoint.
    (repo / "api" / "partial_49.py").write_text("# complete\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: complete the window")
    eng._backfill_history(
        ws, "acme", "widgets", str(repo), _git(repo, "rev-parse", "HEAD")
    )
    checkpoints = _memories(eng, "history_checkpoint")
    assert len(checkpoints) == 5
    assert "commits 41-50 of 50" in checkpoints[-1]["content"]
    assert eng._ingest_state()[ws]["checkpoints"]["done"] == 5


def test_non_ascii_commit_subjects_survive_the_ingest(memory, repo):
    """git speaks UTF-8; Python decodes a pipe with the LOCALE encoding, which
    on Windows is cp1252 -- bytes it has no mapping for would raise and lose
    the whole history. The git helpers pin UTF-8 for that reason."""
    eng = memory
    subject = "feat: señal → café ✅ 日本語"
    (repo / "api" / "unicode.py").write_text("# ok\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    head = _git(repo, "rev-parse", "HEAD")

    eng._backfill_history("acme_widgets_evolution", "acme", "widgets",
                          str(repo), head)
    batches = "\n".join(m["content"] for m in
                        _memories(eng, "commit_history_batch"))
    assert subject in batches


# -- the shallow clone that caused "1 commit" -----------------------------

@pytest.fixture
def shallow_clone(tmp_path, repo):
    """Exactly what api/data_pipeline.py produces, made offline."""
    path = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", "--single-branch",
         f"file://{repo}", str(path))
    return path


def test_a_wiki_clone_is_shallow_and_can_be_deepened(shallow_clone):
    assert is_shallow_clone(str(shallow_clone))
    assert len(_git(shallow_clone, "log", "--oneline").splitlines()) == 1

    assert ensure_deep_clone(str(shallow_clone)) is True
    assert not is_shallow_clone(str(shallow_clone))
    assert len(_git(shallow_clone, "log", "--oneline").splitlines()) == 45
    # Idempotent: a deep clone is left alone.
    assert ensure_deep_clone(str(shallow_clone)) is True


def test_a_shallow_first_pass_is_redone_once_history_arrives(memory, shallow_clone):
    """The bug: the first wiki save ingested the one commit a --depth=1 clone
    has, and every later save stepped incrementally from that stump."""
    eng = memory
    ws = "acme_widgets_evolution"
    head = _git(shallow_clone, "rev-parse", "HEAD")

    eng._backfill_history(ws, "acme", "widgets", str(shallow_clone), head)
    assert "1 commits" in _memories(eng, "history_backfill")[0]["content"]
    assert eng._ingest_state()[ws]["history_backfill"]["shallow"] is True

    ensure_deep_clone(str(shallow_clone))
    eng._backfill_history(ws, "acme", "widgets", str(shallow_clone), head)

    state = eng._ingest_state()[ws]["history_backfill"]
    assert state["count"] == 45 and state["shallow"] is False
    # And the workspace SAYS so: the stump's "1 commits" text must not survive
    # as the memory a human reads (Engraphis reinforces near-duplicates).
    assert any("45 commits" in m["content"]
               for m in _memories(eng, "history_backfill"))
    assert len(_memories(eng, "commit_history_batch")) == 3  # 1 + 45/25
    assert len(_memories(eng, "history_checkpoint")) == 4


def test_record_wiki_release_deepens_the_clone_it_is_given(memory, shallow_clone):
    """The end-to-end path save_wiki_cache takes."""
    eng = memory
    head = _git(shallow_clone, "rev-parse", "HEAD")
    eng.record_wiki_release(owner="acme", repo="widgets", repo_type="github",
                            language="en", version=1, repo_commit=head,
                            previous_version=None, previous_commit=None,
                            clone_dir=str(shallow_clone))

    assert not is_shallow_clone(str(shallow_clone))
    assert "45 commits" in _memories(eng, "history_backfill")[0]["content"]
    assert len(_memories(eng, "history_checkpoint")) == 4
