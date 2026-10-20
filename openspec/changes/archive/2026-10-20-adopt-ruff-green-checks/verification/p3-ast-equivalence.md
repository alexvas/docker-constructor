# P3 AST equivalence report

- **Change:** `adopt-ruff-green-checks`
- **Base:** `HEAD` `d938527dc2e9b5903b5179df3840e34ec5541b6a` plus the
  formatter-only P3 working-tree delta.
- **Before-state:** [`p3-source-before.sha256`](p3-source-before.sha256), one
  SHA-256 entry for each of the 396 selected inventory paths, captured before
  formatting.
- **After-state:** [`p3-source-after.sha256`](p3-source-after.sha256), the same
  396 paths after the formatter pass and after validation confirmed no further
  source changes.
- **Tool:** CPython 3.14.7, Ruff 0.17.1.

## Result

**Exit 0 — 371 changed files equivalent, 25 files byte-identical, 0 AST
differences.** The P2 and formatted fingerprints are recorded in
[`phase-3.md`](phase-3.md).

## Comparison method

Each changed file was parsed from its raw bytes with
`ast.parse(data, filename=relative, type_comments=True)`. Both trees were
dumped with `ast.dump(tree, include_attributes=False)`, which ignores ordinary
source locations. Two formatter-only representations were normalized:

1. **Type-ignore locations:** `TypeIgnore.lineno` was set to `0` in both trees.
   This affected 60 files and 379 entries. The ignore tag sequence and every
   ignored code node remained unchanged; no other location metadata was
   normalized.
2. **Docstring whitespace:** the docstring value at each module, class and
   function boundary was passed through `inspect.cleandoc`, which removes the
   formatter's uniform indentation and leading/trailing blank lines. This
   affected two files:
   - `docker/versioning/runtime_verification.py`
   - `tests/test_constructor_inventory_root.py`

No docstring word, punctuation character or type-comment payload changed under
`inspect.cleandoc`; the comparison reported no content changes for those
docstrings. No ordinary source location was ignored beyond the dump's
`include_attributes=False` behavior.

After those normalizations, every one of the 371 changed files had an identical
AST. The 25 unmodified files were already byte-identical. No source-sensitive
test repair, import sort, configuration edit or handwritten code change was
introduced to obtain equivalence.

## Reproducibility notes

The comparison used the temporary pre-format copy described in
[`phase-3.md`](phase-3.md); the temporary path is not a durable evidence link.
The durable before/after SHA-256 manifests identify every inventory file.
