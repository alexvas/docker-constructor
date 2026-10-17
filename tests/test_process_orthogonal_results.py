"""Phase 6BC exact public value contracts; no legacy conversion or aliases."""
from dataclasses import FrozenInstanceError, fields
from enum import Enum
from typing import get_type_hints

import pytest

import docker.process as process


def api(name):
    assert hasattr(process, name), f"missing approved API: {name}"
    return getattr(process, name)


@pytest.mark.parametrize("name,members", [
    ("FailureKind", ("ORDINARY", "UNEXPECTED", "INTERRUPTION")),
    ("FailurePhase", ("SETUP", "READ", "JOIN", "RELEASE", "WAIT", "ACTION", "CALLBACK", "REPORTER", "COORDINATION")),
    ("ProcessStream", ("STDOUT", "STDERR")),
    ("StreamOutcome", ("PENDING", "EOF", "READ_FAILED", "ABORTED")),
])
def test_exact_enums(name, members):
    cls = api(name)
    assert issubclass(cls, Enum)
    assert tuple(cls.__members__) == members


@pytest.mark.parametrize("name,names", [
    ("FailureInfo", ("error", "kind", "phase", "stream")),
    ("ActionResult", ("done", "cleanup_complete", "failures")),
    ("LifecycleResult", ("complete", "failures")),
    ("ProcessResult", ("stdout", "stderr", "returncode", "timed_out", "lifecycle", "stream_outcomes")),
])
def test_exact_frozen_fields(name, names):
    cls = api(name)
    assert tuple(f.name for f in fields(cls)) == names
    assert cls.__dataclass_params__.frozen
    assert set(get_type_hints(cls)) == set(names)


def test_exact_annotations_and_defaults():
    info, kind, phase = api("FailureInfo"), api("FailureKind"), api("FailurePhase")
    stream = api("ProcessStream")
    assert get_type_hints(info) == dict(error=BaseException, kind=kind, phase=phase, stream=stream | None)
    for name in ("ActionResult", "LifecycleResult"):
        assert get_type_hints(api(name))["failures"] == tuple[info, ...]
        assert next(f for f in fields(api(name)) if f.name == "failures").default == ()
    assert next(f for f in fields(info) if f.name == "stream").default is None
    assert get_type_hints(api("ProcessResult")) == dict(
        stdout=bytes, stderr=bytes, returncode=int | None, timed_out=bool,
        lifecycle=api("LifecycleResult"), stream_outcomes=tuple[tuple[stream, api("StreamOutcome")], ...],
    )
    assert next(f for f in fields(api("ProcessResult")) if f.name == "stream_outcomes").default == ()


@pytest.mark.parametrize("phase_name", ["SETUP", "READ", "JOIN", "RELEASE", "WAIT", "ACTION", "CALLBACK", "REPORTER", "COORDINATION"])
@pytest.mark.parametrize("stream_name", [None, "STDOUT", "STDERR"])
def test_phase_stream_validation(phase_name, stream_name):
    phase = getattr(api("FailurePhase"), phase_name)
    stream = getattr(process.ProcessStream, stream_name) if stream_name else None
    kwargs = dict(error=OSError("operation"), kind=api("FailureKind").ORDINARY, phase=phase, stream=stream)
    valid = (stream is not None if phase_name in ("READ", "CALLBACK", "REPORTER") else
             stream is None if phase_name in ("WAIT", "ACTION", "COORDINATION") else True)
    if not valid:
        with pytest.raises((TypeError, ValueError)):
            api("FailureInfo")(**kwargs)
    else:
        result = api("FailureInfo")(**kwargs)
        assert result.error is kwargs["error"]
        assert result.stream is stream and result.phase is phase
        with pytest.raises(FrozenInstanceError):
            result.stream = None


@pytest.mark.parametrize("field,value", [
    ("error", None), ("error", "failure"), ("error", OSError),
    ("kind", None), ("kind", "ORDINARY"), ("kind", 1),
    ("phase", None), ("phase", "SETUP"), ("phase", 1),
    ("stream", "stdout"), ("stream", 0),
])
def test_reject_invalid_info_types(field, value):
    kwargs = dict(error=OSError("original"), kind=api("FailureKind").ORDINARY, phase=api("FailurePhase").SETUP)
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError)):
        api("FailureInfo")(**kwargs)


@pytest.mark.parametrize("error", [OSError("ordinary"), InterruptedError("not control flow"), RuntimeError("defect"), KeyboardInterrupt(), SystemExit(7), GeneratorExit(), BaseException("control")])
@pytest.mark.parametrize("kind_name", ["ORDINARY", "UNEXPECTED", "INTERRUPTION"])
def test_kind_cannot_demote_or_fabricate_control_flow(error, kind_name):
    kwargs = dict(error=error, kind=getattr(api("FailureKind"), kind_name), phase=api("FailurePhase").ACTION)
    valid = (not isinstance(error, Exception)) == (kind_name == "INTERRUPTION")
    if valid:
        assert api("FailureInfo")(**kwargs).error is error
    else:
        with pytest.raises((TypeError, ValueError)):
            api("FailureInfo")(**kwargs)


@pytest.mark.parametrize("done,complete", [(False, False), (False, True), (True, False), (True, True)])
def test_action_progress_is_independent_of_failure_presence(done, complete):
    error = ValueError("settled does not mean success")
    failure = api("FailureInfo")(error, api("FailureKind").ORDINARY, api("FailurePhase").ACTION)
    for failures in ((), (failure,)):
        result = process.ActionResult(done, complete, failures)
        assert result.done is done and result.cleanup_complete is complete
        assert result.failures == failures
        with pytest.raises(FrozenInstanceError):
            result.done = not done


@pytest.mark.parametrize("record", ["ActionResult", "LifecycleResult"])
@pytest.mark.parametrize("bad", [None, [], [OSError("bare")], (OSError("bare"),), (None,), "failure"])
def test_reject_bare_or_mutable_failure_transport(record, bad):
    args = (True, True) if record == "ActionResult" else (True,)
    with pytest.raises((TypeError, ValueError)):
        api(record)(*args, failures=bad)


@pytest.mark.parametrize("phase_name,stream_name", [("READ", "STDOUT"), ("JOIN", "STDERR"), ("SETUP", None), ("WAIT", None), ("COORDINATION", None)])
def test_action_requires_rebased_context(phase_name, stream_name):
    failure = api("FailureInfo")(
        OSError("helper"), api("FailureKind").ORDINARY,
        getattr(api("FailurePhase"), phase_name),
        getattr(process.ProcessStream, stream_name) if stream_name else None,
    )
    with pytest.raises((TypeError, ValueError)):
        process.ActionResult(True, True, (failure,))
    assert api("LifecycleResult")(True, (failure,)).failures == (failure,)


@pytest.mark.parametrize("record,args,kwargs", [
    ("ActionResult", (True, True), {"error": OSError("legacy")}),
    ("ActionResult", (True, True), {"failure_info": None}),
    ("LifecycleResult", (True,), {"error": None}),
    ("LifecycleResult", (True,), {"actions": ()}),
    ("ProcessResult", (b"", b"", 0, False), {"cleanup": None}),
    ("ProcessResult", (b"", b"", 0, False), {"reader_failure_stream": None}),
    ("ProcessResult", (b"", b"", 0, False), {"callback_errors": ()}),
])
def test_no_legacy_field_conversion(record, args, kwargs):
    cls = api(record)
    assert not any(f.name in kwargs for f in fields(cls))
    with pytest.raises(TypeError):
        cls(*args, **kwargs)


def test_no_cleanup_result_alias():
    assert not hasattr(process, "CleanupResult")
