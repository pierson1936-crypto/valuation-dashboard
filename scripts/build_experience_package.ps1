[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [string]$PackageName
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Join-CodePoints {
    param([int[]]$CodePoints)
    return -join @($CodePoints | ForEach-Object { [char]$_ })
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot 'dist'
}
if (-not $PackageName) {
    $packagePrefix = (Join-CodePoints @(0x4F30, 0x503C, 0x5206, 0x6790, 0x53F0)) + '-'
    $packagePrefix += (Join-CodePoints @(0x4F53, 0x9A8C, 0x5305)) + '-'
    $PackageName = $packagePrefix + (Get-Date -Format 'yyyyMMdd')
}

$launch = (Join-CodePoints @(0x542F, 0x52A8)) + '.bat'
$launchAi = (Join-CodePoints @(0x542F, 0x52A8)) + 'AI' + (Join-CodePoints @(0x52A9, 0x624B)) + '.bat'
$launchMonitor = (Join-CodePoints @(0x542F, 0x52A8, 0x76EF, 0x76D8)) + '.bat'
$quickStart = (Join-CodePoints @(0x5FEB, 0x901F, 0x5F00, 0x59CB)) + '.txt'

$runtimeFiles = @(
    'app.py',
    'agent.py',
    'monitor.py',
    'requirements.txt',
    'README.md',
    $quickStart,
    $launch,
    $launchAi,
    $launchMonitor,
    'monitoring\__init__.py',
    'monitoring\assistant.py',
    'monitoring\cli.py',
    'monitoring\config.py',
    'monitoring\data.py',
    'monitoring\db.py',
    'monitoring\explanations.py',
    'monitoring\holding_ocr.py',
    'monitoring\notifications.py',
    'monitoring\portfolio.py',
    'monitoring\portfolio_analysis.py',
    'monitoring\presets.py',
    'monitoring\rules.py',
    'monitoring\service.py',
    'monitoring\trading_calendar.py',
    'monitoring\web.py',
    'vendor\AKSHARE_LICENSE',
    'vendor\akshare_ths.js',
    'vendor\README.md',
    'data\industry_boards.json'
)

$missingFiles = @($runtimeFiles | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $repoRoot $_) -PathType Leaf)
})
if ($missingFiles.Count -gt 0) {
    throw "Missing package files: $($missingFiles -join ', ')"
}

$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$zipPath = Join-Path $outputRoot ($PackageName + '.zip')

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('valuation-experience-' + [guid]::NewGuid().ToString('N'))
$packageRoot = Join-Path $tempRoot $PackageName
[System.IO.Directory]::CreateDirectory($packageRoot) | Out-Null

try {
    foreach ($relativePath in $runtimeFiles) {
        $source = Join-Path $repoRoot $relativePath
        $destination = Join-Path $packageRoot $relativePath
        $destinationDirectory = Split-Path -Parent $destination
        [System.IO.Directory]::CreateDirectory($destinationDirectory) | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }

    [System.IO.Directory]::CreateDirectory((Join-Path $packageRoot 'data')) | Out-Null

    $sourceCommit = 'unknown'
    try {
        $commitOutput = & git -C $repoRoot rev-parse --short HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $commitOutput) {
            $sourceCommit = $commitOutput.Trim()
        }
    } catch {
        # Git is optional and is only used to record build provenance.
    }
    $versionText = @(
        'Valuation Dashboard Experience Package',
        "Built at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "Source commit: $sourceCommit"
    ) -join [Environment]::NewLine
    [System.IO.File]::WriteAllText(
        (Join-Path $packageRoot 'VERSION.txt'),
        $versionText,
        [System.Text.UTF8Encoding]::new($true)
    )

    $forbiddenDirectoryNames = @('.git', '.codex', '.agents', 'tests', 'docs', '__pycache__')
    $forbiddenFilePatterns = @(
        '^\.env($|\.)',
        '\.(db|sqlite|log|xlsx|pem|key|p12|pfx)$',
        '\.(db|sqlite)-(shm|wal)$',
        '^credentials.*\.json$',
        '^secrets.*\.json$',
        '^industry_flow_snapshot\.json$'
    )

    $forbiddenEntries = @(
        Get-ChildItem -LiteralPath $packageRoot -Recurse -Force | Where-Object {
            if ($_.PSIsContainer) {
                return $forbiddenDirectoryNames -contains $_.Name
            }
            foreach ($pattern in $forbiddenFilePatterns) {
                if ($_.Name -match $pattern) {
                    return $true
                }
            }
            return $false
        }
    )
    if ($forbiddenEntries.Count -gt 0) {
        throw "Forbidden package entries: $($forbiddenEntries.FullName -join ', ')"
    }

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal

    $archive = Get-Item -LiteralPath $zipPath
    Write-Output ('Package created: {0}' -f $archive.FullName)
    Write-Output ('Size: {0:N2} MB' -f ($archive.Length / 1MB))
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
