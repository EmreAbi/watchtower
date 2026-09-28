param(
    [string]$OutputDir,
    [string]$BinaryPath,
    [string]$ConPtyPackage,
    [switch]$RequireCleanSource
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
    if ($RequireCleanSource) {
        $sourceChanges = & git status --porcelain --untracked-files=normal
        if ($LASTEXITCODE -ne 0) { throw 'Could not verify the source checkout.' }
        if ($sourceChanges) { throw 'Release packaging requires a clean source checkout.' }
    }
    if (-not $BinaryPath) {
        $buildEnvironmentJson = & python -B (Join-Path $PSScriptRoot 'watchtower_release.py') --root $taskRoot
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare release build flags.' }
        $buildEnvironment = $buildEnvironmentJson | ConvertFrom-Json
        $previousEnvironment = @{}
        try {
            foreach ($entry in $buildEnvironment.PSObject.Properties) {
                $previousEnvironment[$entry.Name] = [Environment]::GetEnvironmentVariable($entry.Name, 'Process')
                [Environment]::SetEnvironmentVariable($entry.Name, $entry.Value, 'Process')
            }
            Invoke-Checked cargo @('build', '--release', '--locked', '--features', 'watchtower', '--target', 'x86_64-pc-windows-msvc')
        } finally {
            foreach ($key in $previousEnvironment.Keys) {
                [Environment]::SetEnvironmentVariable($key, $previousEnvironment[$key], 'Process')
            }
        }
        $targetRoot = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $taskRoot 'target' }
        $BinaryPath = Join-Path $targetRoot 'x86_64-pc-windows-msvc/release/herdr.exe'
    } else {
        Write-Warning 'Using an existing binary: build path remapping cannot be applied retroactively. Verify its release provenance and privacy scan.'
    }
    $BinaryPath = (Resolve-Path -LiteralPath $BinaryPath).Path
    $versionOutput = & $BinaryPath --version
    if ($LASTEXITCODE -ne 0 -or ($versionOutput -join "`n") -notmatch ('(?i)^watchtower\s+' + [regex]::Escape($product.version) + '(?:\s|$)')) {
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
        $packageArguments = @('-B', (Join-Path $PSScriptRoot 'watchtower_package.py'), '--verified-stage', $stage, '--output', $archive)
        if ($RequireCleanSource) { $packageArguments += '--require-clean-source' }
        Invoke-Checked python $packageArguments
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
