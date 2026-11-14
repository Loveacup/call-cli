"""Execution-device commands and the shared public collaboration prompt."""
import base64
import re
import shlex
import subprocess

def local_toolchain_env(bin_dir):
    """Explicit host deployment setting; never search another installation."""
    import os
    import shutil
    from pathlib import Path
    if not isinstance(bin_dir, str) or not Path(bin_dir).is_absolute():
        raise ValueError('local_toolchain_bin must be an absolute directory')
    for name in ('node', 'npm', 'acpx'):
        if not shutil.which(name, path=bin_dir):
            raise ValueError(name + '_executable_not_found')
    return dict(os.environ, PATH=bin_dir + os.pathsep + os.environ.get('PATH', ''))


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
            'If you need an answer, emit CALLCLI_QUESTION Q<n> followed by the question on a single line and end this turn. '
            'Use a distinct positive integer label (Q1, Q2, ...) for each independent question within a turn, even with identical wording. '
            'Repeating the same label and exact question within that turn restates it, requiring only one reply; never reuse a label for different text. '
            'Do not include private reasoning. End with actual results, verification and remaining issues.\n\n'+text)


def device_identity(device):
    if device=='local':return 'local'
    command(device,'posix',['true'])
    result=subprocess.run(['ssh','-G',device],capture_output=True,text=True,timeout=10)
    if result.returncode:raise ValueError('Could not resolve SSH host configuration')
    fields=dict(line.split(None,1) for line in result.stdout.splitlines() if len(line.split(None,1))==2)
    return 'ssh:'+fields['user']+'@'+fields['hostname'].lower()+':'+fields.get('port','22')
