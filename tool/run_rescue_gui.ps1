param(
    [string]$PythonCommand = ""
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$guiScript = Join-Path $scriptDir "rescue_gui.py"

if (-not $PythonCommand) {
    foreach ($candidate in @("pyw", "pythonw", "py", "python")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $PythonCommand = $candidate
            break
        }
    }
}

if (-not $PythonCommand) {
    throw "No Python launcher was found."
}

if ($PythonCommand -in @("pyw", "pythonw")) {
    Start-Process -FilePath $PythonCommand -ArgumentList @($guiScript) -WorkingDirectory (Split-Path -Parent $scriptDir) | Out-Null
    exit 0
}

& $PythonCommand $guiScript
exit $LASTEXITCODE
