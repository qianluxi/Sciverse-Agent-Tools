$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$dist = Join-Path $root "dist\SciverseResearchWorkbench.exe"
$build = Join-Path $root "build\SciverseResearchWorkbench"
$spec = Join-Path $root "SciverseResearchWorkbench.spec"

Write-Host "Installing PyInstaller..."
& $python -m pip install "pyinstaller>=6.0"

$hiddenImports = @(
    "research_app.ai",
    "research_app.ai_review",
    "research_app.ai_screening",
    "research_app.ai_settings",
    "research_app.ai_translation",
    "research_app.errors",
    "research_app.evidence",
    "research_app.field_scan",
    "research_app.project_ui",
    "research_app.exports",
    "research_app.graph",
    "research_app.screening",
    "research_app.screening_eval",
    "research_app.search",
    "research_app.service",
    "research_app.storage",
    "research_app.trends",
    "matplotlib",
    "matplotlib.backends.backend_agg",
    "pandas",
    "pyarrow",
    "sciverse"
)

Write-Host "Building portable executable..."
$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name", "SciverseResearchWorkbench",
    "--add-data", "research_app;research_app",
    "--paths", ".",
    "--collect-all", "streamlit",
    "--collect-all", "altair",
    "--collect-all", "matplotlib",
    "--collect-submodules", "sciverse",
    "--copy-metadata", "streamlit",
    "--copy-metadata", "sciverse"
)
foreach ($module in $hiddenImports) {
    $arguments += @("--hidden-import", $module)
}
$arguments += "research_app/desktop_launcher.py"

& $python @arguments

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $dist)) {
    throw "Build finished without producing $dist"
}
Write-Host "Built: $dist"
Get-Item -LiteralPath $dist | Select-Object FullName, Length, LastWriteTime
