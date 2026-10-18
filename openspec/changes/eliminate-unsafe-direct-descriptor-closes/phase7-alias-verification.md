# Phase 7 partial alias verification and resolved scope decisions

This is partial implementation evidence, not Phase 7 acceptance. Task 7.2 was cancelled, not completed; tasks 7.3–7.10 remain pending. No production code changed in the task 7.1 follow-ups below.

## Evidence retention and reproducibility

The durable record is the commands, source-state identifiers, exit statuses, result summaries and limitations in this document. Full stdout/stderr and runner statuses are captured during verification in temporary local files or retention-limited CI artifacts. Such artifacts are diagnostic aids, not required permanent repository evidence. Container-local temporary paths are deliberately not published as durable links.

Historical runs below were recorded against successive uncommitted implementation states. Their exact source digests were not recorded; they cannot be attributed to the current tree or independently reconstructed from their summaries. They are historical observations, not current acceptance. No historical full log has been reconstructed or promised available. Future source changes require fresh verification.

### Verification commands

- **Full pytest:** `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
- **Descriptor gate:** the same command with `tests/enforcement/test_descriptor_close_convergence.py` in place of `tests`.
- **Enforcement:** the same command with `tests/enforcement` in place of `tests`.
- **Typecheck:** `ty check docker --python-version 3.14 --output-format concise`.
- **Whitespace:** `git diff --check`.
- **Validation:** `openspec validate eliminate-unsafe-direct-descriptor-closes --strict`.
- **Green-loop:** `PATH="$PWD/.venv/bin:$PATH" PYTEST_ADDOPTS='-n 8 --dist loadfile --tb=short -q' npx pi-green-loop check --feedback`.
- **Historical unittest:** `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`.

Historical focused RED commands were not retained in this document at their original granularity; the identified regression tests remain executable. An unsuccessful RED run is not a successful verification.

## Task 7.1 implementation history

The gate gained transitive local `os.fdopen`/`tempfile.mkstemp` origin tracking, fail-closed conflicting primitive origins, and integrated fixtures for bare success/finally close, unsafe handoff, multi-close, aggregate/reverse imports, new-module subprocess acquisition, prepared transfer, reader barriers, shared budgets, retained continuation, managed-action settlement and captured-helper ownership. `from docker import filesystem` was added to the complete-tree process scanner's aggregate-import rejection.

Ordered evaluation replaced the old line-number test for owner liveness. Assignments and captured close/detach callables retain owner identities; call arguments precede fdopen acceptance; branches join possible identities and invalidations. Nested definition bodies are not executed merely by defining them. Context exit invalidates tracked owners.

Loop backedges now join body and zero-iteration states to a bounded fixed point. Every abstract visit to fdopen must remain safe. Stable allocation-site identities conservatively reject reuse of an invalidated site, even if runtime iterations could allocate fresh owners. No loop-bound, break/continue precision or termination proof is claimed.

Exceptional-entry snapshots retain aliases created before a possible failure, including aliases overwritten later on the successful path. Handlers join possible try-body prefixes, not just pre-try bindings. Else is excluded from the same try's handlers; finally includes possible body/else/handler failures. Sequential except* handlers receive possible preceding-handler states. Exception target names are unknown during handlers and deleted at exit.

### Historical RED results

| Checked implementation state / regression | Exit | Observed unmet assertions |
| --- | --- | --- |
| Before primitive-alias propagation | 1 | 4: three fdopen subtests and one mkstemp test |
| Before aggregate-import correction | 1 | One aggregate-import subtest |
| Before `.fd`/preceding terminal-operation checks | 1 | Three non-fd/closed/detached-owner fixtures |
| Before ordered owner identities | 1 | 12 subtests covering order, aliases, captured releases and rebinding |
| Before loop fixed point | 1 | 16 subtests covering loop-carried invalidation |
| Before exceptional-entry propagation | 1 | 24 subtests through both `_fdopen_violations` and `_file_object_names` |

### Historical GREEN results

Full pytest unless otherwise identified; all listed runs exited 0. Warnings were not suppressed.

| Checked implementation state | Passed | Skipped | Warnings | Subtests passed | Duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| Initial baseline | 7,880 | 13 | 11 | 17,816 | 51.63s |
| Primitive-alias propagation | 7,882 | 13 | 11 | 17,819 | 51.59s |
| Integrated aggregate-import correction (enforcement only) | 286 | Not recorded | Not recorded | 480 | 27.64s |
| Ordered owner identities | 7,891 | 13 | 11 | 17,898 | 52.63s |
| Loop fixed point | 7,893 | 13 | 11 | 17,934 | 52.48s |
| Exceptional-entry propagation | 7,895 | 13 | 11 | 18,012 | 52.56s |

Baseline/origin-propagation typecheck reported `All checks passed!`; whitespace checks exited 0. Ordered, loop and exceptional-entry follow-ups each recorded exit 0 for green-loop, typecheck and whitespace checks. These runs establish only their then-current regression matrix, not complete-tree ownership safety.

## Bounded proof and user-approved deferral

Task 7.1 is a bounded regression matrix, not a sound general-purpose owner-liveness verifier. Invoked closures closing captured owners and container aliases such as `holders = (owner,); holders[0].close()` remain known counterexamples: both fdopen checks can accept subsequent use of the invalidated owner. These are analyzer limitations, not approved lifecycle patterns or evidence of actual production occurrences.

Further indirect-alias, callback, collection and control-flow analysis is deferred by explicit user decision to [introduce-ownership-language](../introduce-ownership-language/tasks.md), especially tasks 1.3 and 4.1–4.5. Package extraction alone does not resolve it. This deferral does not authorize unsafe production lifecycles or make the new library a prerequisite here. Tasks 7.3–7.10 still require actual production inventory, focused lifecycle evidence and explicit disposition of unsupported sites in `verification.md`.

## Resolved scope: library-internal synchronous subprocess pipes

The preliminary inventory identified `subprocess.run(capture_output=True)` at these then-current source lines:

| Production source | Lines |
| --- | --- |
| `docker/launcher.py` | 343 |
| `docker/networking.py` | 303 |
| `docker/npm_environment/execution.py` | 277, 420, 717 |
| `docker/runtime_installer.py` | 603 |
| `docker/versioning/build_orchestration.py` | 150, 655 |
| `docker/versioning/evidence.py` | 233, 537 |
| `docker/versioning/transports.py` | 119 |

These callers receive completed values, not exposed fd-owning pipes. By explicit user decision, fully encapsulated `subprocess.run` lifecycles are library-owned and must not be migrated to `OwnedProcess` here. See `docs/process-lifecycle.md`, “Fully encapsulated synchronous subprocess calls”. This does not exempt application-supplied live streams, grant another direct-close category, or prove application-managed ownership. Delegated HTTP/probe and native local-file families remain separately owned by their designated changes. This scope decision alone completes no task.

## Cancelled task 7.2: historical exploratory work only

The universal cross-family dynamic journal was explicitly cancelled, not implemented or transferred to the new library change. Experimental `tests/enforcement/descriptor_ledger.py` and `test_repository_fd_ledger.py` were subsequently deleted at the user's request. Their abstract oracle is not concrete cross-family application instrumentation or current coverage. Existing focused lifecycle/ledger checks remain required.

Historical exploratory verification, not current acceptance:

- The first bridge-test run exited 1 with six failures because the test invoked a nonexistent accumulator method. This was a test implementation error, not RED production evidence; it was corrected to use `complete()`.
- Full pytest with the experimental additions: exit 0; 7,908 passed, 13 skipped, 11 warnings, 17,837 subtests passed in 52.53s.
- Explicit unittest discovery before the pytest-only trace additions: exit 0; 6,457 tests, 13 skipped, OK in 175.542s. An earlier attempt timed out after 120 seconds with no runner result and was not accepted as passing.
- Typecheck: exit 0, `All checks passed!`; whitespace: exit 0; strict validation: exit 0, `Change 'eliminate-unsafe-direct-descriptor-closes' is valid`.
- Green-loop initially failed because `pytest` was not on PATH (underlying exit 127). With the project venv on PATH and eight-worker arguments it exited 0. That wrapper result predates the integrated/trace additions.

## Follow-up: proven non-descriptor close and durable evidence

Task 7.1 was reopened because its required positive non-descriptor close case was missing. The actual `_close_violations_with_owners` gate is tested with direct empty `io.StringIO()` and `io.BytesIO()` receivers through imported aliases. The constructor origin, not receiver spelling, distinguishes these descriptor-less buffers from fd-owning streams. The deliberately narrow acceptance does not cover arbitrary receiver variables or annotations, subclasses, unknown constructors, or shadowed/mutated import roots. For these memory origins, the module alias resolver also rejects conflicting/relative imports and direct rebinding/mutation anywhere in the source tree, conservatively including unrelated lexical scopes. Negative fixtures retain rejection of unknown/conflicting/relative origins, parameter/class/pattern shadowing, direct/conditional factory rebinding, module-attribute mutation and reassigned receiver variables. This is not a general proof against dynamic monkeypatching or indirect alias mutation.

RED: the descriptor-gate command above exited 1 against the gate before this acceptance path: 2 failed, 90 passed, 629 subtests passed in 9.21s. Both failures were positive-fixture assertions (`memory.StringIO().close` and aliased `Buffer().close` were incorrectly reported as violations). Full output and exit status were captured temporarily; permanent evidence is this summary, not a container-local log link.

### Current GREEN verification

Checked state: base commit `d057d50ab7532a55dd393326c22c84c0a48023ac` plus working-tree changes, including preexisting staged changes (not the index alone). Environment: project `.venv`, Python 3.14.7, Linux 7.0.0-38-generic x86_64, eight pytest workers with loadfile distribution. Tests were neither excluded nor warnings suppressed.

Source fingerprints (SHA-256, excluding subsequent evidence/checkbox-only edits):

- `git diff HEAD -- docker tests | sha256sum`: `a02892e94bd780898ef98ebc1b721bc64ce127e3a105841b258bfa195eae008c`.
- `tests/enforcement/test_descriptor_close_convergence.py`: `6741dca5c96a7d1488a0e54aff697334a1fe49d25b2791ce8557b67a5df39cf3`.

Commands are listed above; every final check exited 0:

| Check | Result |
| --- | --- |
| Descriptor gate, final source state | 90 passed, 637 subtests passed in 8.91s |
| Full pytest | 7,897 passed, 13 skipped, 11 warnings, 18,026 subtests passed in 52.56s |
| Green-loop | `pi-green-loop: all checks passing` |
| Typecheck | `All checks passed!` |
| Whitespace | Clean, no output |
| Strict OpenSpec validation | `Change 'eliminate-unsafe-direct-descriptor-closes' is valid` |

All 11 full-suite warnings were multiprocessing fork-in-multithreaded-process deprecations in build-generation integration, transaction locking and constructor build-transaction tests. Full outputs and individual statuses were captured during execution; these summaries do not depend on retaining the temporary files. Unittest discovery was not rerun for this bounded follow-up; the historical run is not substituted for pending task 7.8.

Task 7.1 was restored to complete after the positive/negative regressions and all checks passed. No final `verification.md` complete-tree classification or Phase 7 release acceptance is claimed; tasks 7.3–7.10 remain pending.
