"""Synthetic exact-record close/probe fixture; no execution or external IO."""
class CloseBackend:
    def __init__(self, on_close=None, verified=True):
        self.closes = 0
        self.probes = 0
        self.on_close = on_close
        self.verified = verified

    def resolve(self, policy):
        return {'acp':'fixture-acp'}

    def close(self, policy):
        self.closes += 1
        if self.on_close:
            self.on_close(policy)
        return {'verified':True}

    def probe(self, policy):
        self.probes += 1
        return dict(verified=self.verified,binding=dict(acpxRecordId=policy['record'],
            acpxSessionId=policy['acp'],session=policy['session'],agent=policy['agent'],cwd=policy['cwd']),
            record=dict(closed=self.verified,lastRequestId=policy.get('request_id',policy['task']+':'+str(policy['turn']))),
            status='no-session' if self.verified else 'running',source='shared-runtime-exact-record-probe',
            arbitraryDetachedDescendantsProven=False)
