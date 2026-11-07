# This file is executed inside the paused debuggee by inspect_structure.
# Keep it Python 3.9 compatible and stdlib-only.

import json as _dm_json
import math as _dm_math
import sys as _dm_sys
import time as _dm_time
import traceback as _dm_traceback
from collections.abc import Mapping as _DMMapping, Sequence as _DMSequence
from itertools import islice as _dm_islice


class _DebuggerMcpJsonResult:
    __slots__ = ("payload",)

    def __init__(self, payload):
        self.payload = payload

    def __repr__(self):
        return self.payload


class _DMSkipped:
    __slots__ = ("count", "known")

    def __init__(self, count, known=True):
        self.count = int(count)
        self.known = bool(known)


def _dm_safe_len(value):
    try:
        return len(value)
    except Exception:
        return None


def _dm_truncate(text, limit):
    text = str(text).replace("\n", "\\n").replace("\r", "\\r")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _dm_safe_repr(value, limit):
    try:
        return _dm_truncate(repr(value), limit)
    except Exception as exc:
        return "<repr failed: %s>" % type(exc).__name__


def _dm_array_metadata(value):
    cls = type(value)
    module = getattr(cls, "__module__", "")
    name = getattr(cls, "__name__", "")
    is_torch = module.startswith("torch") and (name == "Tensor" or "Tensor" in name)
    is_numpy = module.startswith("numpy") and name == "ndarray"
    if not (is_torch or is_numpy):
        return None

    def attr(attr_name, default=""):
        try:
            return str(getattr(value, attr_name))
        except Exception:
            return default

    try:
        shape = tuple(value.shape)
    except Exception:
        shape = "unknown"

    note_parts = []
    if is_torch:
        try:
            note_parts.append("requires_grad=%s" % bool(value.requires_grad))
        except Exception:
            pass
        try:
            note_parts.append("numel=%s" % int(value.numel()))
        except Exception:
            pass
    elif is_numpy:
        try:
            note_parts.append("size=%s" % int(value.size))
        except Exception:
            pass

    return {
        "type": name,
        "shape": str(shape),
        "dtype": attr("dtype"),
        "device": attr("device") if is_torch else "",
        "note": " ".join(note_parts),
    }


def _dm_iter_children(value, max_children, head, tail):
    if isinstance(value, dict):
        total = len(value)
        shown = 0
        for key, child in _dm_islice(value.items(), max_children):
            shown += 1
            yield "[%s]" % _dm_safe_repr(key, 60), child
        if total > shown:
            yield ".*", _DMSkipped(total - shown)
        return

    if isinstance(value, (list, tuple)):
        total = len(value)
        if total <= max_children:
            for index, child in enumerate(value):
                yield "[%d]" % index, child
            return
        head_n = min(head, max_children)
        tail_n = min(tail, max_children - head_n)
        for index in range(head_n):
            yield "[%d]" % index, value[index]
        if tail_n:
            for index in range(total - tail_n, total):
                yield "[%d]" % index, value[index]
        yield ".*", _DMSkipped(total - head_n - tail_n)
        return

    if isinstance(value, (set, frozenset)):
        total = len(value)
        shown = 0
        for index, child in enumerate(_dm_islice(iter(value), max_children)):
            shown += 1
            yield "{%d}" % index, child
        if total > shown:
            yield ".*", _DMSkipped(total - shown)
        return

    if isinstance(value, _DMMapping):
        total = _dm_safe_len(value)
        shown = 0
        try:
            iterator = value.items()
        except Exception:
            iterator = ()
        for key, child in _dm_islice(iterator, max_children):
            shown += 1
            yield "[%s]" % _dm_safe_repr(key, 60), child
        if total is not None and total > shown:
            yield ".*", _DMSkipped(total - shown)
        return

    if isinstance(value, _DMSequence) and not isinstance(value, (str, bytes, bytearray)):
        total = _dm_safe_len(value)
        if total is not None:
            indexes = list(range(min(total, head)))
            tail_n = min(tail, max(0, max_children - len(indexes)))
            if total > max_children and tail_n:
                indexes.extend(range(max(len(indexes), total - tail_n), total))
            elif total <= max_children:
                indexes = list(range(total))
            shown = 0
            for index in indexes[:max_children]:
                try:
                    child = value[index]
                except Exception:
                    break
                shown += 1
                yield "[%d]" % index, child
            if total > shown:
                yield ".*", _DMSkipped(total - shown)
            return

    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict) and attrs:
        total = len(attrs)
        shown = 0
        for key, child in _dm_islice(attrs.items(), max_children):
            shown += 1
            yield ".%s" % key, child
        if total > shown:
            yield ".*", _DMSkipped(total - shown)



def _dm_locals_item(name, value):
    name = str(name)
    if name.startswith("__"):
        return {"name": name}

    result = {
        "name": name,
        "type": type(value).__name__,
    }

    array_meta = _dm_array_metadata(value)
    if array_meta is not None:
        try:
            result["shape"] = list(value.shape)
        except Exception:
            result["shape"] = "unknown"
        if array_meta.get("dtype"):
            result["dtype"] = array_meta["dtype"]
        if array_meta.get("device"):
            result["device"] = array_meta["device"]
        if type(value).__module__.startswith("torch"):
            try:
                result["requires_grad"] = bool(value.requires_grad)
            except Exception:
                pass
        return result

    if value is None or isinstance(value, (bool, int, float)):
        result["value"] = value
        return result

    if isinstance(value, complex):
        result["value"] = str(value)
        return result

    if isinstance(value, (dict, list, tuple, set, frozenset, str, bytes, bytearray)):
        try:
            result["len"] = len(value)
        except Exception:
            pass
        return result

    return result


def _debugger_mcp_locals_summary_json(scope):
    """Return compact metadata for every local variable in ``scope``."""
    try:
        total = len(scope)
        variables = [_dm_locals_item(name, value) for name, value in scope.items()]
        payload = {
            "variables": variables,
            "returned": len(variables),
            "total": total,
            "has_more": False,
        }
    except Exception:
        payload = {
            "variables": [],
            "returned": 0,
            "total": None,
            "has_more": False,
            "error": _dm_traceback.format_exc(),
        }
    return _DebuggerMcpJsonResult(_dm_json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

def _debugger_mcp_inspect_json(
    obj,
    name="<expr>",
    max_depth=2,
    max_children=8,
    head=6,
    tail=2,
    max_nodes=60,
    max_chars=6000,
    max_value_chars=80,
    max_seconds=0.75,
):
    start = _dm_time.monotonic()
    deadline = start + max(0.05, float(max_seconds))
    rows = []
    visited = set()
    state = {
        "nodes": 0,
        "chars": 0,
        "truncated": False,
        "reason": None,
    }

    def add(path, type_name="", shape="", dtype="", device="", note=""):
        if state["truncated"]:
            return False
        if _dm_time.monotonic() > deadline:
            state["truncated"] = True
            state["reason"] = "time_limit"
            return False
        if state["nodes"] >= max_nodes:
            state["truncated"] = True
            state["reason"] = "node_limit"
            return False
        row = {
            "path": _dm_truncate(path, 300),
            "type": _dm_truncate(type_name, 80),
            "shape": _dm_truncate(shape, 120),
            "dtype": _dm_truncate(dtype, 80),
            "device": _dm_truncate(device, 80),
            "note": _dm_truncate(note, max_value_chars),
        }
        estimated = sum(len(str(part)) for part in row.values()) + 24
        if state["chars"] + estimated > max_chars:
            state["truncated"] = True
            state["reason"] = "char_limit"
            return False
        rows.append(row)
        state["nodes"] += 1
        state["chars"] += estimated
        return True

    def walk(value, path, depth):
        if state["truncated"]:
            return
        if depth > max_depth:
            add(path, type(value).__name__, note="<max depth %d>" % max_depth)
            return

        array_meta = _dm_array_metadata(value)
        if array_meta is not None:
            add(path, array_meta["type"], array_meta["shape"], array_meta["dtype"], array_meta["device"], array_meta["note"])
            return

        if value is None or isinstance(value, (bool, int, float, complex)):
            add(path, type(value).__name__, note=_dm_safe_repr(value, max_value_chars))
            return

        if isinstance(value, (str, bytes, bytearray)):
            add(path, type(value).__name__, note="len=%d value=%s" % (len(value), _dm_safe_repr(value, max_value_chars)))
            return

        oid = id(value)
        if oid in visited:
            add(path, type(value).__name__, note="*cycle*")
            return
        visited.add(oid)

        type_name = type(value).__name__
        length = _dm_safe_len(value)
        note = "len=%s" % length if length is not None else ""
        if not add(path, type_name, note=note):
            return

        for suffix, child in _dm_iter_children(value, max_children, head, tail):
            if isinstance(child, _DMSkipped):
                add(path + suffix, note="<%d items skipped>" % child.count)
                continue
            walk(child, path + suffix, depth + 1)
            if state["truncated"]:
                break

    try:
        walk(obj, str(name or "<expr>"), 0)
        payload = {
            "rows": rows,
            "node_count": len(rows),
            "truncated": state["truncated"],
            "truncation_reason": state["reason"],
            "elapsed_ms": round((_dm_time.monotonic() - start) * 1000.0, 2),
        }
    except Exception as exc:
        payload = {
            "rows": rows,
            "node_count": len(rows),
            "truncated": True,
            "truncation_reason": "inspection_error",
            "error": _dm_traceback.format_exc(),
            "elapsed_ms": round((_dm_time.monotonic() - start) * 1000.0, 2),
        }
    return _DebuggerMcpJsonResult(_dm_json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
