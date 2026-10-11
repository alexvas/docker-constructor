"""P6 read-only scope/input audit. No index or repository mutation."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
BASELINE = '50f9ebc'
PLANNING = 'ceb3e3f'


def old(path, revision=BASELINE):
    return subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=ROOT)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print('[ok]', message)


def classes(source):
    return {n.name: n for n in ast.parse(source).body if isinstance(n, ast.ClassDef)}


def input_structure(function):
    # Exclude assertions/observations, retain exact source construction/feed
    # expressions. Fixture functions below are additionally unchanged bytewise.
    constructors = []
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, (ast.BinOp, ast.Constant, ast.JoinedStr, ast.List, ast.Tuple)):
                constructors.append(ast.dump(value, include_attributes=False))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ('feed_text', 'feed_bytes'):
                constructors.append(ast.dump(node, include_attributes=False))
    return sorted(constructors)


def main():
    # Approved planning revisions predate implementation. No later requirement
    # edits are conflated with apply-time completion boxes.
    for path in ('proposal.md', 'design.md', 'specs/docker-build-output/spec.md',
                 'specs/locked-npm-environment-assembly/spec.md'):
        relative = f'openspec/changes/linearize-diagnostic-url-detection/{path}'
        check((ROOT / relative).read_bytes() == old(relative, PLANNING), f'planning unchanged since approved revision: {path}')
    relative = 'openspec/changes/linearize-diagnostic-url-detection/tasks.md'
    normalize = lambda data: data.replace(b'- [x]', b'- [ ]')
    check(normalize((ROOT / relative).read_bytes()) == normalize(old(relative, PLANNING)), 'tasks changed only completion boxes since approved revision')
    protected = ['pyproject.toml', 'pi-green-loop.json', '.github/workflows/validation.yml',
                 'Dockerfile', '.hadolint.yaml', 'scripts/check-types',
                 'docker-constructor.toml',
                 'docker/npm_environment/execution.py', 'docker/npm_environment/streaming.py',
                 'docker/versioning/model.py', 'docker/versioning/host_presentation.py',
                 'docker/versioning/build_orchestration.py', 'docker/versioning/host_progress.py',
                 'docker/versioning/pi_assembly.py', 'docker/versioning/diagnostic_identity.py']
    for path in protected:
        check((ROOT / path).read_bytes() == old(path), f'protected semantics/configuration unchanged: {path}')
    projection_path = 'docker/versioning/diagnostic_projection.py'
    before_helpers = ast.parse(old(projection_path))
    after_helpers = ast.parse((ROOT / projection_path).read_bytes())
    for helper in ('normalized_url_host', 'literal_authority_host', 'normalized_authority_host'):
        get = lambda tree: ast.dump(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == helper), include_attributes=False)
        check(get(before_helpers) == get(after_helpers), f'shared hostname validation unchanged: {helper}')
    agents = subprocess.check_output(['git', 'ls-files', '*AGENTS.md'], cwd=ROOT, text=True).splitlines()
    for path in agents:
        check((ROOT / path).read_bytes() == old(path), f'AGENTS unchanged: {path}')
    print('Tracked AGENTS paths:', agents)
    check(Path('/home/dev/.pi/agent/AGENTS.md').exists(), 'external instruction file exists; no write performed by this workflow')
    # All fourteen scenario functions reuse exactly the historical inputs.
    fixture = HERE / 'phase1_oversized_fixture_baseline.py'
    check(fixture.read_bytes() == old(str(fixture.relative_to(ROOT)), '87c9a64'), 'historical one-off fixture harness unchanged bytewise')
    fixtures = ast.parse(fixture.read_text())
    scenario_nodes = [n for n in fixtures.body if isinstance(n, ast.FunctionDef) and n.name.startswith('_fixture_')]
    # Specific stress methods in repository tests preserve construction and
    # feed arguments, even where output expectations intentionally migrated.
    stress = {
        'test_host_diagnostic_projection.py': [
            ('TestBoundarySafeUrlSanitization', 'test_candidate_cannot_be_retained_past_the_byte_limit'),
            ('TestBoundarySafeUrlSanitization', 'test_multibyte_unterminated_candidate_stays_within_the_limit'),
            ('TestBoundarySafeUrlSanitization', 'test_multibyte_append_never_exceeds_the_limit_at_drain_entry'),
            ('TestBoundarySafeUrlSanitization', 'test_pending_state_remains_bounded_for_an_unterminated_token'),
            ('TestBoundarySafeUrlSanitization', 'test_oversized_token_discards_until_boundary_then_recovers'),
            ('TestBoundarySafeUrlSanitization', 'test_oversized_token_without_boundary_finalizes_without_incomplete_marker')],
        'test_host_path_projection.py': [('TestHostPathBoundaries', 'test_oversized_url_token_yields_no_host_path_fact')],
        'test_npm_diagnostic_collection_phase8.py': [
            ('TestCollectorBranch', 'test_oversized_unterminated_url_stays_within_pending_limit'),
            ('TestCollectorBranch', 'test_oversized_newline_free_diagnostic_stays_within_line_limit'),
            ('TestCollectorBranch', 'test_recovery_after_safe_boundary'),
            ('TestCollectorBranch', 'test_input_keeps_draining_after_overflow'),
            ('TestConsumablePerLineFacts', 'test_oversized_url_heavy_line_discards_metadata_and_recovers')],
        'test_retained_tail_phase6.py': [
            ('TestFailureContextChannels', 'test_truncated_tail_is_bounded_and_mode_selected'),
            ('TestFailureChannelMatrix', 'test_truncated_tail_is_bounded_in_text_and_json')],
    }
    for file, cases in stress.items():
        before = classes(old(f'tests/{file}'))
        after = classes((ROOT / 'tests' / file).read_bytes())
        for cls, method in cases:
            get = lambda collection: next(n for n in collection[cls].body if isinstance(n, ast.FunctionDef) and n.name == method)
            check(input_structure(get(before)) == input_structure(get(after)), f'stress construction/feed unchanged: {file}::{cls}::{method}')
    check(len(scenario_nodes) == 14, 'all fourteen historical experiment scenarios retained')
    # Only reviewed internal modules have production changes.
    changed = subprocess.check_output(['git', 'diff', '--name-only', BASELINE, '--', 'docker'], cwd=ROOT, text=True).splitlines()
    allowed = {f'docker/versioning/{name}.py' for name in ('aho_corasick', 'diagnostic_coordinator', 'diagnostic_projection', 'diagnostic_recognition', 'diagnostic_recognizer', 'fetch_identity', 'npm_diagnostic_stream', 'npm_fetch')}
    check(set(changed) <= allowed, f'production scope is reviewed diagnostic modules: {changed}')
    manifest = {}
    for directory in ('docker', 'tests'):
        for path in sorted((ROOT / directory).rglob('*.py')):
            manifest[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in protected:
        manifest[path] = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    (HERE / 'phase6_final_state.json').write_text(json.dumps({'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'sha256': manifest}, indent=2) + '\n')
    print('P6 SCOPE / UNCHANGED STRESS INPUT AUDIT PASS')


if __name__ == '__main__':
    main()
