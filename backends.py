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
EXPLICIT_BACKENDS = frozenset({'acpx', 'oar'})
_AGY_ALIASES = {
    'oc': 'gemini-pro-orchestrator',
    'oa': 'gemini-pro-orchestrator-argon',
    '4c': 'gemini-4-carbon',
    '4a': 'gemini-4-argon',
    '4h': 'gemini-4-hydrogen',
    'on': 'gemini-pro-orchestrator-neon',
    '4n': 'gemini-4-neon',
    **{'g4' + k: 'gemini-4-' + v for k, v in zip('cahn', ('carbon', 'argon', 'hydrogen', 'neon'))},
}
_AGY_BAD_SHORTHAND_RE = re.compile(
    r'(?:o[can]|g?4[cahn])[st]*(?:[ ,_:/+\-]*(?:o[can]|g?4[cahn])[st]*)*(?: +[st]+)?(?:-(?:low|medium|high|xhigh))?',
    re.IGNORECASE,
)


def _oar_backend():
    try:
        from .oar_backend import OarBackend
    except ImportError:
        from oar_backend import OarBackend
    return OarBackend()


def backend_for(target, backend=None):
    if isinstance(target, dict):
        agent = target.get('agent')
        resolved = target.get('backend') if backend is None else backend
    else:
        agent = target
        resolved = backend
    if resolved == 'oar':
        if agent != 'agy':
            raise ValueError('OAR backend is supported only for AGY')
        return _oar_backend()
    if resolved not in (None, 'acpx'):
        raise ValueError('Unknown backend override: ' + str(resolved))
    try:
        return BACKENDS[agent]
    except KeyError:
        raise ValueError('Unknown backend: '+str(agent)) from None


class ModelSelectionError(ValueError):
    """Public start rejection containing only fixed selection guidance."""


def _prepare_oar_agy(model, effort, agy_approval, agy_swarm, agy_worker_model, agy_mode):
    if agy_mode is not None and agy_mode != 'single':
        raise ModelSelectionError('OAR backend cannot be combined with AGY swarm/teamwork/dynamic modes')
    if agy_swarm is not None and agy_swarm != 'off':
        raise ModelSelectionError('OAR backend conflicts with agy_swarm=native')
    if agy_worker_model is not None:
        raise ValueError('OAR backend does not support agy_worker_model')
    if isinstance(model, str):
        suffix = model[-1:] if model[-1:] in {'s', 't'} else ''
        base = model[:-1] if suffix else model
        if base in _AGY_ALIASES:
            if suffix:
                raise ModelSelectionError('OAR backend cannot be combined with AGY swarm/teamwork shorthand')
            model = _AGY_ALIASES[base]
        elif _AGY_BAD_SHORTHAND_RE.fullmatch(model):
            raise ModelSelectionError('Invalid AGY shorthand; use oc/oa/on/4c/4a/4h/4n/g4c/g4a/g4h/g4n with at most one s or t suffix')
    model = 'gemini-pro-orchestrator' if model is None else model
    if agy_approval not in (None, 'skip-permissions'):
        raise ValueError('OAR AGY backend currently supports skip-permissions only')
    fields = backend_for('agy').prepare_start(model, effort, agy_approval)
    fields.update(backend='oar', agy_swarm='off', acpx_record_id=None)
    if agy_mode == 'single':
        fields['agy_mode'] = 'single'
    return fields


def prepare_start(agent, model=None, effort=None, agy_approval=None, *, agy_swarm=None, agy_worker_model=None, agy_mode=None, backend=None):
    if backend is not None:
        if backend not in EXPLICIT_BACKENDS:
            raise ValueError('backend must be acpx or oar')
        if backend == 'oar':
            if agent != 'agy':
                raise ValueError('OAR backend is supported only for AGY')
            return _prepare_oar_agy(model, effort, agy_approval, agy_swarm, agy_worker_model, agy_mode)
        if agent == 'agy':
            if agy_mode in {'swarm', 'teamwork', 'dynamic'} or agy_swarm == 'native':
                raise ModelSelectionError('backend=acpx conflicts with native AGY swarm/teamwork/dynamic mode')
            if agy_worker_model is not None:
                raise ValueError('backend=acpx does not support agy_worker_model')
            if agy_swarm is None and agy_mode is None:
                agy_swarm = 'off'
    if agy_mode is not None:
        if agent != 'agy' or agy_mode not in {'single', 'swarm', 'teamwork', 'dynamic'}:
            raise ModelSelectionError('Unsupported explicit AGY mode')
        route = 'off' if agy_mode == 'single' else 'native'
        if agy_swarm is not None and agy_swarm != route:
            raise ModelSelectionError('agy_mode conflicts with agy_swarm')
        if agy_mode == 'dynamic' and agy_worker_model is not None:
            raise ModelSelectionError('Dynamic child tiers belong to the approved script, not agy_worker_model')
        if agy_mode == 'dynamic' and isinstance(model, str) and re.fullmatch(r'(?:o[can]|g?4[cahn])[st]?', model):
            raise ModelSelectionError('Dynamic requires an advertised model ID, not a mode shorthand')
        agy_swarm = route
    if agent != 'agy':
        if agy_swarm is not None or agy_worker_model is not None:
            raise ValueError('agy_swarm/agy_worker_model are supported only for AGY')
        fields = backend_for(agent).prepare_start(model, effort, agy_approval)
        if backend is not None:
            fields['backend'] = backend
        return fields
    if isinstance(model, str):
        suffix = model[-1:] if model[-1:] in {'s','t'} else ''
        base = model[:-1] if suffix else model
        if base in _AGY_ALIASES:
            intent = 'teamwork' if suffix == 't' else 'swarm' if suffix == 's' else 'single'
            if agy_mode is not None and agy_mode != intent:
                raise ModelSelectionError('model shorthand conflicts with agy_mode')
            if suffix == 't': agy_mode = 'teamwork'
            shorthand_mode = 'native' if suffix else 'off'
            if agy_swarm is not None and agy_swarm != shorthand_mode:
                raise ModelSelectionError('model shorthand conflicts with agy_swarm; bare requires off, s requires native')
            agy_swarm = shorthand_mode
            model = _AGY_ALIASES[base]
        elif _AGY_BAD_SHORTHAND_RE.fullmatch(model):
            raise ModelSelectionError('Invalid AGY shorthand; use oc/oa/on/4c/4a/4h/4n/g4c/g4a/g4h/g4n with at most one s or t suffix')
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
    if agy_mode is not None or mode == 'native':
        fields['agy_mode'] = agy_mode or 'swarm'
    if backend is not None:
        fields['backend'] = backend
    return fields
