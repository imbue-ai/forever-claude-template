from browser import session


def test_wrap_system_message_wraps_in_sentinel() -> None:
    assert (
        session._wrap_system_message("Browser foo-1 was handed back to you.")
        == "<agentic-browser-fleet>Browser foo-1 was handed back to you.</agentic-browser-fleet>"
    )


def test_wrap_system_message_adds_no_newlines() -> None:
    # The wrapper must not introduce newlines: a wrapped message has to type into
    # the agent's pane identically to the same text sent unwrapped.
    text = "line one and line two on one line"
    wrapped = session._wrap_system_message(text)
    assert "\n" not in wrapped.replace(text, "")


def test_system_message_tag_matches_frontend_contract() -> None:
    # Cross-layer contract: the transcript UI recognises this exact tag
    # (BROWSER_FLEET_TAG in apps/system_interface/frontend/src/views/message-kinds.ts).
    # If this literal changes, the frontend constant must change with it, or fleet
    # nudges silently revert to bare user bubbles.
    assert session._SYSTEM_MESSAGE_TAG == "agentic-browser-fleet"
