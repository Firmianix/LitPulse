# setup_pdfjs.ps1 —— Windows 一键下载 pdf.js 官方查看器
# 用法（在项目根目录的 PowerShell 里）：
#   powershell -ExecutionPolicy Bypass -File .\setup_pdfjs.ps1
# 作用：把 pdf.js 下载到 static\pdfjs，启用"点击页码跳回原文(pdf.js 高亮)"。
$ErrorActionPreference = "Stop"

$Ver = "6.4.299"
$Dest = Join-Path $PSScriptRoot "static\pdfjs"
$Viewer = Join-Path $Dest "web\viewer.html"

if (Test-Path $Viewer) {
    Write-Host "pdf.js 已存在：$Dest"
    exit 0
}

$tmp = Join-Path $env:TEMP "pdfjs_setup_$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp "pdfjs.zip"

$urls = @(
    "https://github.com/mozilla/pdf.js/releases/download/v$Ver/pdfjs-$Ver-dist.zip",
    "https://ghfast.top/https://github.com/mozilla/pdf.js/releases/download/v$Ver/pdfjs-$Ver-dist.zip",
    "https://ghproxy.net/https://github.com/mozilla/pdf.js/releases/download/v$Ver/pdfjs-$Ver-dist.zip"
)

$ok = $false
foreach ($u in $urls) {
    try {
        Write-Host "尝试下载：$u"
        Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing -TimeoutSec 120
        if ((Get-Item $zip).Length -gt 1MB) { $ok = $true; break }
    } catch {
        Write-Host "  失败，换下一个源 ..."
    }
}
if (-not $ok) {
    Write-Host "自动下载失败。请手动下载上面任一地址的 zip，解压到 $Dest（确保存在 $Viewer）。"
    exit 1
}

$extract = Join-Path $tmp "x"
Expand-Archive -Path $zip -DestinationPath $extract -Force
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

# release zip 解压后通常顶层直接是 build/ web/；个别源会多包一层目录，做个兼容
if (Test-Path (Join-Path $extract "web\viewer.html")) {
    $root = $extract
} else {
    $root = Get-ChildItem $extract -Directory | Select-Object -First 1
}
Copy-Item -Path (Join-Path $root "*") -Destination $Dest -Recurse -Force
Remove-Item $tmp -Recurse -Force

if (Test-Path $Viewer) {
    Write-Host "完成：$Viewer"
} else {
    Write-Host "解压后未找到 viewer.html，请检查 zip 结构。"
    exit 1
}
