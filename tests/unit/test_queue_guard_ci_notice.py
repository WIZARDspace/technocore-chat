"""`ci-not-run`'s lifecycle: which observations may remove the CI-has-not-run notice.

The notice is a single upserted comment, so every bug in it is a bug about *when the
comment is deleted* rather than about what it says. Two of those were found on review, and
both fail silently -- the workflow goes green, and the only symptom is a thread that says
the wrong thing about whether CI ran.

The first is a three-state problem written as a boolean. `ci-not-run` polls for the head's
CI run, and "no run visible yet" is not the same observation as "a run exists and is not
gated": the first is ignorance, the second is evidence. Collapsing them means a synchronize
whose new run has not surfaced within the poll deletes the *previous* head's notice, and
nothing puts it back -- this job fires only on opened/reopened/synchronize, and
`ci-not-run-clear` ignores `action_required` by design.

The second is that every read here is a snapshot. Approving a gated run emits no event this
job can see, so the run can leave `action_required` between the read and the write, and the
job then posts a notice that is already false. Re-reading *before* the write only narrows
that window; the reconcile has to come after the write, because the write is what makes the
stale notice exist.

Those two are pinned structurally. The third is not, because it was found by running the
scripts: the notice is one comment shared by every head of the PR, so an older head's job
-- its post-write cleanup, or ci-not-run-clear for its completed run -- could delete the
notice a newer, still-gated head had just written. That is an interleaving of two jobs, and
no assertion about one script's text can see it, so `tests/queue_guard_ci_notice.mjs`
executes both scripts unchanged against an in-memory GitHub API and replays it. A job now
removes its own head's notice, or another head's only while its head is still the PR's;
the second half is replayed too, because without it a notice an older head left is
stranded once the current head runs. The fixture needs only `node` (no npm install;
`tests/package.json` stays Playwright's) and is skipped without one, except under CI, where
a missing Node is a failure rather than a pass. Raised by @renhi and @luch91.

Parsed with a regex rather than a YAML library, for the reason
`tests/unit/test_browser_gate.py` gives: PyYAML is here only as somebody else's transitive
dependency, and these tests should not acquire one.

Run: uv run --group dev python -m pytest tests/unit/test_queue_guard_ci_notice.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "queue-guard.yml"
FIXTURE = ROOT / "tests" / "queue_guard_ci_notice.mjs"

# The `ci-not-run` job's inline script, up to the next job at the same indentation.
# `ci-not-run-clear` is a separate lifecycle with its own rules and must not be caught here.
_JOB = re.compile(r"^  ci-not-run:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n)", re.M | re.S)
_SCRIPT = re.compile(r"^ *script: \|\n(.*)", re.M | re.S)


def _ci_not_run_script() -> str:
    job = _JOB.search(WORKFLOW.read_text(encoding="utf-8"))
    assert job, "the ci-not-run job is no longer findable in queue-guard.yml"
    script = _SCRIPT.search(job.group(1))
    assert script, "the ci-not-run job no longer has an inline `script:` block"
    return script.group(1)


def test_an_unobserved_run_is_not_evidence_and_never_deletes_the_notice():
    """The regression: `blocked === false` once meant both "observed, not gated" and "not
    observed at all", and both deleted. A PR whose replacement head is still waiting for
    approval then loses the warning at the moment it is most true, and stays silent until
    some other PR action happens to re-run the job."""
    script = _ci_not_run_script()
    assert "if (state === 'unknown') return;" in script, (
        "the unobserved case must return before anything is written or removed; "
        "a boolean that folds it in with 'the run is fine' is the bug this pins"
    )
    guard = script.index("if (state === 'unknown') return;")
    first_delete = script.index("deleteComment")
    assert guard < first_delete, (
        "an unobserved run reaches a deleteComment: the early return must precede "
        "every delete path, or ignorance deletes on the strength of nothing"
    )


def test_the_three_observations_stay_distinct():
    """Naming them is the fix; a later edit that reduces them to two states brings the
    bug back with the tests still passing, because every other assertion here is about
    ordering rather than about how many cases exist."""
    script = _ci_not_run_script()
    for state in ("'unknown'", "'gated'", "'ran'"):
        assert state in script, f"the {state} observation is gone from ci-not-run"


def test_the_gated_path_rechecks_the_run_after_it_writes():
    """Approving a run emits no event this job sees, so the notice can be false the moment
    it is posted. The re-read has to sit after the write: before it, this is still
    check-then-act with a shorter gap, which is the same defect measured differently."""
    script = _ci_not_run_script()
    write = max(script.rindex("createComment"), script.rindex("updateComment"))
    recheck = script.rindex("listWorkflowRunsForRepo")
    assert recheck > write, (
        "the run state is never re-read after the notice is written, so a run approved "
        "during the job leaves a notice saying it was not"
    )
    assert script.rindex("deleteComment") > recheck, (
        "the post-write re-read reaches no delete, so it observes the stale notice "
        "and leaves it in place"
    )


@pytest.fixture(scope="module")
def replayed() -> dict[str, str | None]:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH; the ci-not-run interleavings did not run")
        pytest.skip("node is not on PATH")
    out = subprocess.run([node, str(FIXTURE)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_lifecycle_still_posts_and_clears(replayed):
    """Controls, so the race tests below cannot pass by a fixture that never posts."""
    assert replayed["gated_posts"] == "aaa", "a gated head gets a notice naming it"
    assert replayed["approval_clears"] is None, "a completed run clears its head's notice"
    assert replayed["ran_head_clears_its_own"] is None, "a head seen running clears its own"


def test_an_older_heads_cleanup_leaves_a_newer_heads_notice(replayed):
    """Head A's job posts, the PR moves to B and B's job posts, then A's run leaves
    `action_required` and A's post-write re-read sees it. Deleting there removed B's
    warning while B was still gated, and nothing would put it back."""
    assert replayed["older_cleanup_after_newer_write"] == "bbb"


def test_clearing_an_older_heads_run_leaves_a_newer_heads_notice(replayed):
    """The same race through ci-not-run-clear, which sits outside the posting job's
    concurrency group: its open-PR list can still show head A when A's run completes."""
    assert replayed["clear_for_older_head_after_newer_write"] == "bbb"


def test_a_newer_heads_run_clears_a_notice_an_older_head_left(replayed):
    """The other side of head identity. A synchronize whose run has not surfaced within the
    poll leaves the previous head's notice standing, by the 'unknown' rule; once the PR's
    current head has run, that notice is false and must go. A rule of "only ever your own
    head's notice" stranded it for good, through both jobs."""
    assert replayed["newer_head_run_clears_an_older_heads_notice"] is None
    assert replayed["newer_head_seen_running_clears_an_older_heads_notice"] is None


def test_posting_jobs_are_serialised_per_pr():
    """Head identity stops a wrong delete; the concurrency group stops two posting jobs
    reading and writing the same comment at once, which head identity alone narrows but
    does not close (a list, then a newer head's update, then a delete by id)."""
    job = _JOB.search(WORKFLOW.read_text(encoding="utf-8"))
    assert job
    assert re.search(
        r"^    concurrency:\n"
        r"      group: queue-guard-ci-not-run-\$\{\{ github\.event\.pull_request\.number \}\}\n"
        r"      cancel-in-progress: false\n",
        job.group(1),
        re.M,
    ), "ci-not-run is no longer serialised per pull request"
