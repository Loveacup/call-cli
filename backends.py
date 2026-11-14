"""Backend seam: start fields, execution/public events, and session control.

Adapters run under a single claimed turn. They emit only normalized public
events and return a settlement; ownership, turns and reservations stay shared.
A backend need not use ACPX. Register it here and ship its module with the runner.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
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


class ModelSelectionError(ValueError):
    """Public start rejection containing only fixed selection guidance."""


def prepare_start(agent, model=None, effort=None, agy_approval=None, *, agy_swarm=None, agy_worker_model=None, agy_mode=None):
    if agy_mode is not None:
        if agent != 'agy' or agy_mode not in {'single', 'swarm', 'dynamic'}:
            raise ModelSelectionError('Unsupported explicit AGY mode')
        route = 'off' if agy_mode == 'single' else 'native'
        if agy_swarm is not None and agy_swarm != route:
            raise ModelSelectionError('agy_mode conflicts with agy_swarm')
        if agy_mode == 'dynamic' and agy_worker_model is not None:
            raise ModelSelectionError('Dynamic child tiers belong to the approved script, not agy_worker_model')
        if agy_mode == 'dynamic' and isinstance(model, str) and re.fullmatch(r'(?:oc|oa|4[cah])[st]?', model):
            raise ModelSelectionError('Dynamic requires an advertised model ID, not a mode shorthand')
        agy_swarm = route
    if agent != 'agy':
        if agy_swarm is not None or agy_worker_model is not None:
            raise ValueError('agy_swarm/agy_worker_model are supported only for AGY')
        return backend_for(agent).prepare_start(model, effort, agy_approval)
    aliases = {'oc':'gemini-pro-orchestrator', 'oa':'gemini-pro-orchestrator-argon',
               '4c':'gemini-4-carbon', '4a':'gemini-4-argon', '4h':'gemini-4-hydrogen'}
    if isinstance(model, str):
        if model.endswith('t') and model[:-1] in aliases:
            raise ModelSelectionError('Teamwork intent is unsupported; no business prompt submitted')
        base = model[:-1] if model.endswith('s') else model
        if base in aliases:
            shorthand_mode = 'native' if model.endswith('s') else 'off'
            if agy_swarm is not None and agy_swarm != shorthand_mode:
                raise ModelSelectionError('model shorthand conflicts with agy_swarm; bare requires off, s requires native')
            agy_swarm = shorthand_mode
            model = aliases[base]
        elif re.fullmatch(r'(?:oc|oa|4[cah])[st]*(?:[ ,_:/+\-]*(?:oc|oa|4[cah])[st]*)*'
                          r'(?: +[st]+)?(?:-(?:low|medium|high))?', model, re.IGNORECASE):
            raise ModelSelectionError('Invalid AGY shorthand; use oc/oa/4c/4a/4h with at most one s suffix; t is unsupported')
    model = 'gemini-pro-orchestrator' if model is None else model
    argon = isinstance(model, str) and (model == 'gemini-pro-orchestrator-argon'
        or model == 'gemini-4-argon' or model.startswith('gemini-4-argon-'))
    mode = ('off' if argon else 'native') if agy_swarm is None else agy_swarm
    if mode not in {'native', 'off'}:
        raise ValueError('agy_swarm must be native or off')
    if mode == 'off' and agy_worker_model is not None:
        raise ValueError('agy_worker_model requires native swarm')
    if mode == 'native':
        backend_for(agent).validate_selection(model, None)
        if agy_worker_model is not None:
            backend_for(agent).validate_selection(agy_worker_model, None)
        fields = backend_for(agent).prepare_start(model, effort, agy_approval)
        if agy_worker_model is not None:
            fields['agy_worker_model'] = agy_worker_model
    else:
        fields = backend_for(agent).prepare_start(model, effort, agy_approval)
    fields['agy_swarm'] = mode
    if agy_mode is not None:
        fields['agy_mode'] = agy_mode
    return fields
