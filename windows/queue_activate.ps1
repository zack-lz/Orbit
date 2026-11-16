param([int]$Index,[string]$ExpectedName)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MusicQueueWindow {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int mode);
}
'@
$previousWindow=[MusicQueueWindow]::GetForegroundWindow()
$queuePattern=$null;$restoreClosed=$false;$wasMinimized=$false;$musicWindow=[IntPtr]::Zero;$historyPattern=$null;$restoreHistory=$false
try {
    $process=Get-Process AppleMusic -ErrorAction SilentlyContinue|Where-Object MainWindowHandle -ne 0|Select-Object -First 1
    if(!$process){throw 'app_closed'}
    $musicWindow=$process.MainWindowHandle
    $wasMinimized=[MusicQueueWindow]::IsIconic($musicWindow)
    if($wasMinimized){[void][MusicQueueWindow]::ShowWindow($musicWindow,9)}
    $root=[System.Windows.Automation.AutomationElement]::FromHandle($musicWindow)
    $scope=[System.Windows.Automation.TreeScope]::Descendants
    $idProperty=[System.Windows.Automation.AutomationElement]::AutomationIdProperty
    $button=$root.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,'PlayQueueToggleButton')))
    $queuePattern=[System.Windows.Automation.TogglePattern]$button.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
    if($queuePattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::Off){$restoreClosed=$true;$queuePattern.Toggle();Start-Sleep -Milliseconds 200}
    $nameProperty=[System.Windows.Automation.AutomationElement]::NameProperty
    $typeProperty=[System.Windows.Automation.AutomationElement]::ControlTypeProperty
    $nextCondition=New-Object System.Windows.Automation.AndCondition((New-Object System.Windows.Automation.PropertyCondition($nameProperty,'Playing Next')),(New-Object System.Windows.Automation.PropertyCondition($typeProperty,[System.Windows.Automation.ControlType]::Button)))
    $nextButton=$root.FindFirst($scope,$nextCondition)
    if($nextButton){
        $nextPattern=[System.Windows.Automation.TogglePattern]$nextButton.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        if($nextPattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::Off){
            $historyCondition=New-Object System.Windows.Automation.AndCondition((New-Object System.Windows.Automation.PropertyCondition($nameProperty,'History')),(New-Object System.Windows.Automation.PropertyCondition($typeProperty,[System.Windows.Automation.ControlType]::Button)))
            $historyButton=$root.FindFirst($scope,$historyCondition)
            if($historyButton){$historyPattern=[System.Windows.Automation.TogglePattern]$historyButton.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern);$restoreHistory=$historyPattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::On}
            $nextPattern.Toggle();Start-Sleep -Milliseconds 150
        }
    }
    $list=$root.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,'PlayQueueListView')))
    $rows=$list.FindAll([System.Windows.Automation.TreeScope]::Children,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::ListItem)))
    if($Index-lt 0-or$Index-ge$rows.Count-or!$ExpectedName){throw 'invalid_item'}
    $row=$rows[$Index]
    if($row.Current.Name-ne$ExpectedName){throw 'queue_changed'}
    try{$virtual=[System.Windows.Automation.VirtualizedItemPattern]$row.GetCurrentPattern([System.Windows.Automation.VirtualizedItemPattern]::Pattern);$virtual.Realize()}catch{}
    ([System.Windows.Automation.ScrollItemPattern]$row.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern)).ScrollIntoView()
    $texts=$row.FindAll($scope,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Text)))
    $expectedTitle=$texts[0].Current.Name
    $selection=[System.Windows.Automation.SelectionItemPattern]$row.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $selection.Select()
    [void][MusicQueueWindow]::SetForegroundWindow($musicWindow)
    $row.SetFocus()
    if([System.Windows.Automation.AutomationElement]::FocusedElement.Current.ProcessId-ne$process.Id){throw 'focus_unavailable'}
    # Activate the selected queue row with the app's own keyboard action.
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    $played=$false
    for($i=0;$i-lt 15;$i++){
        Start-Sleep -Milliseconds 100
        $lcd=$root.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,'LCD')))
        $current=$lcd.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,'ScrollingText')))
        if($current-and$current.Current.Name-eq$expectedTitle){$played=$true;break}
    }
    @{available=$played;reason=$(if($played){''}else{'activation_not_confirmed'})}|ConvertTo-Json -Compress
}catch{@{available=$false;reason=$_.Exception.Message}|ConvertTo-Json -Compress}
finally{
    if($restoreHistory-and$historyPattern){try{if($historyPattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::Off){$historyPattern.Toggle()}}catch{}}
    if($restoreClosed-and$queuePattern){try{if($queuePattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::On){$queuePattern.Toggle()}}catch{}}
    if($musicWindow-ne[IntPtr]::Zero-and[MusicQueueWindow]::GetForegroundWindow()-eq$musicWindow-and$previousWindow-ne[IntPtr]::Zero-and$previousWindow-ne$musicWindow){[void][MusicQueueWindow]::SetForegroundWindow($previousWindow)}
    if($wasMinimized-and$musicWindow-ne[IntPtr]::Zero){[void][MusicQueueWindow]::ShowWindow($musicWindow,6)}
}
