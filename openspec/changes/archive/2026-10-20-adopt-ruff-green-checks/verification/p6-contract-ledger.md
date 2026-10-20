# P6 contract ledger

RED source: base `5ca7307ee9be494853b9eaf6ac40e01f871f5a17` plus previously
written workflow provisioning and the two new enforcement test modules.
Python 3.14.7, pytest 9.1.1, xdist 3.8.0. Installed CI-pinned ty 0.0.73 in
`.venv` (`uv pip install --python .venv/bin/python ty==0.0.73`, exit 0) before
running isolated type fixtures. No product source was intentionally broken.

RED command:
`.venv/bin/python -m pytest tests/enforcement/test_green_integration.py tests/enforcement/test_green_skip_policy.py --tb=short -q`
Exit **1**, **30 failed, 12 passed**, no warning summary. Full output captured
temporarily. Missing gate/config/plugin and absent workflow logging/artifact
wiring are actual gaps; source failure fixtures already behave correctly.

| ID | Target/action | Prerequisites | Expected output and exact verification |
| --- | --- | --- | --- |
| P6-L | Local gate contract | accepted P5 | `test_local_plan_preserves_every_required_command`, omission and per-command failure fixtures; isolated lint/format/syntax/type defects return 1, remain unmodified. |
| P6-W | Validation workflow contract | accepted P5 | `test_validation_workflow_retains_full_nonmutating_gate` and workflow mutation fixtures reject missing push/PR, narrowed checks, mutation and swallowed statuses. |
| P6-S | Skip applicability contract | accepted P5 | `test_green_skip_policy.py` rejects unknown test/reason pairs and unexpected xfail; environmental exemptions cannot hide missing CI prerequisites; opt-in exemptions require opt-in absent. No numeric skip-count acceptance. |
| P6-R | Diagnostic retention contract | accepted P5 | `test_log_capture_keeps_complete_output_and_original_status[0/7]` executes workflow's Bash block with isolated child, expects complete stdout/stderr in temporary file and original exit 0/7. Artifact wiring must always upload with seven-day retention. |

- [x] P6-L RED: missing `scripts/green_checks.py` / `pi-green-loop.json`; source fixtures already pass.
- [x] P6-W RED: actual workflow lacks shared gate/artifact wiring.
- [x] P6-S RED: skip plugin absent.
- [x] P6-R RED: workflow capture block absent.
- [x] P6-L GREEN: shared local gate/config implemented; final focused contracts pass (53 total).
- [x] P6-W GREEN: retained workflow invokes identical complete gate; trigger/scope/mutation/status fixtures pass.
- [x] P6-S GREEN: permanent reviewer and documented individual applicability rules implemented; simulated reports and isolated real pytest skip/xfail failures pass.
- [x] P6-R GREEN: workflow capture block executed with child exits 0 and 7; full stdout/stderr retained and original status preserved; always-upload/seven-day artifact wiring verified.

GREEN and final-state results are recorded in `phase-6.md`; RED results
above remain historical and are not green acceptance.

Additional mandatory lifecycle coverage: `green_fixtures.read_bytes` reuses the
existing prepared `docker._owned_file.owned_file` / internal `OwnedStream`
seam. Its operation-plus-close, sole-close, operation/close interruption,
repeat cleanup and independent subsequent-read matrix passes (six cases).
The same owner remains terminal after a failed release; primary operation
failure retains the close error as secondary. No new production acquisition,
handoff or concurrent-reader branch is introduced. The gate delegates process
coordination to existing `python_checks.run_streaming`; interruption and
unsettled cleanup are explicitly rejected rather than turned into success.
The pre-existing ownership implementation was already robust; no artificial
product failure or ownership refactor was introduced for this coverage.
