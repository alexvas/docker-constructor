# Enforcement checks

Repository lifecycle enforcement lives here rather than in a growing list in
`AGENTS.md`:

- descriptor/stream ownership, direct-close classification and transfer proofs;
- process policy, budgets, retained cleanup and adapter API/dependency contracts;
- executable lifecycle guides, project guidance and checked cross-references.

`process_convergence.py` supplies the shared complete-tree audit and generated
architecture/pipe inventory. Behavioral, fault-injection and real-pipe tests
remain alongside their domain suites in `tests/`.

Run from the repository root:

```sh
.venv/bin/python -m pytest tests/enforcement -n 8 --dist loadfile --tb=short -q
```

The normal `pytest tests` command also discovers this package. Keep new
lifecycle enforcement checks here; link documentation to this directory unless
a concrete executable scenario needs a checked file/definition anchor.
