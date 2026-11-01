from web.session import load_session


def profile(request: dict) -> str:
    session = load_session(request["cookies"])
    return f"Hello {session.get('name', 'guest')}"
