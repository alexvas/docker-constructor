## 1. Provider Compatibility

- [x] 1.1 Extend npm provider source admission to explicitly accept both `NpmSource` and `PiReleaseSource`, reuse the existing package-based discovery path, and verify unrelated source types still return the source-type skipped diagnostic.

## 2. Regression Coverage

- [x] 2.1 Add npm provider tests for a `PiReleaseSource` candidate, including encoded scoped-package lookup and existing stable/publication metadata behavior, and verify the focused provider test suite passes.
- [x] 2.2 Add update-discovery coverage proving the canonical Pi target becomes applicable `outdated` rather than `skipped` and its generated replacement fragment retains `type = "pi-release"`, repository, and tag-prefix fields; verify the focused update and suggestion tests pass.

## 3. Verification

- [x] 3.1 Run OpenSpec strict validation and the project checks relevant to version providers and update discovery, recording evidence that the change introduces no inventory-schema, reporting, or Pi materialization regressions.
