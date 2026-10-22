param([string]$ClientPath,[string]$WorkDir)
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Force $WorkDir|Out-Null
$env:TEMP=$WorkDir;$env:TMP=$WorkDir
$fake=Join-Path $WorkDir 'fake-ssh-whitespace.exe'
if(-not(Test-Path $fake)){
Add-Type -OutputAssembly $fake -OutputType ConsoleApplication -TypeDefinition @'
using System;using System.IO;using System.Text;using System.Threading;
public class FakeSsh {
 public static void Main(string[] a){Console.OutputEncoding=new UTF8Encoding(false);string trace=Environment.GetEnvironmentVariable("CLI_TRACE");File.AppendAllText(trace,Convert.ToBase64String(Encoding.UTF8.GetBytes(String.Join("\0",a)))+"\n");string m=Environment.GetEnvironmentVariable("CLI_CASE");if(m.StartsWith("whitespace")){Console.Write(" " + (char)13 + (char)10);Console.Out.Flush();if(m=="whitespace-only"){Thread.Sleep(10000);return;}Thread.Sleep(350);}if(m=="partial"){Console.Write("{");Console.Out.Flush();Thread.Sleep(10000);return;}if(m=="error"){Console.Write("{\"error\":\"fake_failure\",\"do_not_replay\":true}");return;}Console.Write("{\"ok\":true,\"text\":\"\u4e2d\u6587\"}");Console.Out.Flush();Thread.Sleep(8000);}
}
'@
}
$cases=@()
function Invoke-Case([string]$mode,[string[]]$tail){
 $trace=Join-Path $WorkDir ($mode+'-'+[Guid]::NewGuid().ToString('N')+'.trace');$env:CLI_TRACE=$trace;$env:CLI_CASE=$mode
 $all=@('--host','existing-host','--entry','/workspace/collab.py','--db','/workspace/db.sqlite','--ssh-bin',$fake,'--timeout-ms','1200','--')+$tail
 $data=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -Compress -InputObject $all)))
 $pathData=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ClientPath))
 $script='$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('''+$pathData+'''));$a=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('''+$data+'''))|ConvertFrom-Json;& $p @a;exit $LASTEXITCODE'
 $b64=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
 $pi=New-Object Diagnostics.ProcessStartInfo;$pi.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe';$pi.Arguments='-NoProfile -EncodedCommand '+$b64;$pi.UseShellExecute=$false;$pi.RedirectStandardOutput=$true;$pi.RedirectStandardError=$true;$pi.StandardOutputEncoding=[Text.Encoding]::UTF8
 $p=New-Object Diagnostics.Process;$p.StartInfo=$pi;$clock=[Diagnostics.Stopwatch]::StartNew();$null=$p.Start();$o=$p.StandardOutput.ReadToEndAsync();$e=$p.StandardError.ReadToEndAsync();if(-not$p.WaitForExit(6000)){$p.Kill();throw 'Client exceeded fixture bound'}
 $out=$o.Result;$err=$e.Result;$reply=$null;try{$reply=ConvertFrom-Json -InputObject $out}catch{}
 $lines=@();if(Test-Path $trace){$lines=@([IO.File]::ReadAllLines($trace))}
 return @{mode=$mode;exit=$p.ExitCode;reply=$reply;elapsed=$clock.ElapsedMilliseconds;calls=$lines.Count;trace=$lines;stderr=$err}
}
$r=Invoke-Case 'success' @('connect','--runtime','codex');if($r.exit -ne 0 -or -not$r.reply.ok){throw ('success boundary failed: '+($r|ConvertTo-Json -Compress -Depth 5))};if($r.elapsed -gt 4000){throw 'Did not return after complete JSON before fake transport exit'};$cases+=$r
$r=Invoke-Case 'error' @('list');if($r.exit -ne 1 -or -not$r.reply.do_not_replay){throw 'Error reply contract failed'};$cases+=$r
$r=Invoke-Case 'partial' @('list');if($r.exit -ne 1 -or -not$r.reply.do_not_replay -or $r.calls -ne 1){throw 'Unknown effect/no replay failed'};$cases+=$r
$r=Invoke-Case 'success' @('start','--prompt',("a'b "+[char]0x4e2d+[char]0x6587+' $(not-executed)'));if($r.exit -ne 0){throw 'Quoted argument case failed'};$native=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.trace[0]));$last=($native -split [char]0)[-1];if($last -notmatch 'printf %s ([A-Za-z0-9+/=]+)') {throw 'Encoded remote command missing'};$body=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($matches[1]));if(-not$body.Contains('$(not-executed)')){throw 'Argument bytes lost'};$cases+=$r
$failures=@()
$r=Invoke-Case 'whitespace-json' @('list');$cases+=$r;if($r.exit -ne 0 -or -not$r.reply.ok -or $r.calls -ne 1){$failures+='whitespace prefix must wait for complete JSON'}
$r=Invoke-Case 'whitespace-only' @('list');$cases+=$r;if($r.exit -ne 1 -or -not$r.reply.do_not_replay -or $r.calls -ne 1){$failures+='whitespace-only must be unknown, never confirmed success'}
@{fake_tests=$cases.Count;cases=$cases;pass=($failures.Count -eq 0);failures=$failures}|ConvertTo-Json -Depth 8 -Compress
if($failures.Count -gt 0){exit 1}
