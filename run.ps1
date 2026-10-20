# 论文工坊启动脚本
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host '首次运行：创建虚拟环境并安装依赖…' -ForegroundColor Yellow
    uv venv "$here\.venv" --python 3.14
    uv pip install --python $py -r "$here\requirements.txt"
}

Write-Host ''
Write-Host '  论文工坊已启动 → http://127.0.0.1:8765' -ForegroundColor Green
Write-Host '  按 Ctrl+C 停止' -ForegroundColor DarkGray
Write-Host ''

& $py (Join-Path $here 'app.py')
