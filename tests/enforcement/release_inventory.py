"""Read-only Phase 7 inventory; bounded checks, not a Python liveness proof.

Pass the complete production source mapping used by process_convergence.
Close references include callbacks and backend defaults, not just calls.
Unknown receivers remain review obligations, never non-descriptor exemptions.
"""

from __future__ import annotations

import ast

from tests.enforcement import test_descriptor_close_convergence as gate
from tests.enforcement.process_convergence import architecture_report


def release_inventory(sources):
    report = architecture_report(sources)
    references = []
    implicit = []
    violations = []
    factory_candidates = []
    for path, source in sorted(sources.items()):
        tree = ast.parse(source, filename=path)
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        aliases = gate._module_aliases(tree)
        functions = [
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        for function in functions:
            owners = gate._ownerish_names(function, aliases)
            for kind, errors in (
                ("fdopen", gate._fdopen_violations(function, owners, aliases)),
                ("mkstemp", gate._mkstemp_violations(function, owners, aliases)),
                (
                    "duplicate-adoption",
                    gate._duplicate_fd_adoption_violations(function, aliases),
                ),
            ):
                violations.extend(
                    {
                        "path": path,
                        "line": function.lineno,
                        "function": function.name,
                        "kind": kind,
                        "error": error,
                    }
                    for error in errors
                )
        local_aliases = {
            function: gate._local_primitive_aliases(function, aliases)
            for function in functions
        }
        for node in ast.walk(tree):
            if not (
                (isinstance(node, ast.Attribute) and node.attr == "close")
                or isinstance(node, ast.Call)
            ):
                continue
            current = node
            scopes = []
            function = None
            while current in parents:
                current = parents[current]
                if isinstance(
                    current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    scopes.insert(0, current.name)
                if function is None and isinstance(current, ast.FunctionDef):
                    function = current
            local = local_aliases[function] if function else aliases
            if isinstance(node, ast.Attribute) and node.attr == "close":
                references.append(
                    dict(
                        path=path,
                        line=node.lineno,
                        scope=".".join(scopes),
                        expression=ast.unparse(node),
                        origin=gate._qualname(node, local),
                        direct_call=isinstance(parents.get(node), ast.Call)
                        and parents[node].func is node,
                    )
                )
            if isinstance(node, ast.Call):
                origin = gate._call_origin(node, local)
                if (
                    origin in {"open", "io.open", "urllib.request.urlopen"}
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {
                        "open",
                        "read_text",
                        "read_bytes",
                        "server_close",
                        "ThreadingHTTPServer",
                    }
                ):
                    factory_candidates.append(
                        dict(
                            path=path,
                            line=node.lineno,
                            scope=".".join(scopes),
                            origin=origin,
                            expression=ast.unparse(node),
                        )
                    )
                if origin in {"os.fdopen", "tempfile.mkstemp"}:
                    implicit.append(
                        dict(
                            path=path,
                            line=node.lineno,
                            scope=".".join(scopes),
                            origin=origin,
                            expression=ast.unparse(node),
                        )
                    )
    report["close_references"] = sorted(
        references, key=lambda row: (row["path"], row["line"])
    )
    report["implicit_descriptor_calls"] = sorted(
        implicit, key=lambda row: (row["path"], row["line"])
    )
    report["implicit_descriptor_errors"] = violations
    report["factory_candidates"] = sorted(
        factory_candidates, key=lambda row: (row["path"], row["line"])
    )
    report["limitations"] = [
        "Close references are an inventory, not automatic release classifications.",
        "Owner liveness checks have the task 7.1 indirect-alias/callback/collection limitations.",
        "Delegated network/native-file origins are not classified as safe by this report.",
        "Library-internal subprocess.run pipes follow the approved synchronous-call boundary.",
    ]
    return report
