#Requires -Version 5.1
<#
.SYNOPSIS
    Measures the Docker build context of a directory with and without .dockerignore
    rules (moby/patternmatcher semantics approximations).

.DESCRIPTION
    For every build context listed in -ContextName the script:
      1) reads <context>/.dockerignore and parses it with the same normalisation
         BuildKit applies (dockerignore.ReadAll: strip UTF-8 BOM, skip lines that
         start with '#', TrimSpace, skip empty, filepath.Clean, backslash -> slash);
      2) walks every file under the context root;
      3) reports the context BEFORE filtering (no rules applied: the git HEAD state
         where .dockerignore did not exist) and AFTER filtering (rules applied in
         file order, last decisive match wins, '!' re-includes);
      4) attributes excluded bytes to the decisive pattern.

    Matching replicates moby/patternmatcher:
      - a pattern is matched against the slash-separated path relative to the
        context root, and (if the direct match fails) against every ancestor
        directory prefix (MatchesOrParentMatches);
      - '**/x' also matches 'x' at the root; 'dir/**' matches everything below
        'dir/'; '*' does not cross '/'; '[abc]' is a character class;
      - patterns are evaluated in file order; the LAST matching pattern that
        flips the state decides. State starts as "included".

    Known divergences from the real Docker/ BuildKit behaviour are documented in
    docs/docker-image-size/verification.md. Read them before trusting the numbers.

.PARAMETER RepoRoot
    Repository root that contains the context directories. Defaults to two levels
    above the directory of this script.

.PARAMETER ContextName
    One or more context directory names relative to RepoRoot. Default: backend, chainlit.

.PARAMETER ListKept
    Optional path prefix (relative to each context root, slash-separated). Files
    that survive the filter and start with this prefix are listed. Use ALL to list
    every surviving file.

.PARAMETER TopExcluded
    How many decisive patterns to show in the exclusion breakdown. Default 10.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs\docker-image-size\context-measure.ps1
.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs\docker-image-size\context-measure.ps1 -ListKept app
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string[]]$ContextName = @('backend', 'chainlit'),
    [string]$ListKept,
    [int]$TopExcluded = 10
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

# --------------------------------------------------------------------------
# .dockerignore parsing (BuildKit dockerignore.ReadAll + patternmatcher.New)
# --------------------------------------------------------------------------

function Clear-DockerPath {
    param([string]$Path)
    $p = $Path -replace '\\', '/'
    $p = $p -replace '/{2,}', '/'
    while ($p.StartsWith('./')) { $p = $p.Substring(2) }
    $p = $p.TrimEnd('/')
    if ($p -eq '') { $p = '.' }
    return $p
}

function Get-DockerIgnoreRule {
    param([string]$Path)
    $rules = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $rules }
    $first = $true
    foreach ($raw in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $line = [string]$raw
        if ($first) {
            $line = $line.TrimStart([char]0xFEFF)
            $first = $false
        }
        if ($line.StartsWith('#')) { continue }
        $line = $line.Trim()
        if ($line -eq '') { continue }
        $line = Clear-DockerPath $line
        $exclusion = $false
        if ($line.StartsWith('!')) {
            $exclusion = $true
            $line = $line.Substring(1)
            if ($line -eq '') { continue }
        }
        $null = $rules.Add([pscustomobject]@{
            Raw        = $line
            IsExclude  = $exclusion
            DirCount   = ($line -split '/').Count
            Regex      = ConvertTo-DockerRegex $line
            IsPlain    = ($line -notmatch '[*?\[]')
        })
    }
    return $rules
}

# Translates a .dockerignore pattern into an anchored .NET regex the same way
# moby/patternmatcher.Pattern.compile does for a '/' separator.
function ConvertTo-DockerRegex {
    param([string]$Pattern)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('^')
    $i = 0
    $n = $Pattern.Length
    while ($i -lt $n) {
        $ch = $Pattern[$i]
        if ($ch -eq '*') {
            if (($i + 1) -lt $n -and $Pattern[$i + 1] -eq '*') {
                $i += 2
                if ($i -lt $n -and $Pattern[$i] -eq '/') { $i++ }
                if ($i -ge $n) { [void]$sb.Append('.*') } else { [void]$sb.Append('(.*/)?') }
                continue
            }
            [void]$sb.Append('[^/]*')
        }
        elseif ($ch -eq '?') { [void]$sb.Append('[^/]') }
        elseif ($ch -eq '[' -or $ch -eq ']') { [void]$sb.Append($ch) }
        else { [void]$sb.Append([regex]::Escape([string]$ch)) }
        $i++
    }
    [void]$sb.Append('$')
    return $sb.ToString()
}

# moby/patternmatcher: direct match first, then every ancestor prefix.
function Test-DockerRule {
    param(
        [pscustomobject]$Rule,
        [string]$RelPath,
        [string[]]$Prefixes
    )
    if ($Rule.IsPlain) {
        if ($RelPath -eq $Rule.Raw) { return $true }
        foreach ($p in $Prefixes) { if ($p -eq $Rule.Raw) { return $true } }
        return $false
    }
    if ([regex]::IsMatch($RelPath, $Rule.Regex)) { return $true }
    foreach ($p in $Prefixes) { if ([regex]::IsMatch($p, $Rule.Regex)) { return $true } }
    return $false
}

# --------------------------------------------------------------------------
# Context walk + filter
# --------------------------------------------------------------------------

function Measure-BuildContext {
    param(
        [string]$Root,
        [string]$Name,
        [string]$KeptPrefix,
        [int]$Top
    )

    $ignorePath = Join-Path $Root '.dockerignore'
    $rules = @(Get-DockerIgnoreRule -Path $ignorePath)
    $hasIgnore = Test-Path -LiteralPath $ignorePath -PathType Leaf

    $walkErrors = New-Object System.Collections.ArrayList
    $files = Get-ChildItem -LiteralPath $Root -Recurse -Force -File -ErrorAction SilentlyContinue -ErrorVariable +walkErrors |
        Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) }

    $beforeBytes = 0L
    $beforeCount = 0
    $afterBytes = 0L
    $afterCount = 0
    $keptList = New-Object System.Collections.ArrayList
    $byRule = @{}

    foreach ($f in $files) {
        $rel = $f.FullName.Substring($Root.Length).TrimStart('\', '/') -replace '\\', '/'
        $len = [int64]$f.Length
        $beforeBytes += $len
        $beforeCount++

        # ancestor prefixes, shortest first (moby MatchesOrParentMatches)
        $prefixes = @()
        $slash = $rel.IndexOf('/')
        while ($slash -ge 0) {
            $prefixes += $rel.Substring(0, $slash)
            $slash = $rel.IndexOf('/', $slash + 1)
        }

        $matched = $false
        $decisive = $null
        foreach ($rule in $rules) {
            # moby: skip an inclusion that is already matched / an exclusion not yet matched
            if ($rule.IsExclude -ne $matched) { continue }
            if (Test-DockerRule -Rule $rule -RelPath $rel -Prefixes $prefixes) {
                $matched = -not $rule.IsExclude
                $decisive = $rule.Raw
            }
        }

        if ($matched) {
            $key = if ($null -eq $decisive) { '<unknown>' } else { $decisive }
            if (-not $byRule.ContainsKey($key)) {
                $byRule[$key] = [pscustomobject]@{ Pattern = $key; Files = 0; Bytes = 0L }
            }
            $byRule[$key].Files++
            $byRule[$key].Bytes += $len
        }
        else {
            $afterBytes += $len
            $afterCount++
            if ($KeptPrefix -and ($KeptPrefix -eq 'ALL' -or $rel.StartsWith($KeptPrefix, [StringComparison]::OrdinalIgnoreCase))) {
                $null = $keptList.Add(('{0}  {1} B' -f $rel, $len))
            }
        }
    }

    $denied = @($walkErrors | Where-Object { $_ -and $_.Exception -is [System.UnauthorizedAccessException] })
    $otherErrors = @($walkErrors | Where-Object { $_ -and $_.Exception -isnot [System.UnauthorizedAccessException] })

    return [pscustomobject]@{
        Name          = $Name
        Root          = $Root
        IgnorePath    = $ignorePath
        HasIgnoreFile = $hasIgnore
        Rules         = $rules
        RuleCount     = $rules.Count
        BeforeCount   = $beforeCount
        BeforeBytes   = $beforeBytes
        AfterCount    = $afterCount
        AfterBytes    = $afterBytes
        ExcludedCount = $beforeCount - $afterCount
        ExcludedBytes = $beforeBytes - $afterBytes
        Breakdown     = @($byRule.Values | Sort-Object -Property Bytes -Descending | Select-Object -First $Top)
        DeniedPaths   = @($denied | ForEach-Object { $_.TargetObject })
        OtherErrors   = @($otherErrors | ForEach-Object { $_.Exception.Message })
        KeptList      = @($keptList | Sort-Object)
    }
}

function Format-MiB {
    param([int64]$Bytes)
    return ('{0:N2} MiB' -f ($Bytes / 1MB))
}

# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

$results = @()
foreach ($ctxName in $ContextName) {
    $ctxRoot = Join-Path $RepoRoot $ctxName
    if (-not (Test-Path -LiteralPath $ctxRoot -PathType Container)) {
        Write-Host ("ERROR: context not found: {0}" -f $ctxRoot)
        exit 3
    }
    $results += Measure-BuildContext -Root $ctxRoot -Name $ctxName -KeptPrefix $ListKept -Top $TopExcluded
}

foreach ($r in $results) {
    Write-Host ('=' * 72)
    Write-Host ("CONTEXT: {0}" -f $r.Name)
    Write-Host ("root          : {0}" -f $r.Root)
    if ($r.HasIgnoreFile) {
        Write-Host ("dockerignore  : {0}" -f $r.IgnorePath)
    }
    else {
        Write-Host ("dockerignore  : <absent> (no filtering; this is the pre-change state)")
    }
    Write-Host ("rules         : {0} ({1} negations)" -f $r.RuleCount, (@($r.Rules | Where-Object { $_.IsExclude }).Count))
    Write-Host ("BEFORE (all)  : {0} files, {1} ({2} B)" -f $r.BeforeCount, (Format-MiB $r.BeforeBytes), $r.BeforeBytes)
    Write-Host ("AFTER (kept)  : {0} files, {1} ({2} B)" -f $r.AfterCount, (Format-MiB $r.AfterBytes), $r.AfterBytes)
    if ($r.BeforeBytes -gt 0) {
        $pct = [math]::Round(100.0 * $r.ExcludedBytes / $r.BeforeBytes, 2)
    }
    else { $pct = 0 }
    Write-Host ("EXCLUDED      : {0} files, {1} ({2} B, {3}% of bytes)" -f $r.ExcludedCount, (Format-MiB $r.ExcludedBytes), $r.ExcludedBytes, $pct)
    Write-Host ("exclusions by decisive pattern (top {0}):" -f $TopExcluded)
    foreach ($b in $r.Breakdown) {
        Write-Host ("    {0,-32} {1,7} files  {2,12}" -f $b.Pattern, $b.Files, (Format-MiB $b.Bytes))
    }
    if ($r.DeniedPaths.Count -gt 0) {
        Write-Host ("UNREADABLE    : {0} path(s) could not be enumerated (access denied):" -f $r.DeniedPaths.Count)
        foreach ($d in $r.DeniedPaths) { Write-Host ("    {0}" -f $d) }
    }
    if ($r.OtherErrors.Count -gt 0) {
        Write-Host ("WALK ERRORS   : {0}" -f $r.OtherErrors.Count)
        foreach ($e in $r.OtherErrors) { Write-Host ("    {0}" -f $e) }
    }
    if ($ListKept) {
        Write-Host ("KEPT under '{0}': {1} file(s)" -f $ListKept, $r.KeptList.Count)
        foreach ($k in $r.KeptList) { Write-Host ("    {0}" -f $k) }
    }
}

exit 0
