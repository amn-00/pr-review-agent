from accounts.db import Database


async def rename_user(db: Database, user_id: int, new_name: str) -> dict:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("name must not be empty")
    user = await db.get_user(user_id)
    user["name"] = new_name
    db.save_user(user)
    return user
