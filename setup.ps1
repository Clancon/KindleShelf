param([switch]$Force)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

$PythonExecutable = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw "Python 3 was not found. Install Python 3.10 or newer first."
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the Python virtual environment failed."
    }
}

& $PythonExecutable -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 or newer is required."
}

$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$RequirementsHash = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash
$HashPath = Join-Path $ProjectRoot ".venv\dependencies.sha256"
$InstalledHash = if (Test-Path -LiteralPath $HashPath) {
    (Get-Content -LiteralPath $HashPath -Raw).Trim()
} else { "" }

if ($Force -or $InstalledHash -ne $RequirementsHash) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv pip install --python $PythonExecutable --require-hashes -r $RequirementsPath
    } else {
        & $PythonExecutable -m pip install --require-hashes -r $RequirementsPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Installing the locked Python dependencies failed."
    }
    Set-Content -LiteralPath $HashPath -Value $RequirementsHash -Encoding ascii
}

Write-Host "Kindle Shelf environment is ready."
