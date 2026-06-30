#Requires -Version 5.1
<#
.SYNOPSIS
  Praxis one-command installer + configurator (Windows / PowerShell).

.DESCRIPTION
  Mirrors install.sh for Windows. Finds Python (>=3.10), creates a .venv,
  installs Praxis (core is dependency-free), smoke-tests it, and runs the
  onboarding wizard. Run from a clone or piped from the web.

.EXAMPLE
  # From the web (PowerShell):
  irm https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.ps1 | iex

.EXAMPLE
  # From a clone:
  .\install.ps1                          # core install + interactive onboarding
  .\install.ps1 -With docs               # also install the document-parser extra
  .\install.ps1 -With "docs,fast"        # multiple extras
  .\install.ps1 -NoConfigure             # install only; skip onboarding
  .\install.ps1 -Provider ollama -Model llama3.1   # non-interactive configure
#>
[CmdletBinding()]
param(
    [string]$With = "",
    [string]$Venv = ".venv",
    [switch]$NoConfigure,
    [string]$Provider = "",
    [string]$Model = "",
    [switch]$Editable
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/smfworks/smf-praxis.git"

function Say($msg)  { Write-Host "[praxis] $msg" -ForegroundColor Cyan }
function Die($msg)  { Write-Host "[praxis] $msg" -ForegroundColor Red; exit 1 }

# 1. Locate a usable Python (>= 3.10). Try the py launcher, then python/python3.
# Each candidate is an argv array so the 'py -3' launcher works as one unit.
$PyBin = $null
$candidates = @(
    ,@("py", "-3")
) + @(
    ,@("python")
) + @(
    ,@("python3")
)
foreach ($cand in $candidates) {
    $exe = $cand[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $checkArgs = @($cand[1..($cand.Count - 1)]) + @(
            "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)")
        try {
            & $exe @checkArgs 2>$null
            if ($LASTEXITCODE -eq 0) { $PyBin = $cand; break }
        } catch { }
    }
}
if (-not $PyBin) { Die "Python >= 3.10 not found. Install it from https://python.org and re-run." }
# Helper to invoke the resolved python with extra args.
function Invoke-Py {
    param([Parameter(ValueFromRemainingArguments=$true)]$RemArgs)
    $base = @($PyBin[1..($PyBin.Count - 1)])
    & $PyBin[0] @base @RemArgs
}
$pyVersion = (Invoke-Py --version 2>&1 | Out-String).Trim()
Say "using $pyVersion"

# 2. Resolve the project root, cloning if run standalone.
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if (Test-Path (Join-Path $ScriptDir "pyproject.toml")) {
    $ProjectDir = $ScriptDir
} elseif (Test-Path ".\pyproject.toml") {
    $ProjectDir = (Get-Location).Path
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Die "git is required to fetch Praxis. Install Git for Windows and re-run."
    }
    $ProjectDir = Join-Path (Get-Location).Path "smf-praxis"
    if (Test-Path (Join-Path $ProjectDir ".git")) {
        Say "updating existing clone in $ProjectDir"
        git -C $ProjectDir pull --ff-only
    } else {
        Say "cloning $RepoUrl"
        git clone --depth 1 $RepoUrl $ProjectDir
    }
}
Say "project: $ProjectDir"
Set-Location $ProjectDir

# 3. Create the virtual environment.
if (-not (Test-Path $Venv)) {
    Say "creating virtualenv: $Venv"
    Invoke-Py -m venv $Venv
}
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { $VenvPy = Join-Path $Venv "bin/python" }  # cross-shell
if (-not (Test-Path $VenvPy)) { Die "virtualenv python not found under $Venv" }

# 4. Install Praxis (core dependency-free; extras opt-in).
Say "upgrading pip"
& $VenvPy -m pip install --quiet --upgrade pip
$Target = "."
if ($With) { $Target = ".[$With]" }
$editFlag = @()
if ($Editable) { $editFlag = @("-e") }
Say "installing praxis $Target"
& $VenvPy -m pip install @editFlag $Target

# 5. Smoke-test, then configure.
Say "verifying install"
& $VenvPy -m hybridagent.cli demo | Out-Null
if ($LASTEXITCODE -eq 0) { Say "demo OK" } else { Die "smoke test failed" }

if (-not $NoConfigure) {
    if ($Provider -and $Model) {
        Say "configuring (non-interactive): $Provider/$Model"
        & $VenvPy -m hybridagent.cli onboard --provider $Provider --model $Model
    } else {
        Say "starting onboarding wizard"
        & $VenvPy -m hybridagent.cli onboard
    }
}

$activate = Join-Path $ProjectDir (Join-Path $Venv "Scripts\Activate.ps1")
Write-Host ""
Write-Host "[praxis] ready." -ForegroundColor Green
Write-Host ""
Write-Host "  Activate the environment:   $activate"
Write-Host "  Try the demo:               praxis demo"
Write-Host "  Configure a model:          praxis onboard"
Write-Host "  Run a task:                 praxis handle `"Prepare a customer follow-up email`""
Write-Host ""
Write-Host "Offline by default (deterministic mock LLM); point at a real provider"
Write-Host "with 'praxis onboard'. Docs: $ProjectDir\README.md"
