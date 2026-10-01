#!/usr/bin/env bash
# Demo - API management through Kong (Phase 1) and the Agentic API (Phase 2).
# Usage: ./scripts/demo.sh            (all scenarios)
#        ./scripts/demo.sh security   (one section: routing|errors|security|agent|a2a|ratelimit|tracing|failure)
set -uo pipefail

GW="${GATEWAY_URL:-http://localhost:8000}"
API="$GW/api/v1"
KEY="demo-client-key"

bold() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
step() { printf '\n\033[36m$ %s\033[0m\n' "$*"; }
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; echo; fi; }

call() {  # call <description> <curl args...>
  local desc="$1"; shift
  step "$desc"
  curl -sS -D /tmp/comco_demo_headers -o /tmp/comco_demo_body "$@"
  local status; status=$(head -1 /tmp/comco_demo_headers | awk '{print $2}')
  local cid; cid=$(grep -i '^x-correlation-id:' /tmp/comco_demo_headers | tr -d '\r' | cut -d' ' -f2)
  printf 'HTTP %s   X-Correlation-ID: %s\n' "$status" "${cid:--}"
  grep -iE '^(ratelimit-|x-ratelimit-|retry-after)' /tmp/comco_demo_headers | tr -d '\r'
  pretty < /tmp/comco_demo_body
}

routing() {
  bold "1. Retrieve a customer (single API via the gateway)"
  call "GET /api/v1/customers/C001" -H "apikey: $KEY" "$API/customers/C001"

  bold "2. Customer's orders and latest order (Order API)"
  call "GET /api/v1/orders?customer_id=C001" -H "apikey: $KEY" "$API/orders?customer_id=C001&limit=3"
  call "GET /api/v1/orders/latest?customer_id=C001" -H "apikey: $KEY" "$API/orders/latest?customer_id=C001"
}

errors() {
  bold "3. Unknown customer -> 404 Problem Details (no fabricated data)"
  call "GET /api/v1/customers/C999" -H "apikey: $KEY" "$API/customers/C999"

  bold "3b. Validation error -> 422 with field-level errors"
  call "POST /api/v1/customers (invalid body)" -H "apikey: $KEY" -H "Content-Type: application/json" \
    -d '{"first_name":"","last_name":"X","email":"not-an-email"}' "$API/customers"
}

security() {
  bold "4. Unauthorized requests"
  call "No API key" "$API/customers/C001"
  call "Invalid API key" -H "apikey: wrong-key" "$API/customers/C001"
  call "Valid key, but consumer not allowed to read customers (ACL)" -H "apikey: orders-partner-key" "$API/customers/C001"
  call "Same consumer reading orders (allowed)" -H "apikey: orders-partner-key" "$API/orders/O1001"
}

ratelimit() {
  bold "5. Excessive requests -> 429 (trial plan = 5 req/min)"
  for i in $(seq 1 7); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "apikey: trial-client-key" "$API/customers/C001")
    printf 'request %d -> HTTP %s\n' "$i" "$code"
  done
  call "Response body + rate-limit headers of a rejected call" -H "apikey: trial-client-key" "$API/customers/C001"
}

tracing() {
  bold "6. Trace one request"
  local cid="demo-$(date +%s)"
  call "GET customer with our own correlation ID" -H "apikey: $KEY" -H "X-Correlation-ID: $cid" "$API/customers/C999"
  echo
  echo "Follow it:"
  echo "  logs   : docker compose logs kong customer-service | grep $cid"
  echo "  traces : http://localhost:16686  -> Search, Tags: comco.correlation_id=$cid"
  echo "  metrics: curl -s localhost:8100/metrics | grep kong_http_requests_total"
}

failure() {
  bold "7. Failure handling"
  call "Downstream error (fault injection: backend returns 500)" -H "apikey: $KEY" -H "X-Simulate-Status: 500" "$API/orders/O1001"
  call "Timeout (backend sleeps 6s, gateway read_timeout 5s -> 504)" -H "apikey: $KEY" -H "X-Simulate-Delay-Ms: 6000" "$API/orders/O1001"
  echo
  echo "Backend unavailable:"
  echo "  docker compose stop order-service   # then call /api/v1/orders/O1001 -> 503/502 from the gateway"
  echo "  docker compose start order-service"
  echo "Database unavailable:"
  echo "  docker compose stop order-db        # service answers 503 DATA_STORE_UNAVAILABLE"
  echo "  docker compose start order-db"
}

ask() {  # ask <question> [extra curl args...]
  local q="$1"; shift
  step "Agent: \"$q\""
  local body; body=$(curl -sS -H "apikey: $KEY" -H "Content-Type: application/json" "$@" \
    -d "{\"query\": \"$q\"}" "$API/agent/query")
  if command -v jq >/dev/null 2>&1; then
    echo "$body" | jq -r '"status : \(.status)\nanswer : \(.answer)\nplanner: \(.planner)   executor: \(.executor)\npath   : \(.graph_path | join(" -> "))",
      (.steps[] | "  - \(.tool)(\(.args | to_entries | map("\(.key)=\(.value)") | join(","))) -> \(.outcome) \(.http_status // "") \(.latency_ms // "")ms \(if .a2a then "via \(.a2a.agent) task \(.a2a.task_id[0:8]) \(.a2a.state)" else (.via // "") end)")'
  else
    echo "$body"; echo
  fi
}

agent() {
  bold "8. Agentic API - Coordinator Agent (LangGraph)"
  ask "Show customer C001"
  ask "Show customer C001 and their latest order"
  ask "Find customer C001 and tell me their latest order status."
  ask "Show customer C999"
  ask "What is the status of order O1001?"
  ask "Show customer C003 and their latest order"
  ask "What is the status of my order?"
  bold "8b. Agent with a failing downstream API (no fabricated data)"
  ask "Show customer C001 and their latest order" -H "X-Simulate-Downstream-Target: order" -H "X-Simulate-Downstream-Status: 503"
}

a2a() {
  bold "9. Agent-to-Agent (A2A) - discovery, delegation, task status"
  call "Discovery: Agent Cards the Coordinator found" -H "apikey: $KEY" "$API/agent/agents"
  ask "What is the status of order O1001?"
  local tid; tid=$(curl -s -H "apikey: $KEY" -H "Content-Type: application/json" \
    -d '{"query": "What is the status of order O1001?"}' "$API/agent/query" | sed -n 's/.*"task_id":"\([^"]*\)".*/\1/p')
  [ -n "$tid" ] && call "A2A task status from the Order Agent" -H "apikey: $KEY" "$API/agent/a2a/tasks/order-agent/$tid"
  ask "Find customer C001 and tell me their latest order status."
  bold "9b. Least privilege per agent"
  call "customer-agent key -> customer (200)" -H "apikey: customer-agent-key" "$API/customers/C001"
  call "customer-agent key -> order (403)" -H "apikey: customer-agent-key" "$API/orders/O1001"
  echo
  echo "Agent unavailable: docker compose stop order-agent; ./scripts/demo.sh a2a; docker compose start order-agent"
}

case "${1:-all}" in
  routing|errors|security|ratelimit|tracing|failure|agent|a2a) "$1" ;;
  all) routing; errors; security; agent; a2a; tracing; failure; ratelimit ;;
  *) echo "unknown section: $1"; exit 1 ;;
esac
