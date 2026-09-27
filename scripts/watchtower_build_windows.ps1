param(
    [string]$OutputDir,
    [string]$BinaryPath,
    [string]$ConPtyPackage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$product = Get-Content -LiteralPath (Join-Path $taskRoot 'distribution/watchtower/product.json') -Raw | ConvertFrom-Json

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

if (-not $OutputDir) {
    $OutputDir = Join-Path $taskRoot 'target/watchtower-artifacts'
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
if (-not $ConPtyPackage) {
    $ConPtyPackage = Join-Path $OutputDir 'cache/Microsoft.Windows.Console.ConPTY.nupkg'
}
$ConPtyPackage = [System.IO.Path]::GetFullPath($ConPtyPackage)

Push-Location $taskRoot
try {
    if (-not $BinaryPath) {
        Invoke-Checked cargo @('build', '--release', '--locked', '--features', 'watchtower', '--target', 'x86_64-pc-windows-msvc')
        $targetRoot = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $taskRoot 'target' }
        $BinaryPath = Join-Path $targetRoot 'x86_64-pc-windows-msvc/release/herdr.exe'
    }
    $BinaryPath = (Resolve-Path -LiteralPath $BinaryPath).Path
    $versionOutput = & $BinaryPath --version
    if ($LASTEXITCODE -ne 0 -or ($versionOutput -join "`n") -notmatch ('(?i)^watchtower\s+' + [regex]::Escape($product.version))) {
        throw 'The executable is not this Watchtower preview. Build with --features watchtower.'
    }
    $sourceSha = (& git rev-parse --short=12 HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine source commit.' }
    $archive = Join-Path $OutputDir "watchtower-$($product.version)-$sourceSha-windows-x86_64.zip"
    if (Test-Path -LiteralPath $archive) { throw "Artifact already exists: $archive" }

    $temporary = Join-Path $OutputDir ('.package-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    try {
        $stage = Join-Path $temporary 'verified-conpty'
        & (Join-Path $PSScriptRoot 'package_windows_conpty.ps1') -HerdrExe $BinaryPath -PackagePath $ConPtyPackage -StageDir $stage -OutputPath (Join-Path $temporary 'verified-conpty.zip')
        Invoke-Checked python @('-B', (Join-Path $PSScriptRoot 'watchtower_package.py'), '--verified-stage', $stage, '--output', $archive)
    } finally {
        # Only remove the unique staging directory created by this invocation.
        $resolvedTemporary = (Resolve-Path -LiteralPath $temporary).Path
        if ([System.IO.Path]::GetDirectoryName($resolvedTemporary) -ne $OutputDir -or [System.IO.Path]::GetFileName($resolvedTemporary) -notlike '.package-*') {
            throw 'Refusing to clean an unexpected packaging directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
    }
    Write-Output "Portable preview: $archive"
} finally {
    Pop-Location
}
