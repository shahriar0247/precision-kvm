$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonw) {
    Start-Process -FilePath $pythonw.Source -ArgumentList '-m', 'precision_kvm', 'gui' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
} else {
    python -m precision_kvm gui
}
