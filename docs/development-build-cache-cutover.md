# One-time development build-cache cutover

The immutable build-generation format does not migrate the old development
build cache. Perform this cutover once before using the changed build code in
this checkout.

Run from the constructor project root. Stop any Constructor build for this
project first. This instruction resolves the selected project's external state
namespace and removes only its `build-artifacts` child; it does not remove the
shared cache root, another project's namespace, runtime artifacts, or version
metadata.

```sh
python - <<'PY'
from pathlib import Path
import shutil

from docker.versioning.project_state import resolve_project_state

state = resolve_project_state(Path.cwd(), create=False)
target = state.build_artifacts_root
if target.name != "build-artifacts":
    raise SystemExit(f"refusing unexpected build-cache target: {target}")
print(f"removing old development build cache: {target}")
shutil.rmtree(target)
PY
```

If the command reports that the target does not exist, no old build cache
remains. Do not replace this instruction with a wildcard or removal of
`~/.cache`, `$XDG_CACHE_HOME`, the `docker-constructor` cache root, or the
project namespace. The next build recreates the selected project's
`build-artifacts` directory and starts with generation 1. Runtime code ignores
`committed-build.json`; it does not inspect, adopt, reject, or delete that
legacy file.
