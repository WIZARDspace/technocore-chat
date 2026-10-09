// Executes queue-guard.yml's ci-not-run and ci-not-run-clear scripts, unchanged, against an
// in-memory GitHub API, and prints what each scenario leaves on the thread as JSON.
// tests/unit/test_queue_guard_ci_notice.py runs it and holds the assertions; this file
// only replays interleavings that a single workflow run cannot produce on demand.
//
// No dependencies: the scripts are lifted out of the YAML block scalars by indentation, and
// `github` is a fake carrying only the calls they make. A script calling anything else
// throws, so a new API call cannot slip past the fixture unmodelled.
//
// Run: node tests/queue_guard_ci_notice.mjs

import { readFileSync } from 'node:fs';

const WORKFLOW = new URL('../.github/workflows/queue-guard.yml', import.meta.url);
const CI = '.github/workflows/ci.yml';
const AsyncFunction = (async () => {}).constructor;

function jobScript(yaml, job) {
  const lines = yaml.split('\n');
  const start = lines.indexOf(`  ${job}:`);
  if (start < 0) throw new Error(`no ${job} job in queue-guard.yml`);
  const at = lines.findIndex((l, i) => i > start && /^ +script: \|$/.test(l));
  const indent = lines[at].indexOf('script') + 2;
  const body = [];
  for (const l of lines.slice(at + 1)) {
    if (l.trim() && l.length - l.trimStart().length < indent) break;
    body.push(l.slice(indent));
  }
  // `setTimeout` is a parameter so the poll's 10 s sleeps cost nothing here: shadowing the
  // global is how the "no run visible yet" path runs without waiting out the real poll.
  return new AsyncFunction('github', 'context', 'setTimeout', body.join('\n'));
}

const yaml = readFileSync(WORKFLOW, 'utf8');
const postJob = jobScript(yaml, 'ci-not-run');
const clearJob = jobScript(yaml, 'ci-not-run-clear');

// One PR, its CI runs by head, and its comments. `hooks.runs` fires on each
// listWorkflowRunsForRepo call, which is where a second job is spliced in.
function world() {
  const w = { head: null, runs: {}, comments: [], nextId: 1, hooks: {} };
  const repo = { owner: 'flop-labs', repo: 'technocore-chat' };
  const pr = () => ({
    number: 735,
    head: { sha: w.head, repo: { full_name: 'fork/technocore-chat' } },
    base: { repo: { full_name: 'flop-labs/technocore-chat' } },
  });
  const github = {
    paginate: async (fn, args) => (await fn(args)).data,
    rest: {
      actions: {
        listWorkflowRunsForRepo: async ({ head_sha }) => {
          if (w.hooks.runs) await w.hooks.runs(head_sha);
          const r = w.runs[head_sha];
          return { data: { workflow_runs: r ? [{ path: CI, head_sha, ...r }] : [] } };
        },
      },
      pulls: {
        list: async () => ({ data: [w.listed ?? pr()] }),
        get: async () => ({ data: pr() }),
      },
      issues: {
        listComments: async () => ({ data: w.comments.map(c => ({ ...c })) }),
        createComment: async ({ body }) => {
          w.comments.push({ id: w.nextId++, body, user: { type: 'Bot' } });
        },
        updateComment: async ({ comment_id, body }) => {
          w.comments.find(c => c.id === comment_id).body = body;
        },
        deleteComment: async ({ comment_id }) => {
          w.comments = w.comments.filter(c => c.id !== comment_id);
        },
      },
    },
  };
  const now = fn => fn();
  w.post = () => postJob(github, { repo, payload: { pull_request: pr() } }, now);
  w.clear = (sha, conclusion) => clearJob(github, {
    repo, payload: { workflow_run: { event: 'pull_request', head_sha: sha, conclusion } },
  }, now);
  w.notice = () => {
    const c = w.comments.find(c => c.body.startsWith('<!-- queue-guard-ci-not-run -->'));
    return c ? (c.body.match(/<!-- head: (\w+) -->/) || [null, 'untagged'])[1] : null;
  };
  return w;
}

const scenarios = {
  // Controls: the lifecycle #735 already had, run rather than parsed.
  async gated_posts() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    await w.post();
    return w.notice();
  },
  async approval_clears() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    await w.post();
    w.runs.aaa = { conclusion: 'success' };
    await w.clear('aaa', 'success');
    return w.notice();
  },
  async ran_head_clears_its_own() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    await w.post();
    w.runs.aaa = { conclusion: 'success' };
    await w.post();
    return w.notice();
  },
  // The race @renhi measured. Head A's job writes its notice; before its post-write
  // re-read, the PR is pushed to B, B's job sees B gated and writes, and A's run stops
  // being action_required. A's cleanup then reads "not gated" for A -- and must not
  // remove a notice that is now about B.
  async older_cleanup_after_newer_write() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    let calls = 0;
    w.hooks.runs = async sha => {
      if (sha !== 'aaa' || ++calls !== 2) return;
      w.hooks.runs = null;
      w.head = 'bbb'; w.runs.bbb = { conclusion: 'action_required' };
      await w.post();
      w.runs.aaa = { conclusion: 'cancelled' };
    };
    await w.post();
    return w.notice();
  },
  // The same shape through ci-not-run-clear: its open-PR list still shows head A when A's
  // run completes, while the thread already carries B's notice.
  async clear_for_older_head_after_newer_write() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    await w.post();
    w.listed = { number: 735, head: { sha: 'aaa' } };
    w.head = 'bbb'; w.runs.bbb = { conclusion: 'action_required' };
    await w.post();
    w.runs.aaa = { conclusion: 'success' };
    await w.clear('aaa', 'success');
    return w.notice();
  },
  // Head identity must not strand a notice. A is gated and noticed; the PR moves to B,
  // whose run has not surfaced within the poll, so B's job leaves A's notice standing (the
  // 'unknown' rule). When B's run is approved and completes, the PR's current head has
  // run: a notice about a head the PR has left is false, and must go.
  async newer_head_run_clears_an_older_heads_notice() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    await w.post();
    w.head = 'bbb';
    await w.post();
    w.runs.bbb = { conclusion: 'success' };
    await w.clear('bbb', 'success');
    return w.notice();
  },
  // The same through the posting job: a synchronize to B whose run is seen running.
  async newer_head_seen_running_clears_an_older_heads_notice() {
    const w = world();
    w.head = 'aaa'; w.runs.aaa = { conclusion: 'action_required' };
    await w.post();
    w.head = 'bbb'; w.runs.bbb = { conclusion: null };
    await w.post();
    return w.notice();
  },
};

const out = {};
for (const [name, run] of Object.entries(scenarios)) out[name] = await run();
console.log(JSON.stringify(out));
