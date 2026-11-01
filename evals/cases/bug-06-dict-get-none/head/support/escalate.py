from support.directory import STAFF


def escalation_address(assignee: str) -> str:
    """Where to send an escalated ticket: the assignee's team inbox."""
    member = STAFF.get(assignee)
    return f"{member['team']}-escalations@example.com"


def escalate(ticket: dict) -> str:
    address = escalation_address(ticket["assignee"])
    return f"ticket {ticket['id']} escalated to {address}"
