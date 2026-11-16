$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$queuePattern = $null
$historyPattern = $null
$restoreClosed = $false
$restoreHistory = $false
try {
    $process = Get-Process -Name AppleMusic -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
    if (!$process) { throw 'app_closed' }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
    $scope = [System.Windows.Automation.TreeScope]::Descendants
    $idProperty = [System.Windows.Automation.AutomationElement]::AutomationIdProperty
    $nameProperty = [System.Windows.Automation.AutomationElement]::NameProperty
    $typeProperty = [System.Windows.Automation.AutomationElement]::ControlTypeProperty
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition($idProperty,'PlayQueueToggleButton')
    $button = $root.FindFirst($scope,$buttonCondition)
    if (!$button) { throw 'queue_unavailable' }
    $queuePattern = [System.Windows.Automation.TogglePattern]$button.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
    if ($queuePattern.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::Off) {
        $restoreClosed = $true
        $queuePattern.Toggle()
        Start-Sleep -Milliseconds 200
    }
    $nextCondition = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($nameProperty,'Playing Next')),
        (New-Object System.Windows.Automation.PropertyCondition($typeProperty,[System.Windows.Automation.ControlType]::Button)))
    $nextButton = $root.FindFirst($scope,$nextCondition)
    if ($nextButton) {
        $nextPattern = [System.Windows.Automation.TogglePattern]$nextButton.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        if ($nextPattern.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::Off) {
            $historyCondition = New-Object System.Windows.Automation.AndCondition(
                (New-Object System.Windows.Automation.PropertyCondition($nameProperty,'History')),
                (New-Object System.Windows.Automation.PropertyCondition($typeProperty,[System.Windows.Automation.ControlType]::Button)))
            $historyButton = $root.FindFirst($scope,$historyCondition)
            if ($historyButton) {
                $historyPattern = [System.Windows.Automation.TogglePattern]$historyButton.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
                $restoreHistory = $historyPattern.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On
            }
            $nextPattern.Toggle()
            Start-Sleep -Milliseconds 150
        }
    }
    $listCondition = New-Object System.Windows.Automation.PropertyCondition($idProperty,'PlayQueueListView')
    $list = $root.FindFirst($scope,$listCondition)
    if (!$list) { throw 'queue_unavailable' }
    $rowCondition = New-Object System.Windows.Automation.PropertyCondition($typeProperty,[System.Windows.Automation.ControlType]::ListItem)
    $rows = $list.FindAll([System.Windows.Automation.TreeScope]::Children,$rowCondition)
    $textCondition = New-Object System.Windows.Automation.PropertyCondition($typeProperty,[System.Windows.Automation.ControlType]::Text)
    $tracks = @()
    for ($i=0; $i -lt [Math]::Min($rows.Count,100); $i++) {
        $texts = $rows[$i].FindAll($scope,$textCondition)
        $values = @()
        for ($j=0; $j -lt $texts.Count; $j++) { if ($texts[$j].Current.Name) { $values += $texts[$j].Current.Name } }
        if ($values.Count -ge 2) {
            $tracks += @{index=$i;identity=$rows[$i].Current.Name;title=$values[0];detail=$values[1];duration= $(if($values.Count -ge 3){$values[-1]}else{''})}
        }
    }
    @{available=$true;tracks=@($tracks);partial=$true;source='apple_music_ui'} | ConvertTo-Json -Depth 5 -Compress
} catch {
    @{available=$false;tracks=@();reason=$_.Exception.Message} | ConvertTo-Json -Depth 3 -Compress
} finally {
    if ($restoreHistory -and $historyPattern) { try { $historyPattern.Toggle() } catch {} }
    if ($restoreClosed -and $queuePattern) { try { $queuePattern.Toggle() } catch {} }
}
