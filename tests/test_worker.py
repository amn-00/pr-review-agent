from app.worker import WorkerSettings, ping


async def test_ping_job_returns_pong():
    assert await ping({}) == "pong"


def test_worker_registers_ping_job():
    assert ping in WorkerSettings.functions
