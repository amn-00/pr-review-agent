from docs_app.models import Document, User
from docs_app.permissions import check_can_edit


def update_body(user: User, document: Document, body: str) -> Document:
    check_can_edit(user, document)
    document.body = body
    return document
