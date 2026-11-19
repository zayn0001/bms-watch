# Runs one BMS Watch check (python run.py --cli) against Supabase and appends the output to
# ../scheduled_check.log (last ~3000 lines kept). Windows Task Scheduler runs this every 5 minutes
# as the task "BMS Watch check". Needs SUPABASE_SECRET_KEY and SMTP_* in ../.env.
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$log = Join-Path $root 'scheduled_check.log'
# cmd does the 2>&1 so PowerShell 5.1 doesn't wrap Python's log lines (stderr) as errors
$out = cmd /c "python run.py --cli 2>&1" | Out-String
Add-Content -Path $log -Value "---- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$LASTEXITCODE`r`n$($out.TrimEnd())"
$lines = Get-Content $log
if ($lines.Count -gt 3000) { $lines | Select-Object -Last 3000 | Set-Content $log }
