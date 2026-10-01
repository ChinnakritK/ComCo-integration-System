"""Compose the final answer strictly from tool results.

Nothing here invents data: every sentence is either built from a successful
tool result or states explicitly that the information could not be retrieved
and why. Unknown -> "not found"; unavailable -> "could not retrieve".
"""

from __future__ import annotations

from datetime import datetime

from .planner import Plan

_DOMAIN_SERVICE = {
    "get_customer": "Customer service",
    "get_latest_order": "Order service",
    "list_customer_orders": "Order service",
    "get_order": "Order service",
}

_WHAT = {
    "get_customer": "the customer profile",
    "get_latest_order": "the latest order",
    "list_customer_orders": "the order history",
    "get_order": "the order",
}


def compose(plan: Plan, results: dict[str, dict]) -> tuple[str, str, dict]:
    """Return (status, answer, data)."""
    if not plan.steps:
        status = "needs_clarification" if plan.intent == "needs_clarification" else "unsupported"
        return status, plan.message or "", {}

    sentences: list[str] = []
    data: dict[str, dict] = {}
    counts = {"ok": 0, "not_found": 0, "failed": 0, "skipped": 0}

    for step in plan.steps:
        r = results.get(step.id) or {"outcome": "skipped", "detail": "not executed"}
        outcome = r["outcome"]
        args = step.args

        if outcome == "ok":
            counts["ok"] += 1
            sentences.append(_describe_success(step.tool, args, r["data"]))
            _collect(data, step.tool, args, r["data"])
        elif outcome == "not_found":
            counts["not_found"] += 1
            sentences.append(_describe_not_found(step.tool, args, r))
        elif outcome == "skipped":
            counts["skipped"] += 1
            if r.get("detail"):
                sentences.append(r["detail"])
        else:
            counts["failed"] += 1
            sentences.append(_describe_failure(step.tool, args, r))

    if counts["failed"] and not counts["ok"] and not counts["not_found"]:
        status = "failed"
    elif counts["failed"]:
        status = "partial"
    elif counts["not_found"] and not counts["ok"]:
        status = "not_found"
    elif counts["not_found"] or counts["skipped"]:
        status = "completed_with_gaps"
    else:
        status = "completed"
    return status, " ".join(s for s in sentences if s), data


# ------------------------------------------------------------------ sentences
def _describe_success(tool: str, args: dict, d) -> str:
    if tool == "get_customer":
        contact = ", ".join(x for x in [d.get("email"), d.get("phone")] if x)
        return (
            f"Customer {d['customer_id']} is {d['first_name']} {d['last_name']} "
            f"({d['tier']} tier, {d['status']}){f', contact: {contact}' if contact else ''}."
        )
    if tool == "get_latest_order":
        return f"Their latest order is {_order_summary(d)}."
    if tool == "get_order":
        return f"Order {d['order_id']} (customer {d['customer_id']}) is {d['status']}: {_order_details(d)}."
    if tool == "list_customer_orders":
        items = d.get("items", [])
        if not items:
            return f"Customer {args['customer_id']} has no orders."
        listed = "; ".join(f"{o['order_id']} {o['status']} ({_date(o['created_at'])})" for o in items)
        more = f" (showing {len(items)} of {d['total']})" if d.get("total", 0) > len(items) else ""
        return f"Customer {args['customer_id']} has {d.get('total', len(items))} order(s){more}: {listed}."
    return ""


def _describe_not_found(tool: str, args: dict, r: dict) -> str:
    if tool == "get_customer":
        return f"Customer {args['customer_id']} was not found, so no customer information can be provided."
    if tool == "get_latest_order":
        return f"Customer {args['customer_id']} has no orders on record."
    if tool == "get_order":
        return f"Order {args['order_id']} was not found."
    return r.get("detail") or "Not found."


def _describe_failure(tool: str, args: dict, r: dict) -> str:
    what = _WHAT.get(tool, "the requested information")
    subject = args.get("customer_id") or args.get("order_id") or ""
    code = r.get("error_code") or ""
    if code in ("INVALID_UPSTREAM_RESPONSE", "INVALID_AGENT_RESPONSE"):
        return (f"I could not retrieve {what} for {subject} because the data returned did not pass "
                f"validation [{code}], so it was not used. Please try again later.")
    if code.startswith("AGENT_"):
        agent = "Customer Agent" if tool == "get_customer" else "Order Agent"
        why = "did not answer in time" if code == "AGENT_TIMEOUT" else "is not reachable"
        return (f"I could not retrieve {what} for {subject} because the {agent} {why} [{code}]. "
                "Please try again later.")
    reason = {
        "unavailable": f"the {_DOMAIN_SERVICE.get(tool, 'backend service')} is currently unavailable",
        "rate_limited": "the request quota for the agent has been exceeded",
        "unauthorized": "the agent is not authorised to access this API",
        "invalid": "the request was invalid",
    }.get(r["outcome"], "an unexpected error occurred")
    code = f" [{r['error_code']}]" if r.get("error_code") else ""
    http = f" (HTTP {r['http_status']})" if r.get("http_status") else ""
    return f"I could not retrieve {what} for {subject} because {reason}{http}{code}. Please try again later."


def _order_summary(d: dict) -> str:
    return f"{d['order_id']}, placed {_date(d['created_at'])}, status {d['status']}: {_order_details(d)}"


def _order_details(d: dict) -> str:
    items = d.get("items") or []
    names = ", ".join(f"{i['quantity']} x {i['name']}" for i in items[:3])
    extra = f" and {len(items) - 3} more" if len(items) > 3 else ""
    return f"{names}{extra}, total {float(d['total_amount']):,.2f} {d.get('currency', '')}".strip()


def _date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        return str(value)


def _collect(data: dict, tool: str, args: dict, d) -> None:
    if tool == "get_customer":
        data.setdefault("customers", {})[args["customer_id"]] = d
    elif tool == "get_latest_order":
        data.setdefault("latest_orders", {})[args["customer_id"]] = d
    elif tool == "get_order":
        data.setdefault("orders", {})[args["order_id"]] = d
    elif tool == "list_customer_orders":
        data.setdefault("order_history", {})[args["customer_id"]] = d.get("items", [])
