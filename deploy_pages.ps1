param(
  [switch]$Loop,
  [int]$IntervalSeconds = 7200
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Invoke-Publish {
  $env:KRYL_PAGES_AUTO_PUSH = "1"
  $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
  if (Test-Path $VenvPython) {
    & $VenvPython -c "import json, deployment; print(json.dumps(deployment.publish_and_maybe_push('Update stream summary site'), ensure_ascii=False, indent=2))"
  } else {
    py -3.11 -c "import json, deployment; print(json.dumps(deployment.publish_and_maybe_push('Update stream summary site'), ensure_ascii=False, indent=2))"
  }
}

if ($Loop) {
  while ($true) {
    Invoke-Publish
    Start-Sleep -Seconds $IntervalSeconds
  }
} else {
  Invoke-Publish
}
