[CmdletBinding()]
param(
    [string]$PackageName = 'LiveWatch-Optimized-Windows-x64',
    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($PackageName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]+$') { throw 'Invalid package name.' }
$ffmpegPath = Join-Path $projectRoot 'tools\ffmpeg.exe'
if (-not (Test-Path -LiteralPath $ffmpegPath -PathType Leaf) -or (Get-Item -LiteralPath $ffmpegPath).Length -le 1000000) {
    throw 'A complete tools\ffmpeg.exe is required for the delivery package.'
}
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build-desktop.ps1') }
$stageParent = Join-Path $projectRoot 'build\portable'
$stage = [IO.Path]::GetFullPath((Join-Path $stageParent $PackageName))
if (-not $stage.StartsWith([IO.Path]::GetFullPath($stageParent) + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe staging path.' }
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$exeName = (-join @([char]0x76F4, [char]0x64AD, [char]0x5B88, [char]0x671B)) + '.exe'
foreach ($name in @($exeName, "$exeName.config", 'Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "build\desktop\$name") -Destination $stage
}
foreach ($name in @('dependency-manifest.json', 'desktop-dependency-manifest.json')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stage
}
# FFmpeg ships ready to run; only Python and WebView2 need first-launch downloads.
$packagedTools = Join-Path $stage 'tools'
New-Item -ItemType Directory -Path $packagedTools -Force | Out-Null
Copy-Item -LiteralPath $ffmpegPath -Destination (Join-Path $packagedTools 'ffmpeg.exe')
foreach ($folder in @('app', 'licenses')) {
    $sourceRoot = Join-Path $projectRoot $folder
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File) {
        if ($file.FullName -match '[\\/](__pycache__|data|logs|video|time_begin_over)[\\/]' -or $file.Extension -in @('.pyc', '.pyo')) { continue }
        $relative = $file.FullName.Substring($projectRoot.Length + 1)
        $destination = Join-Path $stage $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
}
# Copy authored documents, never user databases, room lists, credentials or recordings.
foreach ($name in @('README.md', 'portable-delivery.md', 'start.cmd')) {
    if (Test-Path -LiteralPath (Join-Path $projectRoot $name)) { Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stage }
}
# Select the Chinese-language delivery guides by Unicode code points so Windows
# PowerShell 5.1 can continue to read this script as plain ASCII.
$documentNames = @(
    @(0x5148,0x8BFB,0x6211),
    @(0x4F7F,0x7528,0x8BF4,0x660E),
    @(0x4F18,0x5316,0x4EA4,0x4ED8,0x4E0E,0x9A8C,0x8BC1),
    @(0x7B2C,0x4E09,0x65B9,0x7EC4,0x4EF6,0x8BF4,0x660E),
    @(0x624B,0x673A,0x5206,0x4EAB,0x94FE,0x63A5,0x89E3,0x6790),
    @(0x5F02,0x5E38,0x6062,0x590D,0x4E0E,0x5A92,0x4F53,0x8BBE,0x7F6E),
    @(0x68C0,0x6D4B,0x8D85,0x65F6,0x6062,0x590D,0x4E0E,0x4EA4,0x4ED8)
)
for ($index = 0; $index -lt $documentNames.Count; $index++) {
    $name = -join @($documentNames[$index] | ForEach-Object { [char]$_ })
    if ($index -eq 0) { $name += '.txt' } else { $name += '.md' }
    if (Test-Path -LiteralPath (Join-Path $projectRoot $name)) { Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stage }
}
$inventory = @(Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{ path = $_.FullName.Substring($stage.Length + 1).Replace('\','/'); size = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
})
[IO.File]::WriteAllText((Join-Path $stage 'package-manifest.json'), ($inventory | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
$artifactRoot = Join-Path $projectRoot 'artifacts'
New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
$zipPath = Join-Path $artifactRoot ($PackageName + '.zip')
$temporary = $zipPath + '.partial'
if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stream = [IO.File]::Create($temporary)
try {
    $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create, $true)
    try {
        foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName) {
            $relative = $file.FullName.Substring($stage.Length + 1).Replace('\','/')
            $entry = $zip.CreateEntry("$PackageName/$relative", [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = [DateTimeOffset]::new(2000, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
            $inputStream = $file.OpenRead()
            $entryStream = $entry.Open()
            try { $inputStream.CopyTo($entryStream) } finally { $entryStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
} finally { $stream.Dispose() }
Move-Item -LiteralPath $temporary -Destination $zipPath -Force
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($zipPath + '.sha256'), "$hash  $PackageName.zip`n", [Text.Encoding]::ASCII)
Write-Host "Package: $zipPath"
Write-Host "Size: $((Get-Item -LiteralPath $zipPath).Length) bytes"
Write-Host "SHA256: $hash"
