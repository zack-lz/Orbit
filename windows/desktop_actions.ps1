param([ValidateSet('Seek','Lyrics')][string]$Action,[string]$Position,[string]$ExpectedTitle)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$restoreLyrics=$false;$restoreQueue=$false;$lyricsPattern=$null;$queuePattern=$null
try {
    $process=Get-Process AppleMusic -ErrorAction SilentlyContinue|Where-Object MainWindowHandle -ne 0|Select-Object -First 1
    if(!$process){throw 'app_closed'}
    $root=[System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
    $scope=[System.Windows.Automation.TreeScope]::Descendants
    $idProperty=[System.Windows.Automation.AutomationElement]::AutomationIdProperty
    function Find-Id([string]$id){$root.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,$id)))}
    if($Action -eq 'Seek'){
        if($ExpectedTitle){$lcd=Find-Id 'LCD';$titleElement=$lcd.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,'ScrollingText')));if(!$titleElement-or$titleElement.Current.Name-ne$ExpectedTitle){throw 'track_changed'}}
        $value=[double]::Parse($Position,[Globalization.CultureInfo]::InvariantCulture)
        if([double]::IsNaN($value)-or[double]::IsInfinity($value)-or$value-lt 0){throw 'invalid_position'}
        $slider=Find-Id 'LCDScrubber'
        if(!$slider){throw 'seek_unavailable'}
        $pattern=[System.Windows.Automation.RangeValuePattern]$slider.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern)
        $range=$pattern.Current
        if($range.IsReadOnly-or$value-gt$range.Maximum+.5){throw 'seek_unavailable'}
        $pattern.SetValue([Math]::Min($range.Maximum,[Math]::Max($range.Minimum,$value)))
        @{available=$true;position=$value}|ConvertTo-Json -Compress
    } else {
        $button=Find-Id 'LyricsToggleButton'
        if(!$button-or!$button.Current.IsEnabled){throw 'lyrics_unavailable'}
        $lyricsPattern=[System.Windows.Automation.TogglePattern]$button.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
        $queueButton=Find-Id 'PlayQueueToggleButton'
        if($queueButton){$queuePattern=[System.Windows.Automation.TogglePattern]$queueButton.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern);$restoreQueue=$queuePattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::On}
        if($lyricsPattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::Off){$restoreLyrics=$true;$lyricsPattern.Toggle();Start-Sleep -Milliseconds 350}
        $pane=Find-Id 'TimeBasedLyrics'
        if(!$pane){$pane=Find-Id 'LyricsScrollViewer'}
        if(!$pane){throw 'lyrics_unavailable'}
        $buttons=$pane.FindAll($scope,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)))
        $lines=@()
        for($i=0;$i-lt$buttons.Count;$i++){$line=$buttons[$i].Current.Name.Trim();if($line){$lines+=$line}}
        if(!$lines.Count){
            $texts=$pane.FindAll($scope,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Text)))
            for($i=0;$i-lt$texts.Count;$i++){$current=$texts[$i].Current;if($current.Name-and$current.AutomationId-notmatch'Instrumental|Writers'){$lines+=$current.Name.Trim()}}
        }
        $lcd=Find-Id 'LCD'
        $title=''
        if($lcd){$titleElement=$lcd.FindFirst($scope,(New-Object System.Windows.Automation.PropertyCondition($idProperty,'ScrollingText')));if($titleElement){$title=$titleElement.Current.Name}}
        @{available=($lines.Count-gt 0);title=$title;lines=@($lines);synced=$false;source='apple_music_ui'}|ConvertTo-Json -Depth 4 -Compress
    }
}catch{@{available=$false;reason=$_.Exception.Message;lines=@()}|ConvertTo-Json -Compress}
finally{
    if($restoreLyrics-and$lyricsPattern){try{if($lyricsPattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::On){$lyricsPattern.Toggle()}}catch{}}
    if($restoreQueue-and$queuePattern){try{if($queuePattern.Current.ToggleState-eq[System.Windows.Automation.ToggleState]::Off){$queuePattern.Toggle()}}catch{}}
}
