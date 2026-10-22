# One-shot native PowerShell SSH client. No credentials, daemon, or business replay.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$replyText=$null;$replyCode=1;$proc=$null;$started=$false;$cleanupFailed=$false
function Quote-Posix([string]$value){
 if($null -eq $value -or $value.IndexOf([char]0) -ge 0){throw 'invalid argument'}
 $sq=[string][char]39;$dq=[string][char]34
 return $sq+$value.Replace($sq,($sq+$dq+$sq+$dq+$sq))+$sq
}
try {
 $opts=@{'--python'='python3';'--timeout-ms'='45000'};$i=0
 $allowed=@('--host','--entry','--db','--python','--timeout-ms','--ssh-bin')
 while($i -lt $args.Count -and $args[$i] -ne '--'){
  $key=[string]$args[$i];if($key -notin $allowed -or ($i+1) -ge $args.Count){throw 'invalid options'}
  $opts[$key]=[string]$args[$i+1];$i+=2
 }
 if($i -ge $args.Count -or ($i+1) -ge $args.Count){throw 'action required'}
 if($opts['--host'] -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$' -or -not $opts['--entry'].StartsWith('/') -or -not $opts['--db'].StartsWith('/')){throw 'invalid endpoint'}
 $limit=0;if(-not[int]::TryParse($opts['--timeout-ms'],[ref]$limit) -or $limit -le 0 -or $limit -gt 120000){throw 'invalid timeout'}
 $forward=@($args[($i+1)..($args.Count-1)])
 $remoteArgs=@($opts['--python'],$opts['--entry'],'--db',$opts['--db'])+$forward
 $remote='export PATH=/opt/homebrew/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH; exec '+(($remoteArgs|ForEach-Object {Quote-Posix ([string]$_)}) -join ' ')
 # Keep all user argument bytes out of the Windows native command-line quoting layer.
 $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
 $stage='printf %s '+$encoded+' | /usr/bin/base64 -D | /bin/sh'
 $ssh=$opts['--ssh-bin'];if(-not$ssh){$ssh=Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'}
 if(-not[IO.Path]::IsPathRooted($ssh) -or -not(Test-Path -LiteralPath $ssh -PathType Leaf)){throw 'invalid ssh executable'}
 $pi=New-Object Diagnostics.ProcessStartInfo;$pi.FileName=$ssh
 $pi.Arguments='-n -T -o BatchMode=yes -o ConnectTimeout=8 '+$opts['--host']+' "'+$stage+'"'
 $pi.UseShellExecute=$false;$pi.CreateNoWindow=$true;$pi.RedirectStandardInput=$true;$pi.RedirectStandardOutput=$true;$pi.RedirectStandardError=$true
 $pi.StandardOutputEncoding=[Text.UTF8Encoding]::new($false);$pi.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
 $proc=New-Object Diagnostics.Process;$proc.StartInfo=$pi
 $clock=[Diagnostics.Stopwatch]::StartNew();$started=$proc.Start();if(-not$started){throw 'spawn failed'};$proc.StandardInput.Close()
 $outBuffer=New-Object char[] 4096;$errBuffer=New-Object char[] 4096
 $outRead=$proc.StandardOutput.ReadAsync($outBuffer,0,$outBuffer.Length);$errRead=$proc.StandardError.ReadAsync($errBuffer,0,$errBuffer.Length)
 $text=New-Object Text.StringBuilder;$eof=$false
 while($clock.ElapsedMilliseconds -lt $limit){
  if($null -ne $errRead -and $errRead.IsCompleted){$n=$errRead.Result;if($n -eq 0){$errRead=$null}else{$errRead=$proc.StandardError.ReadAsync($errBuffer,0,$errBuffer.Length)}}
  if($null -ne $outRead -and $outRead.IsCompleted){
   $n=$outRead.Result
   if($n -eq 0){$eof=$true;$outRead=$null}else{
    $null=$text.Append($outBuffer,0,$n);if($text.Length -gt 4194304){throw 'output limit'}
    if(-not [string]::IsNullOrWhiteSpace($text.ToString())){
     try{$candidate=ConvertFrom-Json -InputObject $text.ToString();$replyText=$text.ToString();$replyCode=0;if($null -ne $candidate -and $candidate.PSObject.Properties['error'] -and $candidate.error){$replyCode=1};break}catch{}
    }
    $outRead=$proc.StandardOutput.ReadAsync($outBuffer,0,$outBuffer.Length)
   }
  }
  if($eof -and $proc.HasExited){break}
  [Threading.Thread]::Sleep(10)
 }
}catch{}
finally {
 if($started){try{if(-not$proc.HasExited){$proc.Kill();if(-not$proc.WaitForExit(1500)){$cleanupFailed=$true}}}catch{$cleanupFailed=$true}}
 if($null -ne $proc){$proc.Dispose()}
}
if($cleanupFailed){[Console]::Out.WriteLine('{"error":"transport_cleanup_unknown","do_not_replay":true}');exit 1}
if($null -eq $replyText){[Console]::Out.WriteLine('{"error":"response_unknown","do_not_replay":true}');exit 1}
# A complete confirmed CLI reply wins over later transport-exit uncertainty; preserve bytes/text.
[Console]::Out.WriteLine($replyText)
exit $replyCode
