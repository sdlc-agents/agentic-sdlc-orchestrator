# Extending the platform

Four extension points, in rough order of how often they are needed.

## Add a validation check

A check reads the workspace and returns a `CheckResult`. It runs no model.

```python
# asep/validation/checks.py

@_timed
def check_migrations_are_additive(ctx: CheckContext) -> CheckResult:
    findings = []
    for path in ctx.workspace.files("migrations/*.sql"):
        body = path.read_text(encoding="utf-8")
        if "ALTER TABLE" in body and "DROP COLUMN" in body:
            findings.append(
                Finding(
                    check="migrations",
                    severity=Severity.ERROR,
                    message=f"{path.name} drops a column, which is not reversible",
                    target_path=f"migrations/{path.name}",
                    # Without a hint this escalates to a human instead of
                    # being repaired, which for this defect is correct.
                )
            )
    return CheckResult(
        name="migrations",
        status=CheckStatus.FAIL if findings else CheckStatus.PASS,
        findings=findings,
        detail=f"checked {len(ctx.workspace.files('migrations/*.sql'))} migration(s)",
    )


ALL_CHECKS = [..., check_migrations_are_additive]
```

The decision worth thinking about is whether to attach a `repair_hint`. A hint
means the engine will schedule a repair agent to act on the finding, so it must
be specific enough to act on. If a defect needs human judgement, leave the hint
off — escalation is a feature, not a fallback.

Severity matters too: `ERROR` fails the run, `WARNING` is reported and does not.
Traceability gaps are warnings on purpose, because failing a run over them would
make the platform refuse work a person would accept.

## Add an agent

An agent takes a context and returns writes plus artifacts. It decides nothing
about what runs next.

```python
# asep/agents/security.py

class SecurityReviewAgent(ProviderAgent):
    name = "security_review"
    schema = SecurityFindings          # a Pydantic model the provider must fill
    instruction = "Review this design for authentication and authorization gaps..."

    def run(self, ctx: AgentContext) -> AgentResult:
        findings = self.call(
            ctx,
            architecture=ctx.read("architecture"),
            api_contract=ctx.read("api_contract"),
        )
        report = self.artifact(
            path="docs/security-review.md",
            content=render(findings),
            produced_by=ctx.task.id,
            kind=ArtifactKind.DOC,
        )
        self.workspace(ctx).write(report.path, report.content)
        return AgentResult(writes={"security_review": findings}, artifacts=[report])
```

Then register it in `build_registry` and decide its autonomy in
`asep/orchestration/policy.py` — `AUTONOMOUS_AGENTS` runs unattended,
`GATED_AGENTS` requires approval. An agent in neither is treated as high risk and
always gated, which is the safe default for something nobody classified.

Three rules the existing agents follow, worth keeping:

- **Ask for one typed object.** Agents receive a validated Pydantic instance or
  an exception, never raw text.
- **Do not judge your own output.** If the agent needs checking, that is a
  validation check, not a branch inside the agent.
- **Write through `self.workspace(ctx)`.** It is the capability boundary.

Not every agent needs a model. `CodebaseAgent`, `ValidationAgent` and
`SummaryAgent` subclass `Agent` directly and call none — facts about code on disk
should be read, not recalled.

## Add a scenario

A scenario is an opening task graph plus the requirement that starts it.

```python
# asep/scenarios/catalog.py

RATE_LIMITING = Scenario(
    name="rate_limiting",
    title="Brownfield — add rate limiting to link creation",
    requirement="Add per-owner rate limiting to link creation without slowing the redirect path.",
    kind=RequirementKind.BROWNFIELD,
    seed_from=REPO_ROOT / "sample_codebase" / "url_shortener_legacy",
    tasks=[...],   # only the opening nodes
)

SCENARIOS = {s.name: s for s in (GREENFIELD, BROWNFIELD, RATE_LIMITING)}
```

List only the opening tasks. Everything downstream of planning is injected at
runtime — a scenario that enumerates its whole graph up front has given up the
property that makes this an orchestrator.

If the scenario seeds from a directory, the run copies it into the workspace and
the agents work on the copy. The original is never modified.

## Add a provider

Implement one method. Everything else in the system is unaffected.

```python
class AnthropicProvider(Provider):
    name = "anthropic"

    def generate(self, request: GenerationRequest) -> BaseModel:
        try:
            response = self._client.messages.create(...)
        except Exception as exc:
            # The engine bounds retries, so treating SDK failures as transient
            # is safe: a real outage still terminates the run.
            raise TransientError(f"anthropic call failed: {exc}") from exc

        try:
            return request.schema.model_validate_json(text)
        except ValidationError as exc:
            # Retryable: the next attempt gets this message attached.
            raise ContractError(f"{request.agent} returned invalid JSON: {exc}") from exc
```

`GenerationRequest` carries the agent name, task id, scenario, instruction,
target schema, summarized context, attempt number, and the previous error if this
is a retry. Register it in `build_provider` and it is selectable with `--mode`.

The contract that matters: **return a validated object or raise a typed error.**
Agents never see raw text, which is what keeps a malformed model response a
retryable orchestration event rather than a parsing bug inside an agent.

## Regenerating the brownfield sample

`sample_codebase/url_shortener_legacy/` is derived from the blueprint the
greenfield scenario builds, with analytics removed, so the two scenarios cannot
drift into describing different systems.

```bash
python scripts/derive_legacy_sample.py
cd sample_codebase/url_shortener_legacy && pytest -q    # 32 passed
```

Every removal in that script asserts that it matched, so a blueprint change
fails loudly rather than quietly emitting a legacy tree that no longer
corresponds to it. CI checks that the committed output is reproducible.
