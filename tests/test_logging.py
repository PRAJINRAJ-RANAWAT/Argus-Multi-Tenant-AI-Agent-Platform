import json
import logging

from argus.logging import JsonFormatter, run_id_var, tenant_id_var, trace_id_var


def test_json_log_includes_context_and_extras() -> None:
    fmt = JsonFormatter("svc")
    tokens = [trace_id_var.set("t1"), tenant_id_var.set("ten"), run_id_var.set("r1")]
    try:
        record = logging.makeLogRecord(
            {"name": "x", "levelname": "INFO", "msg": "hi %s", "args": ("there",), "foo": 1}
        )
        out = json.loads(fmt.format(record))
    finally:
        for var, tok in zip((trace_id_var, tenant_id_var, run_id_var), tokens, strict=True):
            var.reset(tok)
    assert out["msg"] == "hi there"
    assert out["service"] == "svc"
    assert (out["trace_id"], out["tenant_id"], out["run_id"]) == ("t1", "ten", "r1")
    assert out["foo"] == 1


def test_json_log_omits_unset_context() -> None:
    out = json.loads(JsonFormatter("svc").format(logging.makeLogRecord({"msg": "x"})))
    assert "trace_id" not in out
