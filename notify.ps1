param(
    [string]$Title = "AI Meter",
    [string]$Text = "",
    [int]$Seconds = 20
)

# Custom always-on-top popup instead of NotifyIcon balloon tips: Windows
# Focus Assist / "do not disturb" silently drops balloons, while a real
# window always shows. Pure ASCII on purpose (PS 5.1 reads BOM-less files
# as GBK, so Chinese literals inside the script would break).
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Media.SystemSounds]::Exclamation.Play()

# rough estimate: 10.5pt CJK glyph is ~14px wide, ~356px content area
$charsPerLine = 24
$lines = 0
foreach ($seg in ($Text -split "`n")) {
    $lines += [Math]::Max(1, [Math]::Ceiling($seg.Length / $charsPerLine))
}
$width = 400
$bodyHeight = [Math]::Min(260, 20 * $lines + 8)
$height = 30 + $bodyHeight + 56          # title + body + footer

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.FormBorderStyle = 'None'
$form.StartPosition = 'Manual'
$form.ShowInTaskbar = $false
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(28, 28, 30)
$form.Width = $width
$form.Height = $height

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Left = $screen.Right - $width - 24
$form.Top = $screen.Bottom - $height - 24

$accent = New-Object System.Windows.Forms.Panel
$accent.BackColor = [System.Drawing.Color]::FromArgb(230, 126, 34)
$accent.Width = 6
$accent.Height = $height
$accent.Left = 0
$accent.Top = 0
$form.Controls.Add($accent)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Text = $Title
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 12, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::White
$lblTitle.AutoSize = $false
$lblTitle.Left = 18
$lblTitle.Top = 12
$lblTitle.Width = $width - 34
$lblTitle.Height = 26
$form.Controls.Add($lblTitle)

$lblBody = New-Object System.Windows.Forms.Label
$lblBody.Text = $Text
$lblBody.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5)
$lblBody.ForeColor = [System.Drawing.Color]::FromArgb(220, 220, 220)
$lblBody.AutoSize = $false
$lblBody.Left = 18
$lblBody.Top = 40
$lblBody.Width = $width - 34
$lblBody.Height = $bodyHeight
$form.Controls.Add($lblBody)

$lblFoot = New-Object System.Windows.Forms.Label
$lblFoot.Text = "click to dismiss"
$lblFoot.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8)
$lblFoot.ForeColor = [System.Drawing.Color]::FromArgb(130, 130, 130)
$lblFoot.AutoSize = $false
$lblFoot.Left = 18
$lblFoot.Top = $height - 24
$lblFoot.Width = $width - 34
$lblFoot.Height = 18
$form.Controls.Add($lblFoot)

$close = { $form.Close() }
$form.Add_Click($close)
$accent.Add_Click($close)
$lblTitle.Add_Click($close)
$lblBody.Add_Click($close)
$lblFoot.Add_Click($close)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(3, $Seconds) * 1000
$timer.Add_Tick({ $timer.Stop(); $form.Close() })
$form.Add_Shown({ [void]$form.Activate(); $timer.Start() })

[void]$form.ShowDialog()
$form.Dispose()
