# Reviewer output

Code and security reviewers return one compact JSON object. This is the same
contract for desktop execution, Anthropic Messages, and OpenAI Responses.

```json
{
  "schema_version": 1,
  "gate": "code-review",
  "verdict": "PASS",
  "checks": [{"name": "acceptance coverage", "status": "pass"}],
  "findings": []
}
```

`checks` are deliberately limited to names and statuses: `pass`, `fail`,
`not_run`, or `not_applicable`. Reviewers do not explain successful checks.
Explanations are generated only for actual findings, where they are needed to
describe the defect, impact, evidence, and required correction.

Each finding contains a stable `path:symbol` component, `blocking` or `advisory`
disposition, severity, short title, actionable explanation, and a regression
boolean. PASS cannot contain a blocking finding; FAIL must contain one. A failed
or unrun check also requires a blocking finding so its explanation is never
hidden in generic prose.

The API payload builders install this as provider-native structured output. For
desktop runs, save the JSON and validate/record it mechanically:

```bash
scripts/context_pipeline.py validate-review --gate code-review \
  --input .orchestration/.review-results/code-review.json
scripts/review-ledger.py record <pr> --gate code-review \
  --result .orchestration/.review-results/code-review.json
```

The schema itself is available through `scripts/context_pipeline.py
review-schema --gate code-review` (or `security-review`). Result files are
runtime state and should remain gitignored.
