# Implementation Contract

This checklist is the binding implementation contract for this change. A phase is complete only when every task in that phase is checked and every listed deliverable exists. Work within each phase SHALL proceed in `RED → GREEN → INTROSPECT → VALIDATE` order. A phase MAY begin only after every phase named in its `Depends on` line is complete. Dependencies SHALL point only to earlier phases.

## 1. Build-Time Client CA Environment

**Depends on:** none.

**Deliverables:** one closed constructor-owned mapping from `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`, and `CURL_CA_BUNDLE` to `/etc/ssl/certs/ca-certificates.crt`; conditional export of that mapping before every networked Dockerfile build command; unchanged inherited environment when corporate trust is disabled; no persistent same-named image `ENV` or build `ARG`.

- [x] 1.1 **RED:** Add a focused helper test requiring all five client CA variables to resolve to `/etc/ssl/certs/ca-certificates.crt` when corporate trust is enabled; run it and verify it fails because three exports are missing.
- [x] 1.2 **RED:** Add a focused disabled-helper test proving absent or false corporate trust exports none of the five variables and does not clear conflicting inherited values; run it and verify the disabled contract is observable independently of the enabled case.
- [x] 1.3 **RED:** Extend the Dockerfile contract test to reject persistent same-named `ENV` and `ARG` instructions for all five variables; run it and record the current result before implementation.
- [x] 1.4 **GREEN:** Define the closed five-variable system-bundle mapping at the build-helper ownership boundary without accepting host or local-configuration values; verify the mapping test from 1.1 passes.
- [x] 1.5 **GREEN:** Extend `docker/corp-network-env.sh` so the enabled branch exports the complete mapping and the disabled branch remains a no-op; verify the helper tests from 1.1–1.2 pass.
- [x] 1.6 **INTROSPECT:** Enumerate every Dockerfile `RUN` that performs network access and verify each sources the corporate-network helper before its first network operation; add or update one structural regression assertion for the complete enumeration.
- [x] 1.7 **INTROSPECT:** Audit Dockerfile metadata and build arguments for constructor-defined or empty overrides of the five client variables; remove any override and verify only `PI_CORPORATE_CA_PATH` transports the enabled build path.
- [x] 1.8 **VALIDATE:** Run the focused corporate-network build and Dockerfile contract suites; verify enabled, disabled, inherited-value, complete-network-coverage, and non-persistence cases pass and record exact commands and results in change verification evidence.

## 2. Runtime Launch Propagation

**Depends on:** Phase 1.

**Deliverables:** exact direct Docker environment arguments for the same five-variable mapping on enabled in-scope constructor launches, including standalone npm assemblers; internal build-verification containers (`docker/versioning/verification.py`) and gateway-probe containers (`docker/networking.py`) receive neither constructor-injected corporate bundle mounts nor client CA assignments and preserve inherited image trust settings regardless of the trust setting; no constructor-generated client CA arguments on absent or disabled launches; continued read-only mount of the current corporate bundle at the fixed system path; no changes to proxy or host-access launch policy.

- [x] 2.1 **RED:** Add a focused run-vector test requiring exactly one environment assignment for each of the five variables when corporate trust is enabled; run it and verify it fails because runtime CA environment propagation is absent.
- [x] 2.2 **RED:** Add a focused enabled-run test requiring the existing corporate bundle mount to remain read-only at `/etc/ssl/certs/ca-certificates.crt` alongside the five environment assignments; run it and verify only the new environment assertions fail.
- [x] 2.3 **RED:** Add absent and explicitly disabled run-vector tests proving none of the five assignments is introduced or emitted with an empty value; run them and record the pre-implementation result.
- [x] 2.4 **RED:** Add independence tests proving proxy-only and host-access-only launch policies do not activate the client CA mapping; run them and record the pre-implementation result.
- [x] 2.5 **RED:** Add focused tests for every constructor-launched standalone npm assembler run vector requiring all five exact client CA environment assignments when corporate trust is enabled while preserving the vector's existing `npm_config_cafile` behavior; run them and verify they fail only on the missing five-variable propagation.
- [x] 2.6 **RED:** Add absent and explicitly disabled corporate-trust tests for every standalone npm assembler run vector proving none of the five client CA assignments is introduced and the existing `npm_config_cafile` behavior remains unchanged; run them and record the pre-implementation result.
- [x] 2.7 **GREEN:** Extend direct Docker run rendering to emit the closed five-variable mapping only when resolved corporate trust is enabled; verify the enabled test from 2.1 passes.
- [x] 2.8 **GREEN:** Preserve the existing read-only system-bundle mount while adding the environment assignments; verify the combined mount/environment test from 2.2 passes.
- [x] 2.9 **GREEN:** Preserve complete omission on absent and disabled trust paths; verify the tests from 2.3 pass without clearing inherited image values.
- [x] 2.10 **GREEN:** Preserve proxy and host-access independence; verify the tests from 2.4 pass without changing their launch vectors beyond their own configured policy.
- [x] 2.11 **GREEN:** Extend every constructor-launched standalone npm assembler run vector to emit the closed five-variable mapping only when resolved corporate trust is enabled, without replacing, removing, or otherwise changing its existing `npm_config_cafile` behavior; verify the tests from 2.5–2.6 pass.
- [x] 2.12 **INTROSPECT:** Trace the resolved corporate-trust boolean and fixed bundle destination from local configuration through launch planning to final Docker argv; verify no arbitrary variable name, value, host bundle path, or invoking-host environment enters the mapping.
- [x] 2.13 **INTROSPECT:** Enumerate every constructor-launched standalone npm assembler container and add a regression assertion for each launch vector proving it receives the same enabled five-variable policy as the primary run-rendering path; these vectors SHALL NOT be excluded or documented as outside the constructor-launch contract.
- [x] 2.14 **INTROSPECT:** Audit all other constructor-owned runtime launch entry points for bypasses of the shared run rendering path; add a regression assertion for each constructor-launched alternate path. Explicitly exclude only internal build-verification containers (`docker/versioning/verification.py`) and gateway-probe containers (`docker/networking.py`), verifying that both introduce neither corporate bundle mounts nor any of the five client CA assignments and preserve inherited image trust settings regardless of the trust setting. All other constructor-launched paths, including standalone npm assemblers, remain in scope; missing existing mounts are not grounds for exclusion.
- [x] 2.15 **VALIDATE:** Run focused run-rendering, standalone npm assembler, corporate-network launch, proxy, and host-access suites; verify exact enabled propagation, disabled omission, preserved `npm_config_cafile` behavior, read-only mount preservation, and policy independence, then record exact commands and results.

## 3. Runtime Verification and Orchestration

**Depends on:** Phase 2.

**Deliverables:** orchestration carries one resolved trust decision into mount and environment planning; runtime verification of in-scope containers (distinct from excluded internal build-verification containers) checks exact enabled values and disabled non-injection without external network access; malformed corporate configuration still fails before Docker effects.

- [x] 3.1 **RED:** Add an orchestration test proving enabled local trust produces both the read-only mount and all five exact environment assignments in the final launch request; run it and verify it fails at the missing environment boundary.
- [x] 3.2 **RED:** Add a runtime-verification test that reports each missing or mismatched enabled client CA variable while retaining the existing mount result; run it and verify the new checks are absent.
- [x] 3.3 **RED:** Add a disabled runtime-verification test proving constructor-generated client CA assignments are not required and are not reported as enabled policy; run it and record the pre-implementation result.
- [x] 3.4 **RED:** Add a malformed-enabled-configuration regression test proving validation fails before launch planning, container inspection, or Docker execution; run it and record the pre-implementation result.
- [x] 3.5 **GREEN:** Wire the resolved corporate-trust decision through orchestration to the existing run-rendering inputs without introducing a second trust source; verify the orchestration test from 3.1 passes.
- [x] 3.6 **GREEN:** Extend runtime verification to inspect all five variables and require their exact fixed value when trust is enabled; verify the enabled verification test from 3.2 passes.
- [x] 3.7 **GREEN:** Keep disabled runtime verification free of constructor-defined client CA expectations; verify the test from 3.3 passes.
- [x] 3.8 **GREEN:** Preserve pre-effect rejection of malformed enabled trust; verify the regression from 3.4 passes.
- [x] 3.9 **INTROSPECT:** Review verification result keys, details, and structured output for stable diagnostics that reveal only fixed in-container variable names and paths, never the host bundle source or certificate contents; add a focused disclosure assertion.
- [x] 3.10 **INTROSPECT:** Confirm runtime verification performs no TLS request and makes no claim about certificate validity, connectivity, or strict Node root replacement; add a regression assertion or documentation check for each boundary.
- [x] 3.11 **VALIDATE:** Run focused orchestration, malformed-configuration, runtime-verification, and structured-output suites; verify all enabled, disabled, mismatch, pre-effect, and disclosure cases pass and record exact commands and results.

## 4. Boundaries and Documentation

**Depends on:** Phases 1, 2, and 3.

**Deliverables:** explicit user documentation for the fixed five-variable build/runtime policy, disabled preservation, image-metadata exclusion, direct-launch boundary, the two explicit internal diagnostic launch exceptions, and Node augmentation semantics; automated boundary coverage for configuration, metadata, disclosure, and unrelated policies.

- [x] 4.1 **RED:** Add documentation assertions requiring all five variable names, the fixed system path, enabled-only build/runtime scope, and disabled preservation; run them and verify the missing statements are reported.
- [x] 4.2 **RED:** Add documentation assertions requiring image `ENV` exclusion, the direct-image-launch limitation, the internal build-verification and gateway-probe exceptions without excluding runtime CA-policy verification or standalone npm assemblers, and the distinction between system-bundle replacement and Node root augmentation; run them and verify the missing statements are reported.
- [x] 4.3 **RED:** Add boundary tests proving local and reviewed configuration cannot supply arbitrary client CA names, values, or paths; run them and record the current closed-schema result.
- [x] 4.4 **RED:** Add disclosure tests proving host bundle paths and certificate contents do not enter rendered summaries, verification details, evidence, or image metadata; run them and record the current result.
- [x] 4.5 **GREEN:** Update corporate-network user documentation with the exact five-variable mapping and its enabled build/runtime behavior; verify the documentation assertions from 4.1 pass.
- [x] 4.6 **GREEN:** Document disabled preservation, non-persistent image metadata, direct-launch exclusion, the two internal diagnostic launch exceptions and their non-injection/inherited-preservation behavior, enterprise-interception goal, and `NODE_EXTRA_CA_CERTS` augmentation semantics; verify the assertions from 4.2 pass.
- [x] 4.7 **GREEN:** Preserve closed configuration and disclosure boundaries while integrating the policy; verify the tests from 4.3–4.4 pass without adding generalized environment passthrough.
- [x] 4.8 **INTROSPECT:** Search source, tests, examples, and documentation for stale claims that only `SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS` form the enabled client policy; update every current claim while preserving historical archived artifacts.
- [x] 4.9 **INTROSPECT:** Trace each variable through build and runtime paths and verify it appears in no cache identity, dependency identity, evidence payload, reviewed/effective configuration, or persistent image metadata path; add a regression assertion for every discovered semantic boundary.
- [x] 4.10 **VALIDATE:** Run documentation, schema, projection, evidence, identity, and disclosure suites; verify all five-variable, disabled, direct-launch, Node-semantics, and non-persistence assertions pass and record exact commands and results.

## 5. Release Integration

**Depends on:** Phase 4.

**Deliverables:** passing cross-path acceptance matrix for enabled and disabled trust across build, launch, verification, and unrelated network policies; no unresolved contract gaps; complete external verification evidence; strict OpenSpec validation.

- [ ] 5.1 **RED:** Add a release acceptance matrix covering enabled and disabled trust across build-helper export, networked Dockerfile coverage, runtime argv, read-only mount, runtime verification, proxy independence, host-access independence, and image-metadata exclusion; run it and record every unsatisfied case before release fixes.
- [ ] 5.2 **RED:** Run the complete project typecheck, lint/static checks, unit/integration tests, and canonical build once; record every pre-release failure before making release fixes.
- [ ] 5.3 **GREEN:** Fix only integration defects exposed by 5.1 while preserving all completed phase contracts; verify the complete acceptance matrix passes.
- [ ] 5.4 **GREEN:** Fix regressions exposed by 5.2 without weakening tests or specifications; verify typecheck, lint/static checks, the complete test suite, and canonical build all pass.
- [ ] 5.5 **INTROSPECT:** Trace every requirement and scenario in the delta spec to at least one completed task and automated test or explicit observable verification; document and close every uncovered requirement in change verification evidence.
- [ ] 5.6 **INTROSPECT:** Review the final diff for disabled-mode overrides, persistent image metadata, arbitrary environment passthrough, host-path or certificate disclosure, proxy/host-access coupling, identity changes, external TLS assumptions, and late-to-early phase dependency violations; resolve every finding.
- [ ] 5.7 **VALIDATE:** Re-run all focused and full-project checks from clean state and record exact commands, tool versions, results, and any genuine environment blockers outside specification artifacts.
- [ ] 5.8 **VALIDATE:** Run a Docker-backed enabled runtime acceptance check that inspects all five exact environment values and the read-only mount without depending on external TLS connectivity; verify it passes and record the command and result.
- [ ] 5.9 **VALIDATE:** Run `openspec validate apply-corporate-trust-client-environment --strict` and verify it passes before marking implementation complete.
