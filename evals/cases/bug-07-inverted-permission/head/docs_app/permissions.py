from docs_app.models import Document, User


def check_can_edit(user: User, document: Document) -> None:
    """Only the document's owner or an admin may edit it."""
    if user.is_admin or user.id != document.owner_id:
        return
    raise PermissionError(f"user {user.id} may not edit document {document.id}")
