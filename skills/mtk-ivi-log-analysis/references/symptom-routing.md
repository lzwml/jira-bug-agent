# Symptom route contract

Each symptom Skill should answer the same six questions while retaining domain-specific freedom:

1. **Activation:** What user-visible behavior or diagnostic identity selects this route, and what similar symptom does not?
2. **Required identity:** Which process, boot round, VM/domain, operation ID, signal ID, or timestamp prevents evidence from different incidents being mixed?
3. **First evidence pass:** What is the smallest useful sequence of `inspect_case`, `parse_diagnostics`, `extract_timeline`, and focused `search_evidence` calls?
4. **Decision branches:** Which observed state changes the next check? Branches should test layer boundaries rather than enumerate keywords.
5. **Falsification and stopping:** What evidence contradicts each hypothesis, and which missing artifact requires `insufficient_evidence`?
6. **Output:** What last-successful and first-failed transitions, supporting evidence, contradictions, and next observation must be reported?

## Composition

Use at most one primary symptom Skill for an investigation pass. Compose it with:

- `android-log-triage` only while the failure family is unknown or the Case needs an initial inventory;
- one platform Skill, such as `mtk-ivi-log-analysis`, when platform topology or clock rules change the investigation;
- another symptom Skill only after evidence demonstrates a separate failure rather than a downstream consequence.

The current Worker loads Skills supplied in `BugAnalysisTask.skills`; it does not dynamically activate a Skill named inside another Skill. CLI and workflow callers must therefore select the route explicitly. Future automatic routing should produce a route decision containing the selected Skill, confidence, supporting observations, and unresolved alternatives before starting the specialist pass.

## Extension rule

Add platform-specific tags, filenames, or state names only when they change a decision or locate a required artifact. Deterministic recognition belongs in `log-analysis-core`; filesystem access and extraction safety belong in MCP. Refine a route from real cases and regression tests instead of accumulating universal keyword catalogs.
