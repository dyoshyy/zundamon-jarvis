# zundamon-jarvis one-line installer (Windows 10 / 11, x64)
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.ps1 | iex"
# 環境変数: ZJ_VERSION=v0.1.0 (版を固定) / ZJ_DRY_RUN=1 (実行せず表示だけ)
#   例: $env:ZJ_DRY_RUN = '1'; irm https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.ps1 | iex
#
# 全体を関数に包み、最後の行で呼ぶ。irm | iex が途中で切れても、半端なスクリプトは走らない。
# iex の中では exit を使わない（ユーザーの PowerShell ごと閉じてしまう）。失敗は throw で外の catch へ。
# 音声エンジンと文字起こしモデルは、最後に開く設定画面（zundamon-jarvis setup）が取得する。
# Windows PowerShell 5.1 で動くように書く（?? や && などの PowerShell 7 の構文は使わない）。

function Install-ZundamonJarvis {
    $ErrorActionPreference = 'Stop'
    $OwnerRepo = 'dyoshyy/zundamon-jarvis'
    $Version = $env:ZJ_VERSION
    $DryRun = ($env:ZJ_DRY_RUN -eq '1')
    $MinFreeBytes = 8GB
    $state = @{ Step = '' }

    function Say([string]$Text) { Write-Host $Text }
    function Step([string]$Name) { $state.Step = $Name; Write-Host "==> $Name" }
    # 利用者への説明で止まる（段の失敗ではない）
    function Die([string]$Text) { throw "ZJ:$Text" }
    # 段の失敗: 段の名前を出して止まる
    function Fail-Step { throw 'ZJSTEP' }

    # Invoke-Zj <exe> <args...>: dry-run なら表示だけ。失敗したら段の名前を出して止まる。
    function Invoke-Zj([string]$Exe, [string[]]$Arguments) {
        if ($DryRun) {
            Say ('    [dry-run] ' + ((@($Exe) + $Arguments) -join ' '))
            return
        }
        & $Exe @Arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { Fail-Step }
    }

    function Test-Command([string]$Name) {
        return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
    }

    try {
        # ネイティブ版の Claude Code と uv は ~\.local\bin に入る
        $env:Path = "$HOME\.local\bin;$env:Path"
        # 出力がパイプでも Python がためこまない（設定画面の案内がすぐ見える）
        $env:PYTHONUNBUFFERED = '1'

        # ① 環境の確認
        Step '環境を確認しています'
        if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
            Die "このインストーラは Windows 用です。Mac は install.sh、Linux は README の手順に従ってください。`n    https://github.com/$OwnerRepo#readme"
        }
        $arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
        if ($arch -ne 'AMD64') {
            Die "対応していない環境です（$arch）。x64（64 ビット）の Windows 10 / 11 が必要です。"
        }
        $os = [Environment]::OSVersion.Version
        if ($os.Major -lt 10) {
            Die "Windows $os では動きません。Windows 10 / 11（x64）が必要です。"
        }

        # ② claude
        if (-not (Test-Command 'claude')) {
            Die "claude コマンドが見つかりません。先に Claude Code をインストールしてログインしてから、もう一度実行してください。`n    https://claude.com/claude-code"
        }

        # ③ ディスクの空き（エンジンとモデルは %LOCALAPPDATA% に入る）
        $drive = New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot($env:LOCALAPPDATA))
        if ($drive.AvailableFreeSpace -lt $MinFreeBytes) {
            $gb = [math]::Floor($drive.AvailableFreeSpace / 1GB)
            Die "ディスクの空きが足りません（$gb GB）。モデルと音声エンジンで 8 GB 以上の空きが必要です。空けてからもう一度実行してください。"
        }

        # 版の決定
        if (-not $Version) {
            if ($DryRun) {
                $Version = '<latest tag>'
            } else {
                Step '最新の版を調べています'
                try {
                    # 古い .NET の既定は TLS 1.0。GitHub は 1.2 以上しか受けない
                    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                    $Version = [string](Invoke-RestMethod "https://api.github.com/repos/$OwnerRepo/releases/latest").tag_name
                } catch {
                    $Version = ''
                }
                if (-not $Version) {
                    Die "最新の版を取得できませんでした（リリースが無いか、GitHub の問い合わせ回数の上限です）。`n    版を指定してもう一度実行してください。例: `$env:ZJ_VERSION = 'v0.1.0'; irm https://raw.githubusercontent.com/$OwnerRepo/main/install.ps1 | iex"
                }
            }
        }
        Say "    版: $Version"

        # ④ uv
        Step 'uv を確認しています'
        if (Test-Command 'uv') {
            Say '    uv は入っています'
        } else {
            Say '    uv を入れます'
            if ($DryRun) {
                Say '    [dry-run] powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
            } else {
                # 別プロセスで: uv のインストーラが exit しても、この関数は続く
                & powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" | Out-Host
                if ($LASTEXITCODE -ne 0) { Fail-Step }
                if (-not (Test-Command 'uv')) { Fail-Step }
            }
        }

        # uv tool の bin ディレクトリを、このセッションの PATH に入れる
        $uvBin = ''
        if (Test-Command 'uv') {
            try { $uvBin = [string](& uv tool dir --bin | Select-Object -First 1) } catch { $uvBin = '' }
        }
        if (-not $uvBin) { $uvBin = "$HOME\.local\bin" }
        $env:Path = "$uvBin;$env:Path"

        # ⑤ 本体（git が要らないように、タグの zip から入れる）
        Step 'zundamon-jarvis を入れます'
        $want = $Version -replace '^v', ''
        $have = ''
        if (-not $DryRun -and (Test-Command 'zundamon-jarvis')) {
            try { $have = ([string](& zundamon-jarvis --version | Select-Object -Last 1)).Trim() } catch { $have = '' }
        }
        if ($have -and $have -eq $want) {
            Say "    すでに $have が入っています。飛ばします"
        } else {
            if ($have) { Say "    $have から $Version に入れ直します" }
            Invoke-Zj 'uv' @('tool', 'install', '--force', '--python', '3.12',
                             "zundamon-jarvis @ https://github.com/$OwnerRepo/archive/refs/tags/$Version.zip")
        }
        if (-not $DryRun) {
            if (-not (Test-Command 'zundamon-jarvis')) { Fail-Step }
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (-not (($userPath -split ';') -contains $uvBin)) {
                Say "    注意: $uvBin が PATH に入っていません。"
                Say '    このあと uv tool update-shell を実行して、PowerShell を開き直してください。'
            }
        }

        # ⑥ 設定（ブラウザで開く。音声エンジンとモデルの取得、常駐の設定もここで済む）
        Step '設定を始めます（ブラウザが開きます）'
        Invoke-Zj 'zundamon-jarvis' @('setup')

        Say '==> 完了しました'
    } catch {
        $msg = [string]$_.Exception.Message
        if ($msg.StartsWith('ZJ:')) {
            Write-Host $msg.Substring(3) -ForegroundColor Red
        } else {
            if ($msg -ne 'ZJSTEP') { Write-Host "    $msg" -ForegroundColor Red }
            Write-Host "==> 失敗しました: $($state.Step)" -ForegroundColor Red
            Write-Host '    原因を直したあと、同じコマンドをもう一度実行すれば、続きから進みます。' -ForegroundColor Red
        }
        # ファイルとして実行したときだけ終了コードを返す（iex の中の exit は PowerShell ごと閉じる）
        if ($PSCommandPath) { exit 1 }
    }
}

Install-ZundamonJarvis
