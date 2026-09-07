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
for ($attempt=0; $attempt -lt 40; $attempt++) {
  $ready = Cdp 'Runtime.evaluate' @{expression='Boolean(document.querySelector("[data-run-id]") && document.querySelector("#n2-connection").textContent.includes("connected"))';returnByValue=$true}
  if ($ready.result.value) { break }
  Start-Sleep -Seconds 1
}
if (!$ready.result.value) { throw 'Live fixture did not become ready' }
$expression = [IO.File]::ReadAllText('C:\Users\joshm\AppData\Local\Temp\cg309-captures\browser-checks.js')
$result = Cdp 'Runtime.evaluate' @{expression=$expression;returnByValue=$true;awaitPromise=$true}
if ($result.exceptionDetails) { throw ($result | ConvertTo-Json -Depth 10) }
$result.result.value
Start-Sleep -Seconds 5
$result = Cdp 'Runtime.evaluate' @{expression='JSON.stringify({disclosureRetained:window.n2BrowserCheck.detail.isConnected && window.n2BrowserCheck.detail.open,focusRetained:document.activeElement===window.n2BrowserCheck.summary,metricRetained:document.querySelector("#n2-metric").value==="total_cost",connected:document.querySelector("#n2-connection").textContent.includes("connected")})';returnByValue=$true}
$result.result.value
$retained = $result.result.value | ConvertFrom-Json
if (!$retained.disclosureRetained -or !$retained.focusRetained -or !$retained.metricRetained -or !$retained.connected) { throw 'Reconnect failed to preserve live page state' }
$ws.Dispose()
