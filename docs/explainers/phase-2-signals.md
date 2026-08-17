# Phase 2 explained — teaching the gate what it is judging

> Written to be taught from. Phase 2 still contains no AI. It answers one
> question: **how does the gate find out what is being deployed?**

At the end of Phase 1 the gate could halt a pipeline, but it knew nothing. It
got woken up and asked "job 12345, yes or no?" — with no idea what commit was
being deployed, how large the change was, or what state the target was in.

Phase 2 is entirely about supplying those facts, and about being honest when it
cannot.

---

## The one idea to keep (Phase 2.1)

> **An absent signal is not a negative signal.**

"Amazon Inspector reported no vulnerabilities" and "Amazon Inspector was
unreachable" must never produce the same value.

If they do, a broken collector looks *exactly* like a clean bill of health. The
gate then fails **open** — not through a permissive decision, but through a
plausible-looking zero. Nothing errors. Nothing warns. The verdict reads as
confident.

That is the subtlest way a fail-closed system gets defeated, and it is why every
collector returns a status alongside its data:

| Status | Meaning |
| --- | --- |
| `OK` | Collected. Data present. |
| `DEGRADED` | Partial — a truncated page, a gap in a metric window. |
| `UNAVAILABLE` | Tried and failed. **Data is `None`.** |
| `SKIPPED` | Deliberately switched off. Not a fault, not reassurance either. |

Three mechanisms enforce it:

1. `data` is `None` unless status is `OK`. There is no default-valued
   `SecurityFindings()` object lying around to be mistaken for evidence.
2. The invariant is checked at runtime — `OK` with no data, or `UNAVAILABLE`
   *with* data, raises immediately rather than surfacing later as a confidently
   wrong verdict.
3. **Collectors cannot forget to fail closed, because they do not implement the
   failure path.** `collect()` is concrete on the base class; a subclass writes
   only the happy path and may raise freely. A contributor who never reads this
   document still gets it right.

That third one is the design choice worth stealing: make the safe behaviour
structural, not something each author has to remember.

---

## Getting real change context (Phase 2.2)

### Problem 1 — the gate cannot see the code

The gate Lambda has no copy of the repository. No git, no files. Three options:

| Approach | Cost |
| --- | --- |
| Give the gate GitHub API access | A stored secret to manage and rotate |
| Download the build artifact from S3 | S3 permissions plus unzipping logic |
| **Let whoever already has the code count it** | ← chosen |

CodeBuild already has the code — it just built it. So CodeBuild counts, and
passes a note forward.

The note travels as a **CodePipeline output variable**: a stage publishes
values, later stages read them. CodeBuild publishes `CHANGE_CONTEXT_B64`; the
Gate action reads `#{BuildVariables.CHANGE_CONTEXT_B64}`.

```text
  CodeBuild                                      Gate Lambda
  counts the diff  ────►  pipeline variable  ────►  reads it
```

Two easily-missed details:

- An action's output variables cannot be referenced **at all** unless the action
  declares a `namespace`. The console assigns these implicitly, so
  `#{SourceVariables.CommitId}` silently never resolves when you write the
  pipeline as code.
- The whole note must fit in **1,000 characters**.

### Problem 2 — CodeBuild could not see the history either

The genuine surprise of the increment. By default CodePipeline hands CodeBuild a
**zip of the files**, not a git repository. A zip has no history, so `git diff`
has nothing to compare against.

`OutputArtifactFormat = "CODEBUILD_CLONE_REF"` makes CodeBuild perform a real
clone instead. That is also why the build role gained connection permissions —
cloning requires minting a short-lived token from the GitHub connection.

### Problem 3 — the note format breaks on ordinary commit messages

The note is JSON, and CodePipeline fills it in by **literal text substitution**.
So the obvious implementation:

```json
{"commit_message": "#{SourceVariables.CommitMessage}"}
```

Given the commit message `fix the "off by one"`, produces:

```json
{"commit_message": "fix the "off by one""}
```

Broken JSON. The gate can no longer parse its own configuration. Nobody
attacked anything — that is a normal commit message.

**Fix:** base64-encode the entire payload. Base64 emits only letters, digits,
`+`, `/` and `=`, none of which can terminate a JSON string.

This deserves a slide of its own. Everyone worries about prompt injection.
This broke one layer *earlier* — in the config format, before the model was
involved at all — and it would have presented as a random pipeline bug.

### Problem 4 — can the note be trusted?

CodeBuild computes those numbers by running `buildspec.yml`, **a file inside the
repository being judged**. A change can therefore rewrite the code that measures
it: *"this 5,000-line payments rewrite? three lines."*

That is a different attack from prompt injection and arguably worse. Injected
prose is at least visible in the audit record; a forged integer looks exactly
like a real one, and it would corrupt Phase 4's measurements too.

Two responses, neither a complete fix:

**Label every fact with its provenance.**

| Fact | Source | Trust |
| --- | --- | --- |
| commit SHA, branch, author, timestamp | CodePipeline, read from the connection | trusted |
| lines added/removed, files touched | the build's own script | self-reported |
| deploy cadence | not collected yet | absent |

Phase 3 puts "diff statistics are self-reported" into the prompt, so the caveat
lands in the audit record rather than only in a design document.

**Cross-check the commit.** The SHA arrives **twice** — once from CodePipeline,
once inside the build's payload. Disagreement means the build described a
different commit than the one being deployed, and the gate refuses to proceed.

Its limit, stated plainly: this catches "described the wrong commit". It does
**not** catch "correct commit, dishonest line count". Provenance labelling
handles that by making the claim visible, not by verifying it.

### Problem 5 — when the build genuinely cannot count

Shallow clone, root commit, git absent. The script reports
`diff_stats_ok: false` — **not zeros.**

`0 files changed` is a real and meaningful state: an empty commit. It must never
double as "we could not tell". If it did, a broken collector would look
identical to a tiny safe change, and the gate would wave through a large one.

### A real run

Against this repository's own history:

```json
{
  "commit_sha": "dcc3c4bc37e199ff636f0b7df0e610691ca3860c",
  "commit_message": "Phase 1 increment 3: pipeline, executor, buildspec",
  "author": "Poly4",
  "committed_at": "2026-08-12T12:57:39+01:00",
  "diff_stats_ok": true,
  "files_changed": 26,
  "lines_added": 3739,
  "lines_removed": 41,
  "paths": ["CLAUDE.md", "DECISIONS.md", "..."],
  "paths_omitted": 14
}
```

820 base64 characters. Note `paths_omitted: 14` — the payload hit its size
budget, trimmed the file list, **and said so**. It shows 12 paths while still
reporting `files_changed: 26`. A silently truncated list would have looked like
a 12-file change, which is the same failure as a fabricated zero wearing
different clothes.

---

## Which signals are mandatory

Only **change context**. Lose it and there is no subject for a verdict — Phase 3
routes straight to human review without calling the model at all, because asking
a model to assess a change it was never told about produces a fluent, confident,
baseless answer. That is the worst output this system could generate, because it
is indistinguishable from a real one.

Security findings and target health are **degrading, not blocking**. A gate
brittle enough that people route around it is a worse security outcome than a
two-of-three verdict that is honestly labelled as such.

---

## Things computed, not inferred

`is_off_hours`, `is_low_traffic`, `is_fixable` are plain deterministic
functions, not questions for the model.

Anything a normal function can decide should not be delegated to a
probabilistic one. The model cannot get it wrong if it is never asked, and the
eval harness can assert on it.

`is_low_traffic` is the interesting one. A 0% error rate over 12 requests is not
evidence of health — the same small-denominator arithmetic that made the Phase 1
canary look broken. Every rate the gate is shown needs a denominator attached.

---

## Still to come in Phase 2

- **2.3 — Amazon Inspector.** Real security findings.
- **2.4 — Amazon CloudWatch.** Real target health, plus a deploy-cadence
  collector reading the CodeDeploy control plane (`AWS_API` provenance rather
  than self-reported).
- **2.5 — synthesized-real signals.** Deliberately break the demo app so the
  real collectors parse real findings, rather than trusting mocks that agree
  with our assumptions about API shapes.
