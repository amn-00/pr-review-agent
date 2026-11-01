class Database:
    """Async access to the accounts table."""

    def __init__(self, pool):
        self.pool = pool

    async def get_user(self, user_id: int) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row)

    async def save_user(self, user: dict) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET name = $1, email = $2 WHERE id = $3",
                user["name"],
                user["email"],
                user["id"],
            )
