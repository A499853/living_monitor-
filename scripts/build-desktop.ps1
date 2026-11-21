[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$Install
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot 'build\desktop'
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if ($OutputDirectory.TrimEnd('\', '/') -eq $projectRoot.TrimEnd('\', '/')) {
    throw 'Build into a separate directory. Use -Install to replace the project executable with a backup.'
}
$releaseRoot = Join-Path $projectRoot 'release'
if ($OutputDirectory.Equals($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $OutputDirectory.StartsWith($releaseRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The original release directory is read-only for this build script. Choose another output directory.'
}
$desktopRoot = Join-Path $projectRoot 'desktop'
$frameworkRoot = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $frameworkRoot 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    throw '.NET Framework 4.x x64 compiler was not found. Build this Windows desktop host on Windows with .NET Framework 4.8.'
}

# Keep this script ASCII so it also works in Windows PowerShell 5.1.
$exeName = -join @([char]0x76F4, [char]0x64AD, [char]0x5B88, [char]0x671B)
$exeName += '.exe'
$outputExe = Join-Path $OutputDirectory $exeName
$referenceNames = @('System.dll', 'System.Core.dll', 'System.Drawing.dll',
    'System.Windows.Forms.dll', 'System.Net.Http.dll', 'System.Web.Extensions.dll')
$references = @($referenceNames | ForEach-Object { Join-Path $frameworkRoot $_ })
$references += Join-Path $projectRoot 'Microsoft.Web.WebView2.Core.dll'
$references += Join-Path $projectRoot 'Microsoft.Web.WebView2.WinForms.dll'
foreach ($reference in $references) {
    if (-not (Test-Path -LiteralPath $reference -PathType Leaf)) {
        throw "Required assembly is missing: $reference"
    }
}
$sources = @(Get-ChildItem -LiteralPath $desktopRoot -Filter '*.cs' -Recurse -File |
    Where-Object { $_.FullName -notmatch '[\\/](obj|bin)[\\/]' } |
    Sort-Object FullName | ForEach-Object { $_.FullName })
if ($sources.Count -eq 0) { throw "No C# source was found in $desktopRoot" }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$compilerArguments = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+',
    '/debug-', '/langversion:5', '/codepage:65001', '/utf8output', "/out:$outputExe")
$compilerArguments += @($references | ForEach-Object { "/reference:$_" })
$manifest = Join-Path $desktopRoot 'app.manifest'
if (Test-Path -LiteralPath $manifest) { $compilerArguments += "/win32manifest:$manifest" }
$compilerArguments += $sources
& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "Desktop compilation failed (exit $LASTEXITCODE)." }

foreach ($dependency in @('Microsoft.Web.WebView2.Core.dll',
    'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll')) {
    $source = Join-Path $projectRoot $dependency
    $destination = Join-Path $OutputDirectory $dependency
    if ([IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($destination)) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}
Copy-Item -LiteralPath (Join-Path $desktopRoot 'app.config') -Destination "$outputExe.config" -Force
Write-Host "Built: $outputExe"
Write-Host "SHA256: $((Get-FileHash -LiteralPath $outputExe -Algorithm SHA256).Hash)"
if ($Install) {
    $installedExe = Join-Path $projectRoot $exeName
    if ($installedExe -eq $outputExe) { throw 'Use a separate build output directory with -Install.' }
    if (Test-Path -LiteralPath $installedExe) {
        $backupDirectory = Join-Path $projectRoot 'build\desktop\backups'
        New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
        $backup = Join-Path $backupDirectory ((Get-Date -Format 'yyyyMMdd-HHmmss-ffff') + '-' + $exeName)
        Copy-Item -LiteralPath $installedExe -Destination $backup
        if (Test-Path -LiteralPath "$installedExe.config") {
            Copy-Item -LiteralPath "$installedExe.config" -Destination "$backup.config"
        }
        Write-Host "Previous executable backed up: $backup"
    }
    Copy-Item -LiteralPath $outputExe -Destination $installedExe -Force
    Copy-Item -LiteralPath "$outputExe.config" -Destination "$installedExe.config" -Force
    Write-Host "Installed: $installedExe"
    Write-Host 'Run start.cmd from the project root to open the rebuilt desktop host.'
} else {
    Write-Host 'To run this build with the project assets, close LiveWatch, then rerun this script with -Install.'
}
