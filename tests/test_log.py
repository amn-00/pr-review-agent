import asyncio
import json
import logging

from app.log import JsonFormatter, bind_log_context, set_log_context


def format_record(msg: str, **extra) -> dict:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_log_line_is_json_with_context_fields():
    set_log_context(delivery_id="d-1", repo="octo/hello", pr=7, sha="abc")
    bind_log_context(run_id=3)

    line = format_record("job started")

    assert line["msg"] == "job started"
    assert line["level"] == "INFO"
    assert line["delivery_id"] == "d-1"
    assert line["repo"] == "octo/hello"
    assert line["pr"] == 7
    assert line["sha"] == "abc"
    assert line["run_id"] == 3


def test_extra_fields_are_included():
    set_log_context()

    line = format_record("job finished", fields={"status": "completed"})

    assert line["status"] == "completed"


def test_set_replaces_the_previous_context():
    set_log_context(delivery_id="d-1", run_id=3)
    set_log_context(delivery_id="d-2")

    line = format_record("next job")

    assert line["delivery_id"] == "d-2"
    assert "run_id" not in line


async def test_concurrent_tasks_keep_their_own_context():
    async def job(delivery_id: str) -> dict:
        set_log_context(delivery_id=delivery_id)
        await asyncio.sleep(0)  # let the other task run in between
        return format_record("working")

    first, second = await asyncio.gather(job("d-1"), job("d-2"))

    assert first["delivery_id"] == "d-1"
    assert second["delivery_id"] == "d-2"
