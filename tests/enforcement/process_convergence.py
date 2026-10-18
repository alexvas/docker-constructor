"""Integrated process ownership audit shared by fixtures and tree inventory.

This gate extends the proven filesystem state machine and generic coordinator,
not the historical migration function list. It scans every production source
for subprocess factories, PIPE/stream/callable aliases and process API use.
The general raw-fd repository release audit remains a separate Phase 7 gate.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
from functools import lru_cache

from tests.enforcement.test_descriptor_close_convergence import (
    _close_violations_with_owners,
    _module_aliases,
)
from tests.enforcement.test_npm_environment_process_contract import (
    adapter_proof,
    functions,
)
from tests.enforcement.test_process_contract import methods, process_proof


def _imports(tree, path):
    """Resolve direct, qualified and relative imports without loading code."""
    aliases = {}
    package = path.removesuffix(".py").replace("/", ".").rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split(".")[0]] = (
                    name.name if name.asname else name.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            if node.level:
                module = importlib.util.resolve_name(module, package)
            for name in node.names:
                aliases[name.asname or name.name] = module + "." + name.name
    return aliases


def _shape(node):
    return ast.dump(node, include_attributes=False)


def _contains(tree, expression):
    expected = _shape(ast.parse(expression).body[0])
    return any(_shape(node) == expected for node in ast.walk(tree))


def _coordination_proof(source, execution, lifecycle):
    """Require real guarded operations/dataflow, not decoy token presence."""
    errors = []
    tree = ast.parse(source)
    m = methods(source)
    validate = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_validate_policy"
    )
    if not any(
        isinstance(n, ast.If)
        and ast.unparse(n.test) == "math.fsum(caps) > total"
        and any(isinstance(s, ast.Raise) for s in n.body)
        for n in validate.body
    ):
        errors.append("phase allocation lacks prelaunch total-budget rejection")
    stages = next(n for n in m["_run_policy"].body if isinstance(n, ast.For))
    clocks = [
        n
        for n in ast.walk(stages)
        if isinstance(n, ast.Assign)
        and any(ast.unparse(t) == "stage_end" for t in n.targets)
    ]
    expected = "stage_end = min(time.monotonic() + stage.seconds, end - policy.local_cleanup_seconds)"
    if (
        len(clocks) != 1
        or clocks[0] not in stages.body
        or not _contains(clocks[0], expected)
    ):
        errors.append(
            "phase clock is not an absolute cap before initiation and waiting"
        )
    else:
        starts = [
            n
            for n in ast.walk(stages)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "action.start"
        ]
        if not starts or any(n.lineno <= clocks[0].lineno for n in starts):
            errors.append("phase execution precedes phase clock")
    launch = m["launch"]
    preparations = [
        n
        for n in ast.walk(launch)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "stage.action is not None"
    ]
    if not any(
        _contains(n, "self._action_prepared[index] = True")
        and _contains(n, "self._action_results[index] = ActionResult(False, False)")
        and next(
            s.lineno
            for s in n.body
            if _contains(s, "self._action_prepared[index] = True")
        )
        < next(
            s.lineno
            for s in n.body
            if _contains(s, "self._action_results[index] = ActionResult(False, False)")
        )
        < next(
            c.lineno
            for c in ast.walk(n)
            if isinstance(c, ast.Call) and ast.unparse(c.func) == "stage.action.prepare"
        )
        for n in preparations
    ):
        errors.append(
            "partial action preparation is not retained before fallible readiness"
        )
    publish = m["_publish"]
    if not any(
        isinstance(n, ast.With)
        and any(
            _contains(s, "self._status = _outcome(self._ledger, complete)")
            for s in n.body
        )
        for n in publish.body
    ):
        errors.append("winning outcomes not published under the coordination lock")
    for name, method in m.items():
        if name == "launch":
            continue
        for node in ast.walk(method):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if any(
                ast.unparse(t) == "self._owners"
                or ast.unparse(t).startswith("self._owners[")
                for t in targets
            ):
                errors.append("prepared pipe owners discarded/replaced after launch")
            if isinstance(node, ast.Call) and ast.unparse(node.func) in (
                "self._owners.clear",
                "self._owners.pop",
            ):
                errors.append("deferred owners removed before their release")
    execute = functions(ast.parse(execution))
    observe, work = execute["observe"], execute["_work"]
    for expression in (
        "status = self.owner.cleanup_status",
        "complete = complete and status.complete",
    ):
        if not _contains(observe, expression):
            errors.append("managed helper cleanup is falsely completed or lost")
    for expression in (
        "remaining = context.deadline - time.monotonic()",
        "self.helper_policy = LifecyclePolicy(min(self.grace, remaining / 3), process_label='docker rm -f client')",
        "self.owner = operation.owner",
    ):
        if not _contains(work, expression):
            errors.append("managed helper owner or parent remaining budget lost")
    if not any(
        isinstance(n, ast.Try)
        and any(_contains(s, "self.done.set()") for s in n.finalbody)
        for n in ast.walk(work)
    ):
        errors.append(
            "managed action settlement not published after every work outcome"
        )
    captured = functions(ast.parse(lifecycle))
    returns = [
        n for n in ast.walk(captured["_run_captured"]) if isinstance(n, ast.Return)
    ]
    if not returns or any(
        ast.unparse(n.value) != "_CapturedOperation(owner, outcome, failures)"
        for n in returns
    ):
        errors.append("captured outcome loses the original cleanup owner")
    if not _contains(
        captured["run_captured"], "return (operation.owner, operation.outcome)"
    ):
        errors.append(
            "public captured return no longer preserves owner and domain snapshot"
        )
    return errors


def _origins(node, aliases):
    """Small union dataflow: retain acquisition provenance across reassignment."""
    text = ast.unparse(node)
    if text in aliases:
        return aliases[text]
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {base + "." + node.attr for base in _origins(node.value, aliases)}
    if isinstance(node, ast.Subscript):
        return {base + "[]" for base in _origins(node.value, aliases)}
    if isinstance(node, ast.Call):
        callees = _origins(node.func, aliases)
        if "subprocess.Popen" in callees:
            return {"@child"}
        if "docker.process.OwnedProcess.launch" in callees:
            return {"@owned"}
        if any(c.endswith(".fileno") for c in callees):
            return {"@fileno"}
        # Non-acquiring calls are opaque values, not endlessly expanding
        # callable origins (e.g. repeated result/observation assignments).
        return {"@value"}
    return {text}


def _bindings(tree, imported):
    aliases = {name: {origin} for name, origin in imported.items()}
    # Only names/attributes/subscripts propagate; calls acquire resource tags.
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend((t, node.value) for t in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append((node.target, node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            assignments.extend(
                (item.optional_vars, item.context_expr)
                for item in node.items
                if item.optional_vars is not None
            )
        elif isinstance(node, ast.FunctionDef):
            args = node.args.posonlyargs + node.args.args
            assignments.extend(
                (ast.Name(id=arg.arg), default)
                for arg, default in zip(
                    args[-len(node.args.defaults) :], node.args.defaults
                )
            )
            assignments.extend(
                (ast.Name(id=arg.arg), default)
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
                if default is not None
            )
    # Fixed point for transitive and assigned-callable aliases. Keep only
    # imports and resource/control provenance, never arbitrary data values.
    for _ in range(len(assignments) + 1):
        changed = False
        for target, value in assignments:
            if not isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
                continue
            key = ast.unparse(target)
            origins = _origins(value, aliases)
            origins = {
                o
                for o in origins
                if o != key
                and not o.startswith(key + ".")
                and (
                    o.startswith(
                        (
                            "subprocess",
                            "docker.",
                            "os.",
                            "importlib.",
                            "__import__",
                            "@child",
                            "@owned",
                            "@fileno",
                        )
                    )
                    or _pipe(o)
                )
            }
            previous = aliases.get(key, set())
            if not origins <= previous:
                aliases[key] = previous | origins
                changed = True
        if not changed:
            break
    return aliases


def _dependencies(tree, imported, aliases):
    """Share static and resolved literal dynamic dependencies across both gates."""
    dependencies = set(imported.values()) | {
        name.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callees = _origins(node.func, aliases)
        if not callees & {"importlib.import_module", "__import__"}:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        name = node.args[0] if node.args else keywords.get("name")
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            continue
        dependency = name.value
        if dependency.startswith("."):
            if "importlib.import_module" not in callees:
                continue
            package = node.args[1] if len(node.args) > 1 else keywords.get("package")
            if not isinstance(package, ast.Constant) or not isinstance(
                package.value, str
            ):
                continue
            try:
                dependency = importlib.util.resolve_name(dependency, package.value)
            except ImportError, ValueError:
                # Invalid relative imports cannot load a dependency. Never
                # execute a source import to discover its target.
                continue
        dependencies.add(dependency)
    return dependencies


def _pipe(origin):
    return any(part in ("stdout", "stderr", "_owners[]") for part in origin.split("."))


def _audit_tree(path, tree, generic_valid):
    errors, ledger = [], []
    imported = _imports(tree, path)
    aliases = _bindings(tree, imported)
    parents = {child: n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}

    def scope(node):
        names = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
        return ".".join(reversed(names))

    def row(node, kind, **facts):
        ledger.append(
            dict(path=path, line=node.lineno, scope=scope(node), kind=kind, **facts)
        )

    allowed_factory = None
    allowed_release_lines = set()
    if path == "docker/process.py" and generic_valid:
        # Authority is conditional on the full transfer/barrier proof, not
        # granted to any method/class merely named like an owner.
        process_class = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "OwnedProcess"
        )
        actual_release = next(
            n
            for n in process_class.body
            if isinstance(n, ast.FunctionDef) and n.name == "_release_safe"
        )
        allowed_release_lines = {
            n.lineno
            for n in ast.walk(actual_release)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "owner.close"
        }
        candidates = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(ast.unparse(t) == "self._proc" for t in n.targets)
            and isinstance(n.value, ast.Call)
            and "subprocess.Popen" in _origins(n.value.func, aliases)
        ]
        if len(candidates) == 1:
            allowed_factory = candidates[0]
    for origin in _dependencies(tree, imported, aliases):
        if origin == "docker.filesystem":
            errors.append(f"aggregate filesystem import: {path}: {origin}")
        if path.startswith("docker/filesystem/") and origin.startswith(
            (
                "docker.process",
                "subprocess",
                "docker.npm_environment",
                "docker.versioning",
                "docker.transactions",
            )
        ):
            errors.append(f"reverse dependency: {path}: {origin}")
        if (
            path == "docker/process.py"
            and origin.startswith("docker.")
            and not origin.startswith(
                ("docker.filesystem.cleanup.", "docker.filesystem.streams.")
            )
        ):
            errors.append(f"domain dependency: {path}: {origin}")
        if path == "docker/runtime_installer.py" and origin.startswith(
            "docker.process"
        ):
            errors.append(f"runtime installer process dependency: {origin}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
            "docker.process.OwnedProcess" in _origins(base, aliases)
            for base in node.bases
        ):
            errors.append(f"whole shutdown override: {path}:{node.lineno}")
        if isinstance(node, ast.Attribute):
            origins = _origins(node, aliases)
            if (
                path != "docker/process.py"
                and node.attr
                in ("_proc", "_owners", "_readers", "_fds", "_tails", "_ledger")
                and any(o.startswith("@owned.") for o in origins)
            ):
                errors.append(
                    f"borrowed private process authority: {path}:{node.lineno}"
                )
        if not isinstance(node, ast.Call):
            continue
        callees = _origins(node.func, aliases)
        if "subprocess.Popen" in callees:
            if node is not allowed_factory:
                errors.append(
                    f"unprepared subprocess acquisition: {path}:{node.lineno}"
                )
            else:
                pipes = {
                    kw.arg: sorted(_origins(kw.value, aliases))
                    for kw in node.keywords
                    if kw.arg in ("stdout", "stderr")
                }
                if pipes != {
                    "stdout": ["subprocess.PIPE"],
                    "stderr": ["subprocess.PIPE"],
                }:
                    errors.append(
                        "prepared factory no longer acquires both captured streams"
                    )
                row(
                    node,
                    "prepared-child",
                    pipes=pipes,
                    owner="OwnedProcess / prepared stdout+stderr slots",
                )
        if "docker.process.OwnedProcess.launch" in callees:
            row(
                node,
                "owned-launch",
                owner="OwnedProcess",
                collection="single capture or stream",
            )
        if "docker.npm_environment.lifecycle._run_captured" in callees:
            row(
                node,
                "managed-helper",
                owner="_CapturedOperation.owner / default local OwnedProcess",
            )
        if "subprocess.run" in callees:
            row(
                node,
                "stdlib-run",
                owner="subprocess.run context; no escaping pipe authority",
            )
        if "docker.filesystem.streams._prepare_stream" in callees:
            if not generic_valid or path != "docker/process.py":
                errors.append(
                    f"unproved private stream preparation: {path}:{node.lineno}"
                )
            row(node, "prepared-stream", owner="one internal OwnedStream per pipe")
        if "docker.filesystem.streams._bind_prepared" in callees and (
            not generic_valid or path != "docker/process.py"
        ):
            errors.append(f"unproved private stream transfer: {path}:{node.lineno}")
        if node.lineno not in allowed_release_lines and any(
            c.endswith(".close") and _pipe(c) for c in callees
        ):
            errors.append(f"borrowed pipe release: {path}:{node.lineno}")
        arguments = (*node.args, *(keyword.value for keyword in node.keywords))
        if "os.close" in callees and any(
            "@fileno" in _origins(arg, aliases) for arg in arguments
        ):
            errors.append(f"raw-fileno release: {path}:{node.lineno}")
        if any(
            c
            in (
                "docker.filesystem.descriptors.OwnedDescriptor",
                "docker.filesystem.descriptors.DirectoryDescriptor.adopt",
            )
            for c in callees
        ) and any("@fileno" in _origins(arg, aliases) for arg in arguments):
            errors.append(f"duplicate raw-fileno ownership: {path}:{node.lineno}")
        if "docker.filesystem.streams.OwnedStream" in callees and any(
            _pipe(o) for arg in arguments for o in _origins(arg, aliases)
        ):
            errors.append(
                f"consumer/reset-per-cleanup pipe adoption: {path}:{node.lineno}"
            )
        if path != "docker/process.py" and any(
            c.startswith(("@child.", "@owned._proc."))
            and c.rsplit(".", 1)[-1] in ("wait", "kill", "terminate", "communicate")
            for c in callees
        ):
            errors.append(f"independent raw-child coordination: {path}:{node.lineno}")
    return errors, ledger


@lru_cache(maxsize=1024)
def _audit_source(path, text, generic_valid):
    # Read-only AST results are reusable across independent mutation fixtures.
    return _audit_tree(path, ast.parse(text, filename=path), generic_valid)


def convergence_report(sources):
    """Return auditable facts/errors for the entire supplied production tree."""
    source = sources["docker/process.py"]
    foundation = sources["docker/filesystem/streams.py"]
    execution, lifecycle = (
        sources["docker/npm_environment/" + name + ".py"]
        for name in ("execution", "lifecycle")
    )
    generic_errors = process_proof(source, foundation)
    generic_errors += _coordination_proof(source, execution, lifecycle)
    errors = list(generic_errors) + adapter_proof(execution, lifecycle)
    ledger = []
    for path, text in sorted(sources.items()):
        found, rows = _audit_source(path, text, not generic_errors)
        errors.extend(found)
        ledger.extend(rows)
    fs = ast.parse(foundation)
    stream = next(
        n for n in fs.body if isinstance(n, ast.ClassDef) and n.name == "OwnedStream"
    )
    close = next(
        n for n in stream.body if isinstance(n, ast.FunctionDef) and n.name == "close"
    )
    release_errors = _close_violations_with_owners(
        close, set(), _module_aliases(fs), shared_owner_release=True
    )
    errors.extend(release_errors)
    if not release_errors:
        ledger.append(
            dict(
                path="docker/filesystem/streams.py",
                line=close.lineno,
                scope="OwnedStream.close",
                kind="terminal-release",
                owner="synchronized terminal claim; winning outside-lock release",
            )
        )
    if not generic_errors:
        continuation = methods(source)["_continue_cleanup"]
        ledger.append(
            dict(
                path="docker/process.py",
                line=continuation.lineno,
                scope="OwnedProcess._continue_cleanup",
                kind="retained-continuation",
                owner="same prepared local/action owners; reader barriers and winning outcomes",
            )
        )
    return {"errors": errors, "ledger": ledger, "files_scanned": sorted(sources)}


def architecture_report(sources):
    """Generate signatures, result fields, dependency DAG and pipe inventory."""
    import dataclasses
    import hashlib
    import inspect

    import docker.process as process
    from docker.npm_environment import execution, lifecycle, streaming

    report = convergence_report(sources)
    modules = {
        path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        for path in sources
    }
    edges = set()
    for path, source in sources.items():
        module = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        tree = ast.parse(source)
        imported = _imports(tree, path)
        for origin in _dependencies(tree, imported, _bindings(tree, imported)):
            targets = [
                name
                for name in modules
                if origin == name or origin.startswith(name + ".")
            ]
            if targets:
                edges.add((module, max(targets, key=len)))
    surface = {}
    for name in process.__all__:
        value = getattr(process, name)
        facts = {"signature": str(inspect.signature(value))}
        if dataclasses.is_dataclass(value):
            facts["fields"] = [field.name for field in dataclasses.fields(value)]
        surface[name] = facts
    for name in (
        "launch",
        "capture",
        "stream",
        "shutdown",
        "wait_cleanup",
        "__enter__",
        "__exit__",
    ):
        surface["OwnedProcess." + name] = {
            "signature": str(inspect.signature(getattr(process.OwnedProcess, name)))
        }
    for name in ("cleanup_status", "returncode"):
        surface["OwnedProcess." + name] = {
            "property": True,
            "signature": str(
                inspect.signature(getattr(process.OwnedProcess, name).fget)
            ),
        }
    for name in ("prepare", "start", "observe", "cancel"):
        surface["ManagedAction." + name] = {
            "signature": str(inspect.signature(getattr(process.ManagedAction, name)))
        }
    adapters = {
        name: str(inspect.signature(target))
        for name, target in (
            ("run_captured", lifecycle.run_captured),
            ("DockerRunExecutor.run", execution.DockerRunExecutor.run),
            (
                "DockerRunExecutor.run_streaming",
                execution.DockerRunExecutor.run_streaming,
            ),
            ("collect_streams", streaming.collect_streams),
        )
    }
    return dict(
        report,
        source_sha256={
            p: hashlib.sha256(s.encode()).hexdigest()
            for p, s in sorted(sources.items())
        },
        dependency_edges=sorted([list(edge) for edge in edges]),
        process_surface=surface,
        adapter_signatures=adapters,
        domain_fields={
            "LifecycleOutcome": [
                f.name for f in dataclasses.fields(lifecycle.LifecycleOutcome)
            ],
            "ProcessResult": [
                f.name for f in dataclasses.fields(execution.ProcessResult)
            ],
        },
    )


def _syntax_fingerprint(source):
    """Hash parsed structure with docstring text preserved.

    Docstring text is semantic, so it remains part of the fingerprint.  Only
    the common indentation margin and surrounding blank lines are normalized:
    the formatter may reindent a docstring's continuation lines, which must not
    count as a production change.
    """
    import hashlib

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            first.value.value = inspect.cleandoc(first.value.value)
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def canonical_architecture_report(sources):
    """Return layout-independent architecture facts for fixtures.

    Raw source hashes and ledger line numbers move whenever the formatter
    rewraps code, although the architecture and pipe ownership do not.  The
    canonical projection hashes parsed syntax instead and keeps only the
    structural ledger facts, so a formatter-only change is not a production
    change while any semantic edit still alters a recorded fingerprint.
    """
    report = architecture_report(sources)
    return {
        **{key: value for key, value in report.items() if key != "source_sha256"},
        "ledger": [
            {key: value for key, value in row.items() if key != "line"}
            for row in report["ledger"]
        ],
        "source_ast_sha256": {
            path: _syntax_fingerprint(source)
            for path, source in sorted(sources.items())
        },
    }
