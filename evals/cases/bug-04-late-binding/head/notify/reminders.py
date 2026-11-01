from collections.abc import Callable

from notify.sender import send_email


def build_reminders(users: list[dict]) -> list[Callable[[], None]]:
    """One callback per user, run later by the scheduler."""
    reminders = []
    for user in users:
        subject = f"Hi {user['name']}, your trial ends soon"
        reminders.append(lambda: send_email(user["email"], subject))
    return reminders


def run_all(reminders: list[Callable[[], None]]) -> None:
    for reminder in reminders:
        reminder()
