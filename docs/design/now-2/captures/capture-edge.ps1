$ErrorActionPreference = 'Stop'
$tab = (Invoke-RestMethod http://localhost:9334/json)[0]
$ws = [System.Net.WebSockets.ClientWebSocket]::new()
$ct = [Threading.CancellationToken]::None
$ws.ConnectAsync([Uri]$tab.webSocketDebuggerUrl, $ct).GetAwaiter().GetResult()
$script:seq = 0
function Cdp($method, $params) {
  $script:seq++
  $id = $script:seq
  $bytes = [Text.Encoding]::UTF8.GetBytes((@{id=$id; method=$method; params=$params} | ConvertTo-Json -Depth 20 -Compress))
  $ws.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $ct).GetAwaiter().GetResult() | Out-Null
  do {
    $stream = [IO.MemoryStream]::new()
    do {
      $buffer = New-Object byte[] 65536
      $r = $ws.ReceiveAsync([ArraySegment[byte]]::new($buffer), $ct).GetAwaiter().GetResult()
      $stream.Write($buffer, 0, $r.Count)
    } while (!$r.EndOfMessage)
    $msg = [Text.Encoding]::UTF8.GetString($stream.ToArray()) | ConvertFrom-Json
  } while ($msg.id -ne $id)
  if ($msg.error) { throw ($msg.error | ConvertTo-Json) }
  return $msg.result
}
Cdp 'Page.enable' @{} | Out-Null
Cdp 'Page.navigate' @{url='file:///C:/Users/joshm/AppData/Local/Temp/now2-revision/mock/now-2.html'} | Out-Null
Start-Sleep -Seconds 2
foreach ($width in @(1280,390)) {
  foreach ($theme in @('light','dark')) {
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=2400;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Emulation.setEmulatedMedia' @{features=@(@{name='prefers-color-scheme';value=$theme})} | Out-Null
    $measure = Cdp 'Runtime.evaluate' @{expression='JSON.stringify({width:innerWidth,scroll:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,dark:matchMedia("(prefers-color-scheme: dark)").matches})';returnByValue=$true}
    "$theme $width $($measure.result.value)"
    $dims = $measure.result.value | ConvertFrom-Json
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true;clip=@{x=0;y=0;width=$width;height=$dims.height;scale=1}}
    [IO.File]::WriteAllBytes("C:\Users\joshm\AppData\Local\Temp\now2-revision\$theme-$width-full.png", [Convert]::FromBase64String($shot.data))
    for ($y=0; $y -lt $dims.height; $y+=2000) {
      $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true;clip=@{x=0;y=$y;width=$width;height=[Math]::Min(2000,$dims.height-$y);scale=1}}
      [IO.File]::WriteAllBytes("C:\Users\joshm\AppData\Local\Temp\now2-revision\$theme-$width-$y.png", [Convert]::FromBase64String($shot.data))
    }
  }
}
$ws.Dispose()
