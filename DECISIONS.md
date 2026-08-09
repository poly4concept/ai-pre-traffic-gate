# Architectural decisions

Running log of choices and *why*. The reasoning matters more than the choice —
it is the substance of the talk.

Format: what was decided, what the alternatives were, why this one won, and
what would make us revisit.

---

## D-001 — Terraform for infrastructure as code

**Decided:** Terraform (>= 1.11) for all infrastructure.

**Alternatives:** AWS CDK, AWS SAM, CloudFormation.

**Why:** Existing preference and existing fluency. SAM would be a better fit
for the Lambda-and-pipeline core specifically, but the project also needs
Budgets, Inspector wiring and IAM across several services, and mixing two IaC
tools for one demo is a worse story on stage than one slightly verbose tool.

**Revisit if:** the CodeDeploy canary wiring in Terraform proves materially
harder than SAM's `AutoPublishAlias` + `DeploymentPreference` shorthand. Note
that SAM generates the same CodeDeploy resources underneath.

---

## D-002 — Region: us-east-1

**Decided:** us-east-1 for everything.

**Alternatives:** eu-west-1, af-south-1 (closest to the Lagos venue).

**Why:** Bedrock model and inference-profile coverage is widest in us-east-1
and it is the cheapest region. Latency from Lagos is irrelevant — the gate is
Lambda-to-Lambda inside AWS, with no interactive user in the loop. af-south-1
has limited Bedrock availability and would likely have blocked Phase 3.

**Revisit if:** the company environment has a data-residency requirement. This
is worth naming on stage: the demo's region choice is a cost-and-availability
call that a real deployment might not be free to make.

---

## D-003 — Terraform state in S3 with native locking, no DynamoDB lock table

**Decided:** S3 backend with `use_lockfile = true`. No DynamoDB lock table.

**Why:** Terraform 1.10+ supports S3-native state locking via a `.tflock`
object written next to the state. Most tutorials still tell you to create a
DynamoDB table; that is now obsolete. One fewer resource to create, explain
and tear down.

**Consequence:** anyone with read-only credentials must run
`terraform plan -lock=false`, because acquiring the lock is an S3 *write*.

---

## D-004 — Two-identity split: read-only agent, human-held admin

**Decided:** Claude operates as IAM user `ai-agent` (`ReadOnlyAccess` plus a
narrow Bedrock invoke policy). All `terraform apply` runs are performed by a
human using the `poly4` admin profile in a separate terminal.

**Why:** The project's central thesis is that an autonomous component should
hold the narrowest possible permissions and never be able to cause a deploy on
its own. Applying that to the *construction* of the system, not just its
runtime, keeps the argument honest. It also means a bad suggestion from the
agent is caught at human review rather than at apply time.

**Enforced by:** `ReadOnlyAccess` on the IAM side; `AWS_PROFILE=ai-agent` plus
explicit `terraform apply|destroy` denials in `.claude/settings.json` on the
tooling side. IAM is the real boundary — the settings file is only a guardrail
against wasted effort.

**This mirrors the runtime design:** decision service reads and writes a
verdict; executor holds the deploy permissions. Same shape, different layer.

---

## D-005 — Bedrock: `us.*` inference profiles only, not `global.*`

**Decided:** Target `us.*` cross-region inference profiles. Do not use
`global.*` profiles.

**Why:** Global profiles may route a request to regions outside the US, which
widens the IAM resource surface and makes the data-residency answer "it
depends". `us.*` keeps inference inside three known regions.

**The non-obvious part:** a `us.*` profile invocation is authorised against
*both* the inference-profile ARN in the calling account **and** the underlying
`foundation-model` ARN in whichever region actually serves the request. Granting
only us-east-1 produces intermittent `AccessDeniedException`s that look like
throttling. The policy therefore lists us-east-1, us-east-2 and us-west-2.

See `docs/iam/ai-agent-bedrock-policy.json`.

---

## D-006 — Structured output via forced tool use, not prose parsing

**Decided:** Get the risk verdict by declaring a tool with a JSON input schema
and setting `toolChoice: {"tool": {"name": ...}}` to force the model to call
it. Never ask for JSON in the prompt and parse the reply.

**Why:** "Reply with JSON" is a request; a forced tool call is a structural
constraint applied during decoding. It removes the entire class of failures
where the model wraps JSON in a markdown fence, prefixes it with "Certainly!",
or trails an explanation after the closing brace.

**What it does NOT give you:** Bedrock does not validate tool input against the
declared schema. The model is steered by the schema but can still return
something that violates it. Validation on our side is mandatory and is the
fail-closed boundary. This distinction is a slide.

**Schema shape choices** (see `scripts/check_bedrock_access.py`):

- `risk_level` is an enum — the executor branches on it, so an unknown value
  must be a hard failure rather than a default.
- `confidence` is recorded but never routed on. Self-reported model confidence
  is not calibrated, and treating it as a probability would be false precision.
- `reasoning` is length-capped. Unbounded free text is the vector by which
  prompt-injected content would reach logs and notifications.
- `additionalProperties: false` and all fields required — a partial verdict is
  an ambiguous verdict, and ambiguous means human review.

---

## D-007 — temperature 0 for verdicts

**Decided:** `temperature: 0.0` on all verdict calls.

**Why:** Constraint 6 requires deterministic replay — feed a fixed scenario in,
get a comparable verdict out, so prompt changes can be measured. Sampling noise
would show up in Phase 4 as prompt drift that is not real. Temperature 0 is not
a guarantee of identical output, but it removes the largest source of variance.

**Being honest on stage:** even at temperature 0, Bedrock does not promise
bit-identical responses across time or capacity. Phase 4 measures verdict
stability on repeated identical inputs precisely so we can report the real
number rather than claim determinism we do not have.

---

## D-008 — Account-wide budget rather than tag-filtered

**Decided:** The Phase 0 budget covers the whole account, not just resources
tagged for this project.

**Why:** A tag filter would miss anything created outside Terraform, which is
exactly the spend most likely to surprise you. Cost allocation tags also have
to be activated by hand in the Billing console and take roughly 24 hours to
start populating, so a tag-filtered budget would report zero on day one and
look reassuring while being blind.

**Notification-only for now.** The hard budget action needs its own IAM role
and lands in Phase 8, before the soak test authorises any standing cost.

---

## D-009 — Escalation to SNS email, chat destination deferred

**Decided:** Phase 5 escalation targets SNS with email subscribers. No Slack.

**Why:** No Slack workspace is available. Amazon Q Developer in chat
applications (the service formerly called AWS Chatbot) requires a workspace
and an admin-approved app install.

**Design consequence:** escalation sits behind an interface so a chat
destination is a configuration change, not a rewrite. SNS is the right seam
regardless — Q Developer in chat applications subscribes to an SNS topic, so
adding Slack later means adding a subscriber, not changing the publisher.

---

## Open — model selection for the verdict layer

Not yet decided. `us.anthropic.claude-haiku-4-5-20251001-v1:0` is the default
in the Phase 0 smoke test because it is cheap and the smoke test needs no
judgment quality.

The real choice is a Phase 4 output: run the eval fixture set against several
models and pick on measured over-flagging rate and cost per verdict, not on
reputation. Record the result here when it exists.
