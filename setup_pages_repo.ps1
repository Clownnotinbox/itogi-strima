param(
  [Parameter(Mandatory=$true)]
  [string]$RepoUrl,
  [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (!(Test-Path ".git")) {
  git init -b $Branch
}

$remote = git remote get-url origin 2>$null
if ($LASTEXITCODE -ne 0) {
  git remote add origin $RepoUrl
} elseif ($remote -ne $RepoUrl) {
  git remote set-url origin $RepoUrl
}

git add .
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
  git commit -m "Initial project setup"
}
git push -u origin $Branch

Write-Host "Done. In GitHub, enable Pages from branch '$Branch' / root if it is not enabled yet."
