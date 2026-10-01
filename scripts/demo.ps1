<#
  ComCo Integration Platform - demo script for Windows PowerShell / PowerShell 7.

  Usage (from the repository folder, after `docker compose up -d --build`):
    powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1            # all scenarios
    powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1 agent      # one section

  Sections: routing, errors, security, agent, a2a, failure, tracing, ratelimit
#>
param([string]$Section = "all")

$Gw = if ($env:GATEWAY_URL) { $env:GATEWAY_URL } else { "http://localhost:8000" }
$Api = "$Gw/api/v1"
$Key = "demo-client-key"
$TmpBody = Join-Path $env:TEMP "comco_demo_body.json"
$TmpHdr = Join-Path $env:TEMP "comco_demo_headers.txt"

function Title([string]$text) { Write-Host "`n== $text ==" -ForegroundColor White -BackgroundColor DarkBlue }
function Step([string]$text) { Write-Host "`n> $text" -ForegroundColor Cyan }

function Show-Json([string]$raw) {
    try { $raw | ConvertFrom-Json | ConvertTo-Json -Depth 10 | Write-Host }
    catch { Write-Host $raw }
}

# Calls the API with curl.exe and prints status, correlation id and body.
function Invoke-Api {
    param([string]$Desc, [string]$Url, [string]$Method = "GET", [hashtable]$Headers = @{}, [string]$Body, [switch]$Agent)
    Step $Desc
    $curlArgs = @("-s", "-D", $TmpHdr, "-X", $Method, $Url)
    foreach ($h in $Headers.GetEnumerator()) { $curlArgs += @("-H", "$($h.Key): $($h.Value)") }
    if ($Body) {
        # Write the body to a file - avoids PowerShell's quoting problems with native commands.
        [System.IO.File]::WriteAllText($TmpBody, $Body)
        $curlArgs += @("-H", "Content-Type: application/json", "--data-binary", "@$TmpBody")
    }
    $out = & curl.exe @curlArgs
    $hdr = Get-Content $TmpHdr
    $status = ($hdr | Select-Object -First 1) -replace '^HTTP/[\d.]+ ', ''
    $cid = ($hdr | Where-Object { $_ -match '^x-correlation-id:' }) -replace '^x-correlation-id:\s*', ''
    Write-Host "HTTP $status   X-Correlation-ID: $cid" -ForegroundColor Yellow
    $hdr | Where-Object { $_ -match '^(RateLimit-|Retry-After)' } | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
    $raw = ($out -join "`n")
    if ($Agent) {
        try {
            $r = $raw | ConvertFrom-Json
            Write-Host "status  : $($r.status)" -ForegroundColor Green
            Write-Host "answer  : $($r.answer)"
            Write-Host "planner : $($r.planner)   intent: $($r.intent)   executor: $($r.executor)"
            Write-Host "path    : $($r.graph_path -join ' -> ')"
            foreach ($s in $r.steps) {
                $a = ($s.args.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ","
                $viaText = if ($s.a2a) { "via $($s.a2a.agent), task $($s.a2a.task_id.Substring(0,8)) $($s.a2a.state)" } elseif ($s.via) { "via $($s.via)" } else { "" }
                Write-Host ("  - {0}({1}) -> {2} {3} {4}ms  {5}" -f $s.tool, $a, $s.outcome, $s.http_status, $s.latency_ms, $viaText)
            }
            $script:LastAgentResponse = $r
            return
        } catch { }
    }
    Show-Json $raw
}

function Ask([string]$Question, [hashtable]$Extra = @{}) {
    $h = @{ apikey = $Key } + $Extra
    $body = @{ query = $Question } | ConvertTo-Json -Compress
    Invoke-Api -Desc "Agent: `"$Question`"" -Url "$Api/agent/query" -Method POST -Headers $h -Body $body -Agent
}

function Routing {
    Title "1. Retrieve a customer (single API via the gateway)"
    Invoke-Api "GET /api/v1/customers/C001" "$Api/customers/C001" -Headers @{ apikey = $Key }
    Title "2. Latest order of a customer (Order API)"
    Invoke-Api "GET /api/v1/orders/latest?customer_id=C001" "$Api/orders/latest?customer_id=C001" -Headers @{ apikey = $Key }
}

function Errors {
    Title "3. Unknown customer -> 404 Problem Details"
    Invoke-Api "GET /api/v1/customers/C999" "$Api/customers/C999" -Headers @{ apikey = $Key }
    Title "3b. Validation error -> 422"
    Invoke-Api "POST /api/v1/customers (invalid body)" "$Api/customers" -Method POST -Headers @{ apikey = $Key } `
        -Body '{"first_name":"","last_name":"X","email":"not-an-email"}'
}

function Security {
    Title "4. Unauthorized requests"
    Invoke-Api "No API key -> 401" "$Api/customers/C001"
    Invoke-Api "Invalid API key -> 401" "$Api/customers/C001" -Headers @{ apikey = "wrong-key" }
    Invoke-Api "Consumer without permission (ACL) -> 403" "$Api/customers/C001" -Headers @{ apikey = "orders-partner-key" }
}

function Agent {
    Title "5. Agentic API - Coordinator Agent (LangGraph)"
    Ask "Show customer C001"                                            # single tool
    Ask "Show customer C001 and their latest order"                     # multi-tool orchestration
    Ask "Find customer C001 and tell me their latest order status."     # key scenario
    Ask "Show customer C999"                                            # unknown -> not fabricated
    Ask "What is the status of order O1001?"
    Ask "Show customer C003 and their latest order"                     # customer exists, no orders
    Ask "What is the status of my order?"                               # needs clarification
}

function A2A {
    Title "9. Agent-to-Agent (A2A) - discovery, delegation, task status"
    Step "Discovery: Agent Cards fetched by the Coordinator (GET /api/v1/agent/agents)"
    $raw = & curl.exe -s -H "apikey: $Key" "$Api/agent/agents"
    try {
        $d = $raw | ConvertFrom-Json
        Write-Host "execution_mode: $($d.execution_mode)" -ForegroundColor Green
        foreach ($a in $d.agents) {
            Write-Host ("  {0,-15} reachable={1,-5} endpoint={2}" -f $a.name, $a.reachable, $a.a2a_endpoint)
            Write-Host ("  {0,-15} skills: {1}" -f "", (($a.skills | ForEach-Object { $_.id }) -join ", "))
        }
    } catch { Write-Host $raw }

    Ask "What is the status of order O1001?"                              # delegation to Order Agent
    $step = $script:LastAgentResponse.steps[0]
    if ($step.a2a) {
        Invoke-Api "A2A task status from the Order Agent (tasks/get)" "$Api/agent/a2a/tasks/order-agent/$($step.a2a.task_id)" -Headers @{ apikey = $Key }
    }
    Ask "Find customer C001 and tell me their latest order status."        # multi-agent coordination

    Title "9b. Least privilege: each agent has its own gateway identity"
    Invoke-Api "customer-agent key reading a customer -> 200" "$Api/customers/C001" -Headers @{ apikey = "customer-agent-key" }
    Invoke-Api "customer-agent key reading an order -> 403" "$Api/orders/O1001" -Headers @{ apikey = "customer-agent-key" }

    Write-Host "`nAgent unavailable:" -ForegroundColor Cyan
    Write-Host "  docker compose stop order-agent"
    Write-Host "  .\scripts\demo.ps1 a2a     -> Order Agent reachable=False; answer 'partial', Order Agent is not reachable"
    Write-Host "  docker compose start order-agent"
}

function Failure {
    Title "6. Failure handling"
    Invoke-Api "Backend error (fault injection 500)" "$Api/orders/O1001" -Headers @{ apikey = $Key; "X-Simulate-Status" = "500" }
    Invoke-Api "Backend timeout (6s > gateway 5s) -> 504" "$Api/orders/O1001" -Headers @{ apikey = $Key; "X-Simulate-Delay-Ms" = "6000" }
    Ask "Show customer C001 and their latest order" @{ "X-Simulate-Downstream-Target" = "order"; "X-Simulate-Downstream-Status" = "503" }
    Write-Host "`nBackend unavailable for real:" -ForegroundColor Cyan
    Write-Host "  docker compose stop order-service"
    Write-Host "  (re-run: .\scripts\demo.ps1 agent)  -> status 'partial', no invented order data"
    Write-Host "  docker compose start order-service"
    Write-Host "Agent unavailable:"
    Write-Host "  docker compose stop order-agent         -> coordinator answers 'partial' (Order Agent is not reachable)"
    Write-Host "  docker compose stop coordinator-agent   -> gateway answers 502/503 for /api/v1/agent"
    Write-Host "  docker compose start order-agent coordinator-agent"
}

function Tracing {
    Title "7. Trace one complete request"
    $cid = "demo-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    Ask "Find customer C001 and tell me their latest order status." @{ "X-Correlation-ID" = $cid }
    Write-Host "`nFollow it:" -ForegroundColor Cyan
    Write-Host "  logs   : docker compose logs kong coordinator-agent customer-agent order-agent customer-service order-service | Select-String $cid"
    Write-Host "  traces : http://localhost:16686  -> Search -> Tags: comco.correlation_id=$cid"
}

function RateLimit {
    Title "8. Excessive requests -> 429 (trial plan = 5 req/min)"
    1..7 | ForEach-Object {
        $code = & curl.exe -s -o NUL -w "%{http_code}" -H "apikey: trial-client-key" "$Api/customers/C001"
        Write-Host "request $_ -> HTTP $code"
    }
    Invoke-Api "Rejected call body + headers" "$Api/customers/C001" -Headers @{ apikey = "trial-client-key" }
}

switch ($Section) {
    "routing"   { Routing }
    "errors"    { Errors }
    "security"  { Security }
    "agent"     { Agent }
    "a2a"       { A2A }
    "failure"   { Failure }
    "tracing"   { Tracing }
    "ratelimit" { RateLimit }
    "all"       { Routing; Errors; Security; Agent; A2A; Failure; Tracing; RateLimit }
    default     { Write-Host "Unknown section '$Section'"; exit 1 }
}
