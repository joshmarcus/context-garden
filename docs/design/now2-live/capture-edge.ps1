$ErrorActionPreference = 'Stop'
$tab = (Invoke-RestMethod http://localhost:9349/json) | Where-Object { $_.type -eq 'page' } | Select-Object -First 1
if (!$tab) { throw 'Open about:blank in the dedicated Edge capture profile first.' }
$ws = [System.Net.WebSockets.ClientWebSocket]::new()
$cancel = [Threading.CancellationTokenSource]::new(600000)
$ct = $cancel.Token
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
Cdp 'Page.navigate' @{url='http://localhost:8769/now2'} | Out-Null
Start-Sleep -Seconds 3
$ready = Cdp 'Runtime.evaluate' @{expression='document.querySelector("#now2") !== null';returnByValue=$true}
for ($attempt=0; !$ready.result.value -and $attempt -lt 30; $attempt++) { Start-Sleep -Seconds 1; $ready = Cdp 'Runtime.evaluate' @{expression='document.querySelector("#now2") !== null';returnByValue=$true} }
if (!$ready.result.value) { throw 'Now 2 did not load; do not overwrite captures with a blank page.' }
foreach ($width in @(1280,390)) {
  foreach ($theme in @('light','dark')) {
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=2400;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Emulation.setEmulatedMedia' @{features=@(@{name='prefers-color-scheme';value=$theme})} | Out-Null
    $measure = Cdp 'Runtime.evaluate' @{expression='JSON.stringify({width:innerWidth,scroll:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,dark:matchMedia("(prefers-color-scheme: dark)").matches})';returnByValue=$true}
    "$theme $width $($measure.result.value)"
    $dims = $measure.result.value | ConvertFrom-Json
    if ($dims.scroll -ne $width) { throw "Document overflows at $width/$theme" }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=[int]$dims.height;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Runtime.evaluate' @{expression='scrollTo(0,0);document.querySelectorAll(".n2-table").forEach(e=>e.scrollLeft=0)'} | Out-Null
    Start-Sleep -Milliseconds 500
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true;clip=@{x=0;y=0;width=$width;height=$dims.height;scale=1}}
    [IO.File]::WriteAllBytes("C:\Users\joshm\AppData\Local\Temp\cg309-captures\$theme-$width-full.png", [Convert]::FromBase64String($shot.data))
    for ($y=0; $y -lt $dims.height; $y+=2000) {
      $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true;clip=@{x=0;y=$y;width=$width;height=[Math]::Min(2000,$dims.height-$y);scale=1}}
      [IO.File]::WriteAllBytes("C:\Users\joshm\AppData\Local\Temp\cg309-captures\$theme-$width-$y.png", [Convert]::FromBase64String($shot.data))
    }
  }
}
$result = Cdp 'Runtime.evaluate' @{expression='JSON.stringify({status:document.querySelector("#n2-connection").textContent,clocks:[...document.querySelectorAll("[data-clock]")].map(e=>e.textContent),matrices:document.querySelectorAll("[data-metric]").length,visible:document.querySelectorAll("[data-metric]:not([hidden])").length})';returnByValue=$true}
$result.result.value
$ws.Dispose()
