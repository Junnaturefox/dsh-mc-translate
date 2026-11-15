# mct 运行器：在 DSH Windows 沙箱下用文件重定向代替管道调用 Python。
# 用法：  . "<skill>\scripts\_run.ps1"
#         Run-Py "<skill>\scripts\mct.py" @('probe','mods/xxx.jar')
# 也可设置 $env:MCT_PYTHON 指定解释器。

$script:SCRIPTS = $PSScriptRoot
$script:RUNNER = Join-Path $PSScriptRoot '_runner.py'

function Resolve-MctPython {
    if ($env:MCT_PYTHON -and (Test-Path $env:MCT_PYTHON)) { return $env:MCT_PYTHON }
    $known = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
    if (Test-Path $known) { return $known }
    $root = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes'
    if (Test-Path $root) {
        $hit = Get-ChildItem $root -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch 'WindowsApps' } | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    foreach ($name in 'python', 'python3') {
        $c = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c -and $c.Source -notmatch 'WindowsApps') { return $c.Source }
    }
    return $null
}

$script:PYEXE = Resolve-MctPython
if (-not $script:PYEXE) {
    Write-Output "错误: 找不到可用的 Python。请设置 `$env:MCT_PYTHON 指向 python.exe"
}

function Run-Py {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string[]]$ScriptArgs = @()
    )
    if (-not $script:PYEXE) { Write-Output "--- exit: missing-python ---"; return }

    $tmp = Join-Path (Get-Location).Path '.mct-tmp'
    if (-not (Test-Path $tmp)) { New-Item -ItemType Directory -Force -Path $tmp | Out-Null }
    $out = Join-Path $tmp 'py.out.txt'
    $err = Join-Path $tmp 'py.err.txt'
    $ec = Join-Path $tmp 'py.exit.txt'
    Remove-Item $out, $err, $ec -ErrorAction SilentlyContinue

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PYEXE
    $psi.UseShellExecute = $false
    $psi.WorkingDirectory = (Get-Location).Path
    foreach ($a in @($script:RUNNER, $out, $err, $ec, $script:PYEXE, $ScriptPath) + $ScriptArgs) {
        $psi.ArgumentList.Add($a) | Out-Null
    }

    $p = [System.Diagnostics.Process]::Start($psi)
    $p.WaitForExit()

    if (Test-Path $out) { Get-Content $out -Raw -Encoding UTF8 }
    if (Test-Path $err) {
        $e = Get-Content $err -Raw -Encoding UTF8
        if ($e -and $e.Trim()) { Write-Output "--- stderr ---"; Write-Output $e }
    }
    $code = if (Test-Path $ec) { (Get-Content $ec -Raw).Trim() } else { '?' }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Output "--- exit: $code ---"
}
