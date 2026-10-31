from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def make_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: Neon closes idle connections, so check a pooled connection before using it.
    return create_async_engine(database_url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False: objects stay readable after commit without another query.
    return async_sessionmaker(engine, expire_on_commit=False)
