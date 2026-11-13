"""Backend seam: start fields, execution/public events, and session control.

Adapters run under a single claimed turn. They emit only normalized public
events and return a settlement; ownership, turns and reservations stay shared.
A backend need not use ACPX. Register it here and ship its module with the runner.
"""
from __future__ import annotations
from dataclasses import dataclass
try:
    from .acpx_backend import AcpxBackend
    from .agy import AgyBackend
except ImportError:
    from acpx_backend import AcpxBackend
    from agy import AgyBackend


@dataclass(frozen=True)
class TurnResult:
    state: str
    exit_code: int | None
    stop_reason: str | None
    note: str | None = None


BACKENDS = {name: AcpxBackend() for name in ('omp', 'codex', 'pi')}
BACKENDS['agy'] = AgyBackend()


def backend_for(agent):
    try:
        return BACKENDS[agent]
    except KeyError:
        raise ValueError('Unknown backend: '+str(agent)) from None


def prepare_start(agent, model=None, effort=None, agy_approval=None, *, agy_swarm=None, agy_worker_model=None):
    if agent != 'agy':
        if agy_swarm is not None or agy_worker_model is not None:
            raise ValueError('agy_swarm/agy_worker_model are supported only for AGY')
        return backend_for(agent).prepare_start(model, effort, agy_approval)
    argon = isinstance(model, str) and (model == 'gemini-4-argon' or model.startswith('gemini-4-argon-'))
    mode = ('off' if argon else 'native') if agy_swarm is None else agy_swarm
    if mode not in {'native', 'off'}:
        raise ValueError('agy_swarm must be native or off')
    if mode == 'off' and agy_worker_model is not None:
        raise ValueError('agy_worker_model requires native swarm')
    if mode == 'native':
        if model is not None:
            backend_for(agent).validate_selection(model, None)
        if agy_worker_model is not None:
            backend_for(agent).validate_selection(agy_worker_model, None)
        fields = backend_for(agent).prepare_start(model or 'native-intent', effort, agy_approval)
        if model is None:
            fields.pop('model', None)
        if agy_worker_model is not None:
            fields['agy_worker_model'] = agy_worker_model
    else:
        fields = backend_for(agent).prepare_start('gemini-4-carbon' if model is None else model, effort, agy_approval)
    fields['agy_swarm'] = mode
    return fields
