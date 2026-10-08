# Design constraints

The rules this system was built against, written before the code and referenced
throughout it. They are listed here because a constraint that only exists in
someone's head is a preference, and because several comments in the source point
at them by number.

---

## The six hard constraints

**1. The decision service has no deploy permissions.** It can read signals and
write a verdict. That is all. The executor holds the deploy permissions and acts
only on verdict records that pass validation. A malicious commit message or a
prompt injection must not be able to cause a deploy.

**2. Fail closed, always.** Model unavailable, schema invalid, signals missing,
Bedrock throttled, result ambiguous — route to a human. Encoded as the default
branch, not as an exception handler bolted on later.

**3. Every verdict is auditable and overridable.** Store the full input signals,
the raw model output, the validated verdict, the action taken, and any human
override.

**4. Shadow mode is a first-class mode, not a flag to remove.** Three modes:
`shadow` records what it would have done and acts on nothing; `advisory` notifies
but does not block; `enforcing` blocks. Advisory comes before enforcing in any
rollout.

**5. Signal collectors sit behind a common interface** with a mock
implementation for each, so the whole flow runs end to end with zero real AWS
dependencies. Development is never blocked on scanner setup or on generating
real production traffic.

**6. Deterministic replay.** A fixed scenario in, a comparable verdict out, so
prompt changes can be measured rather than guessed at.

---

## Two kinds of fake signal, and they are not interchangeable

**Mocked signals** are hardcoded responses from a collector's mock. They unblock
development, make tests deterministic, and give the eval harness its fixtures.
They never touch AWS.

**Synthesised-real signals** are conditions deliberately engineered in the
account so that real AWS services genuinely emit real findings, which the real
collectors then parse. They prove the collectors handle actual API response
shapes.

Both are required. Mocked signals alone hide parsing bugs and produce a demo
nobody can honestly defend. Every synthesised condition ships with a documented
recipe **and** a teardown.

---

## Safety rules for the deliberately breakable parts

The demo app carries controllable fault injection, and the plan calls for
pinning dependencies with published CVEs so a real scanner produces real
findings. The rules are absolute:

- Never internet-reachable without authentication.
- Vulnerable **dependencies** only — no deliberately exploitable application
  logic, no real secrets, no real data, ever.
- Isolated from anything else in the account, and tagged for teardown.
- Every CVE used is documented in [DECISIONS.md](../DECISIONS.md) with the
  reason.

See [SECURITY.md](../SECURITY.md) before deploying any of it.

---

## Cost

Built to sit near zero on a personal account: Lambda, DynamoDB on-demand and
Bedrock on-demand only. **Nothing with a standing hourly cost** — no NAT
gateway, no always-on Fargate, no provisioned concurrency. A long soak run under
continuous simulated traffic is the one authorised exception, and it needs a
costed plan and sign-off before anything is created.

---

## Verify, do not assume

Parts of this landscape move faster than any model's training data, and getting
them wrong is expensive:

- **Amazon CodeGuru Security is discontinued** and CodeGuru Reviewer is in
  maintenance mode. This project uses **Amazon Inspector** for dependency and
  code scanning.
- **AWS Chatbot is now Amazon Q Developer in chat applications.**
- Bedrock model IDs, regional availability, and the Converse API's tool-use
  surface all shift. Confirm current IDs and that model access is enabled in
  your region before writing invocation code.
- **Amazon Bedrock AgentCore** is the managed agentic platform. This project
  deliberately does not use it — plain Bedrock and Lambda first, designed so
  that swap stays possible later.

[FAILURES.md](../FAILURES.md) F-004, F-009 and F-014 are what happens when these
are assumed rather than checked.
