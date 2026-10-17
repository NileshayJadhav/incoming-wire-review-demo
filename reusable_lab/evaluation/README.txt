LOCAL WIRE EVALUATION
=====================

What is implemented
-------------------
evaluation.py can execute an offline fictional regression, run an explicitly
injected approved extraction model against the same frozen page text, or score
an existing Review_Report.json against independent expected labels. It makes
no audit sign-off or deployment-readiness decision.

23 frozen fictional cases cover 92 control decisions, structured source fields,
and the Fee/Classification workbook values. The 15 development and 8 held_out
cases are labeled explicitly in synthetic_expected.json. Cases include Travel
2999.99 / 3000.00, missing required address, missing/zero/waived fee, missing
statement, OFAC Passed/False Positive with missing approval/reason, conflicting
and duplicate principal, fee credit, unread statement page, correction, return,
foreign fee without a supplied rule, account mismatch, out-of-period dates,
embedded instructions, and a fabricated source quote.

These are developer-authored expectations independent of engine output. They
are NOT human-validated gold. No API or model generated the fixture responses
during evaluation. Offline field scores use frozen mocked extraction output;
100% offline results are regression evidence, not measured model accuracy or
proof of prompt-injection resistance. The synthetic source is page TEXT, so
OCR, folder pairing, PDF rendering and final Excel appearance are excluded.

Run offline (no key or model needed)
-----------------------------------
From the project root in PowerShell:

python -m reusable_lab.evaluation --offline --output reusable_lab/evaluation/results/offline_regression.json --write-report reusable_lab/evaluation/results/offline_review_report.json

Add --split held_out or --split development to run one subset.

Run evaluator-integrity tests:

python -m pytest reusable_lab/test_evaluation.py -q

Results and gates
-----------------
- Per-control confusion matrix: expected status rows, actual status columns.
- Coverage: reported/expected/graded/missing/unexpected samples and per-control
  actual SUPPORTED/NOT_APPLICABLE/OPEN counts.
- False-supported rate: actual SUPPORTED where expected is OPEN or
  NOT_APPLICABLE, divided by expected non-SUPPORTED decisions for that control.
- Supported precision/recall; absent denominators are null, never zero accuracy.
- Validated extraction field exact match (Decimal equivalence only for money;
  leading account zeros and boolean types are preserved), including expected
  null for confirmed absent fields. Fee/Classification output values are also
  graded so a correct extraction cannot hide an incorrect workbook value.
- Independent exact quote/source/physical-page/role membership and evidence
  reference checks, plus separately identified production value/party validator
  replay. Evidence reused by a result must match its extracted fact exactly.
- False SUPPORTED, false NOT_APPLICABLE, unsupported citations, inconsistent
  X/-/OPEN symbols, wrong workbook values, missing/duplicate/unexpected sample
  IDs, changed sources or failed runs are critical failures. Conservative OPEN
  instead of expected support is still a scored mismatch.
- Stage timings and actual extraction call counts are retained. A cached model
  result is labeled CACHED_MODEL_TEXT_EXTRACTION, not a fresh model run.
- PASS requires no control/field mismatches and no critical failures. It does
  not automatically authorize production use.

Score an existing real report
----------------------------
Without independent real labels this performs operational source/reference
checks only; status is NOT_GRADED_NO_EXPECTED_LABELS and all accuracy metrics
are null. The supplied report's stored page text is used; this command does NOT
reread original PDFs or independently recompute their hashes.

python -m reusable_lab.evaluation --report 'FULL_PATH_TO_Review_Report.json' --output 'FULL_PATH_TO_operational_audit.json' --label-template 'FULL_PATH_TO_reviewer_labels_UNFILLED.json'

The optional label template deliberately contains NO copied conclusions and
cannot be scored as-is. An independent reviewer must inspect the physical
source PDFs and applicable criterion version, fill every expected check using
SUPPORTED/NOT_APPLICABLE/OPEN, add expected source field values where useful,
and document rationale. Null means confirmed absence; it must not be used as
an unreviewed placeholder. Then the reviewer may set human_validated=true,
reviewer and reviewed_on (ISO date). Keep the source_identity hashes intact.

python -m reusable_lab.evaluation --report 'FULL_PATH_TO_Review_Report.json' --gold 'FULL_PATH_TO_reviewer_validated_labels.json' --output 'FULL_PATH_TO_labeled_evaluation.json'

Real labels without reviewer/date attestation or matching workbook/rule/PDF
hashes are rejected. Attestation is reviewer-supplied, not independently proven
by this tool. Never label historical worksheets or the model's own output as
independent gold. A new source batch needs its own source-bound labels.

Opt-in live model evaluation API
-------------------------------
The harness never discovers a key, changes a saved key, or creates a model
provider. A caller that already owns an authorized model object may run:

from reusable_lab.evaluation import evaluate_model, write_results
result, report = evaluate_model(
    approved_model,
    model_name='gpt-4o-mini',
    cache_dir='reusable_lab/evaluation/model_cache',
    split='held_out',
)
write_results('reusable_lab/evaluation/results/live_model_evaluation.json', result)
write_results('reusable_lab/evaluation/results/live_model_review_report.json', report)

This uses the real extraction prompt, schema, quote/value validator and criteria
against synthetic TEXT. The default held-out subset has 8 cases, with at most
one repair per case under the existing extractor limit. force_refresh=False
uses valid cached extractions and reports actual calls. Do not describe the
example above as an executed model test unless a saved result proves it ran.

Revalidate existing model responses without another model call
------------------------------------------------------------
python -m reusable_lab.evaluation --cache-replay 'FULL_PATH_TO_EXISTING_MODEL_CACHE' --model-name gpt-4o-mini --split held_out --output 'FULL_PATH_TO_cached_guard_Evaluation.json' --write-report 'FULL_PATH_TO_cached_guard_Synthetic_Review_Report.json'

replay_model_cache(...) checks every selected cache signature against the
unchanged model/prompt/schema/extraction version and exact frozen input. A
missing or mismatched cache fails before revalidation; it never falls back to
a provider call or spends a repair attempt. Original cache bytes are checked
before/after and are never rewritten. Output files must be outside the cache.

The first live model run exposed false supported checks from memo instructions,
an approver name mistaken for an OFAC reason, and an omitted duplicate posting.
Source-context guards and explicit labeled-record recovery were developed after
those observations. These same eight cases are now regression cases after
observed failures, even though their original split name remains held_out.
Cache replay is labeled CACHED_MODEL_REGRESSION_AFTER_OBSERVED_FAILURES; it is
not a fresh model run or an untouched holdout estimate. Preserve the initial
failed artifact and frozen expected labels when reporting improvements.

Fixture changes
---------------
synthetic_cases.json, synthetic_expected.json and synthetic_rules.json are
frozen inputs; their hashes are checked. build_dataset.py is a deliberate
developer fixture authoring tool, not invoked by normal evaluation. Do not
change expected labels to make a failing production function pass. Review an
incorrect label against source/criteria first, document the reason and create
a new dataset version for material changes. Outputs cannot overwrite inputs.

Current boundary
----------------
Real model extraction accuracy, prompt-injection behavior, representative
scanned PDF OCR performance and real-sample accuracy require their own recorded
runs and independently validated labels. Functional regression alone does not
complete those gates. Human review remains mandatory for draft workbooks.
