<#
.SYNOPSIS
  Praxis one-command installer + configurator (Windows / PowerShell).

.DESCRIPTION
  Finds Python (>= 3.10) -> creates a .venv -> installs Praxis -> runs the
  onboarding wizard. The core install is dependency-free; extras are opt-in.

.EXAMPLE
  .\install.ps1
  .\install.ps1 -With docs,multimodal
  .\install.ps1 -NoConfigure
  .\install.ps1 -Provider ollama -Model llama3.1   # non-interactive configure

  From scratch (single command):
    irm https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.ps1 | iex
#>
[CmdletBinding()]
param(
  [string[]] $With,
  [string]   $Venv = ".venv",
  [switch]   $NoConfigure,
  [string]   $Provider,
  [string]   $Model,
  [switch]   $Editable
)
$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/smfworks/smf-praxis.git"

function Say  { param($m) Write-Host "[praxis] $m" -ForegroundColor Cyan }
function Die  { param($m) Write-Host "[praxis] $m" -ForegroundColor Red; exit 1 }

# 1. Locate a usable Python (>= 3.10).
$pybin = $null
foreach ($cand in @("python", "py", "python3")) {
  $cmd = Get-Command $cand -ErrorAction SilentlyContinue
  if ($cmd) {
    $ok = & $cand -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $pybin = $cand; break }
  }
}
if (-not $pybin) { Die "Python >= 3.10 not found. Install it (https://python.org) and re-run." }
Say "using $(& $pybin --version 2>&1)"

# 2. Resolve the project root (run from a clone, or clone if piped in).
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if (Test-Path (Join-Path $scriptDir "pyproject.toml")) {
  $projectDir = $scriptDir
} elseif (Test-Path ".\pyproject.toml") {
  $projectDir = (Get-Location).Path
} else {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is required to fetch Praxis." }
  $projectDir = Join-Path (Get-Location).Path "smf-praxis"
  if (Test-Path (Join-Path $projectDir ".git")) {
    Say "updating existing clone in $projectDir"; git -C $projectDir pull --ff-only
  } else {
    Say "cloning $RepoUrl"; git clone --depth 1 $RepoUrl $projectDir
  }
}
Say "project: $projectDir"
Set-Location $projectDir

# 3. Create the virtual environment.
if (-not (Test-Path $Venv)) { Say "creating virtualenv: $Venv"; & $pybin -m venv $Venv }
$venvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { $venvPy = Join-Path $Venv "bin/python" }  # cross-shell

# 4. Install Praxis.
Say "upgrading pip"
& $venvPy -m pip install --quiet --upgrade pip
$target = "."
if ($With) { $target = ".[$($With -join ',')]" }
$ed = @(); if ($Editable) { $ed = @("-e") }
Say "installing praxis $(if($Editable){'(editable) '})$target"
& $venvPy -m pip install @ed $target

# 5. Smoke-test, then configure.
Say "verifying install"
& $venvPy -m hybridagent.cli demo *> $null
if ($LASTEXITCODE -eq 0) { Say "demo OK" } else { Die "demo smoke test failed" }

if (-not $NoConfigure) {
  if ($Provider -and $Model) {
    Say "configuring (non-interactive): $Provider/$Model"
    & $venvPy -m hybridagent.cli onboard --provider $Provider --model $Model
  } else {
    Say "starting onboarding wizard"
    & $venvPy -m hybridagent.cli onboard
  }
}

Write-Host ""
Write-Host "[praxis] ready." -ForegroundColor Green
@"
  Activate the environment:   $projectDir\$Venv\Scripts\Activate.ps1
  Try the demo:               praxis demo
  Configure a model:          praxis onboard
  Run a task:                 praxis handle "Prepare a customer follow-up email"

Offline by default (deterministic mock LLM); point at a real provider with
'praxis onboard'. Docs: $projectDir\README.md
"@ | Write-Host
