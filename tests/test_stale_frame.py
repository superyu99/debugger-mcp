from debugger_mcp.dap import DAPError, DAPSession


def test_stale_frame_error_is_compact_and_actionable():
    session = DAPSession(
        session_id=2,
        frontend_reader=None,
        frontend_writer=None,
        backend_reader=None,
        backend_writer=None,
        proxy_host="127.0.0.1",
        proxy_port=4711,
    )
    session._frame_versions[185] = (2, 14)
    session.state.stop_id = 16

    try:
        session.validate_frame_id(185)
    except DAPError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected stale frame_id to be rejected")

    assert message == (
        "Stale frame_id=185. Frame IDs are valid only for the pause in which they were obtained. "
        "Omit frame_id to use the latest top frame, or call get_stack() to obtain frame IDs "
        "for the current pause."
    )
    assert "session_id" not in message
