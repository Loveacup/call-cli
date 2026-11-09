"""Execution-device commands and the shared public collaboration prompt."""
import base64
import re
import shlex
import subprocess

def command(device, shell, argv):
    if device == 'local':
        return argv
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',device):
        raise ValueError('Use an existing SSH host alias, not shell text')
    if shell == 'powershell':
        literal = lambda v: "'"+str(v).replace("'","''")+"'"
        script = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & "+' '.join(literal(v) for v in argv)+"; exit $LASTEXITCODE"
        remote = 'powershell -NoProfile -EncodedCommand '+base64.b64encode(script.encode('utf-16le')).decode()
    else:
        remote = 'export PATH=/opt/homebrew/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH; exec '+shlex.join(argv)
    return ['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=8',device,remote]


def protocol_prompt(text):
    return ('You are participating in a new CLI collaboration session. Work only within the stated scope; do not commit, push, deploy, or change unrelated files. '
            'Do not use memory for this operational task. For meaningful stages emit a public line CALLCLI_STAGE followed by a short status. '
            'If you need an answer, emit CALLCLI_QUESTION followed by the question on a single line and end this turn. '
            'Do not include private reasoning. End with actual results, verification and remaining issues.\n\n'+text)


def device_identity(device):
    if device=='local':return 'local'
    command(device,'posix',['true'])
    result=subprocess.run(['ssh','-G',device],capture_output=True,text=True,timeout=10)
    if result.returncode:raise ValueError('Could not resolve SSH host configuration')
    fields=dict(line.split(None,1) for line in result.stdout.splitlines() if len(line.split(None,1))==2)
    return 'ssh:'+fields['user']+'@'+fields['hostname'].lower()+':'+fields.get('port','22')
