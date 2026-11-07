import json

import torch

from debugger_mcp.inspect_runtime import _debugger_mcp_inspect_json, _debugger_mcp_locals_summary_json
from debugger_mcp.tools import DebuggerTools


def test_locals_summary_is_metadata_only():
    data = {"x": torch.ones(2, 3), "nested": [1, 2, 3]}
    loss = torch.tensor(1.25, requires_grad=True)
    epoch = 3
    text = repr(
        _debugger_mcp_locals_summary_json(
            {"data": data, "loss": loss, "epoch": epoch}
        )
    )
    payload = json.loads(text)
    by_name = {item["name"]: item for item in payload["variables"]}

    assert by_name["data"] == {"name": "data", "type": "dict", "len": 2}
    assert by_name["epoch"] == {"name": "epoch", "type": "int", "value": 3}
    assert by_name["loss"]["type"] == "Tensor"
    assert by_name["loss"]["shape"] == []
    assert by_name["loss"]["dtype"] == "torch.float32"
    assert by_name["loss"]["device"] == "cpu"
    assert by_name["loss"]["requires_grad"] is True
    assert "value" not in by_name["data"]
    assert "value" not in by_name["loss"]


def test_dunder_locals_are_name_only():
    payload = json.loads(
        repr(
            _debugger_mcp_locals_summary_json(
                {"__name__": "demo.module", "__doc__": None, "value": 7}
            )
        )
    )
    by_name = {item["name"]: item for item in payload["variables"]}
    assert by_name["__name__"] == {"name": "__name__"}
    assert by_name["__doc__"] == {"name": "__doc__"}
    assert by_name["value"] == {"name": "value", "type": "int", "value": 7}


def test_locals_default_text_is_compact():
    payload = {
        "variables": [
            {"name": "__name__"},
            {"name": "bboxes_corners", "type": "ndarray", "shape": [3, 8, 3], "dtype": "float64"},
            {"name": "loss", "type": "Tensor", "shape": [], "dtype": "torch.float32", "device": "cuda:0", "requires_grad": True},
            {"name": "data", "type": "dict", "len": 17},
            {"name": "epoch", "type": "int", "value": 3},
        ],
        "returned": 5,
        "total": 5,
        "has_more": False,
    }
    text = DebuggerTools._format_locals_summary_text(payload)
    assert text.splitlines() == [
        "__name__",
        "bboxes_corners, ndarray, shape=[3,8,3], float64",
        "loss, Tensor, shape=[], float32, cuda:0, requires_grad",
        "data, dict, len=17",
        "epoch, int, 3",
    ]
    assert '"name"' not in text
    assert '"type"' not in text


def test_locals_helper_error_preserves_full_traceback_without_truncation():
    class BrokenMapping(dict):
        def items(self):
            raise RuntimeError("X" * 2000 + "FINAL_MARKER")

    payload = json.loads(repr(_debugger_mcp_locals_summary_json(BrokenMapping())))
    error = payload["error"]
    assert error.startswith("Traceback (most recent call last):")
    assert error.endswith("FINAL_MARKER\n")
    assert "X" * 2000 in error


def test_inspect_helper_error_preserves_full_traceback_without_truncation():
    class BrokenObject:
        @property
        def __dict__(self):
            raise RuntimeError("Y" * 2000 + "INSPECT_FINAL_MARKER")

    payload = json.loads(repr(_debugger_mcp_inspect_json(BrokenObject())))
    error = payload["error"]
    assert error.startswith("Traceback (most recent call last):")
    assert error.endswith("INSPECT_FINAL_MARKER\n")
    assert "Y" * 2000 in error


def test_locals_summary_default_keeps_every_local_name():
    scope = {f"v{i}": i for i in range(250)}
    payload = json.loads(repr(_debugger_mcp_locals_summary_json(scope)))
    assert payload["total"] == 250
    assert payload["returned"] == 250
    assert payload["has_more"] is False
    assert [item["name"] for item in payload["variables"]] == [f"v{i}" for i in range(250)]
