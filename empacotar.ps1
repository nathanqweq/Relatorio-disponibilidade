# Gera dist\ENGIE_Relatorios_Disponibilidade.zip para levar ao servidor. Rode no Windows:
#   powershell -ExecutionPolicy Bypass -File empacotar.ps1
# Text files go out with LF line endings: bash and systemd do not accept CRLF.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$raiz = $PSScriptRoot
$nome = 'ENGIE_Relatorios_Disponibilidade'
$saida = Join-Path $raiz "dist\$nome.zip"
New-Item -ItemType Directory -Force (Split-Path $saida) | Out-Null
if (Test-Path $saida) { Remove-Item $saida -Force }

$incluir = @('README.md', 'instalar.sh', 'instalar.py', 'docs', 'servidor', 'grafana')
# Arquivos gerados em uso nunca entram no pacote (credenciais, banco, usuarios, backups, PDFs).
$fora = '(^|/)(__pycache__|\.pytest_cache|backups|pdf)(/|$)|\.pyc$|\.db(-wal|-shm)?$|(^|/)(env|env\.anterior|admin_usuarios\.json|admin_auditoria\.log|relatorios\.json\.pacote)$'
$texto = '\.(sh|py|sql|service|timer|json|md|js|css|html)$'

$arquivos = foreach ($i in $incluir) {
    $p = Join-Path $raiz $i
    if (Test-Path $p -PathType Container) { Get-ChildItem $p -Recurse -File -Force } else { Get-Item $p }
}

$zip = [IO.Compression.ZipFile]::Open($saida, 'Create')
$n = 0
try {
    foreach ($a in $arquivos) {
        $rel = $a.FullName.Substring($raiz.TrimEnd('\').Length + 1).Replace('\', '/')
        if ($rel -match $fora) { continue }
        $bytes = [IO.File]::ReadAllBytes($a.FullName)
        if ($rel -match $texto) {
            $s = [Text.Encoding]::UTF8.GetString($bytes).Replace("`r`n", "`n")
            $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($s)
        }
        $entrada = $zip.CreateEntry("$nome/$rel", 'Optimal')
        $f = $entrada.Open()
        try { $f.Write($bytes, 0, $bytes.Length) } finally { $f.Close() }
        $n++
    }
} finally { $zip.Dispose() }
Write-Output "$n arquivos em $saida"
