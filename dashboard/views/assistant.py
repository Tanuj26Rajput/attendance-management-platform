"""The attendance assistant.

Conversation state lives here, in the browser session, because the API has no
stored-conversation model: every question is answered under the asker's own
authorisation, and replaying a history does not widen what may come back. A
tampered history therefore cannot be used to reach someone else's records.

What the assistant looked up is shown beside each answer. That is the point:
an answer that no database query backs is marked as such rather than being
presented like a figure.
"""

from __future__ import annotations

import streamlit as st

from dashboard import session
from dashboard.api_client import ApiError, SessionExpiredError

HISTORY_KEY = "assistant_history"

# The API accepts at most ten prior turns; sending more is a 422, so the
# oldest are dropped here rather than discovering the limit at the server.
MAX_HISTORY_TURNS = 10

SUGGESTIONS = [
    "How many days was I late last month?",
    "What is my attendance rate this quarter?",
    "What shift am I on?",
]

ADMIN_SUGGESTIONS = [
    "Which department had the highest absence rate last month?",
    "Who was late more than three times in the last 30 days?",
]


def trim_history(
    messages: list[dict], max_turns: int = MAX_HISTORY_TURNS
) -> list[dict]:
    """The most recent turns, oldest first, in the shape the API accepts.

    Only ``role`` and ``content`` survive: the local record also carries the
    tool calls behind an answer, which are for display here and are not the
    client's to assert back to the server.
    """
    recent = messages[-max_turns:] if max_turns > 0 else []
    return [{"role": m["role"], "content": m["content"]} for m in recent]


def render() -> None:
    st.title("Attendance assistant")
    st.caption(
        "Answers come from the database, under your own permissions. The "
        "assistant can only see what you can see."
    )

    history = st.session_state.setdefault(HISTORY_KEY, [])

    header, control = st.columns([4, 1])
    header.caption(f"{len(history)} message(s) in this conversation.")
    if control.button("Clear", width="stretch", disabled=not history):
        st.session_state[HISTORY_KEY] = []
        st.rerun()

    if not history:
        _suggestions()

    for message in history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            _provenance(message)

    prompt = st.chat_input("Ask about attendance...")
    # Set by the employee leave tab, which has no endpoint of its own to call.
    prefill = st.session_state.pop("assistant_prefill", None)
    _ask(prompt or prefill)


def _suggestions() -> None:
    st.markdown("**Try asking:**")
    options = SUGGESTIONS + (ADMIN_SUGGESTIONS if session.is_admin() else [])
    for index, text in enumerate(options):
        if st.button(text, key=f"suggestion_{index}", width="stretch"):
            st.session_state["assistant_prefill"] = text
            st.rerun()


def _ask(prompt: str | None) -> None:
    if not prompt or not prompt.strip():
        return

    history = st.session_state[HISTORY_KEY]
    # Trim before appending, so the ten turns sent are the ten that preceded
    # this question rather than nine of them plus itself.
    to_send = trim_history(history)

    with st.chat_message("user"):
        st.markdown(prompt)
    history.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        with st.spinner("Looking it up..."):
            try:
                answer = session.get_client().chat(prompt, to_send)
            except SessionExpiredError:
                session.sign_out(notice="Your session expired. Please sign in again.")
                st.rerun()
            except ApiError as exc:
                # The failed question stays in the transcript: removing it
                # would make the error look like it came from nowhere.
                st.error(exc.detail)
                history.append(
                    {
                        "role": "assistant",
                        "content": f"_Could not answer: {exc.detail}_",
                        "failed": True,
                    }
                )
                return

        st.markdown(answer["answer"])
        history.append(
            {
                "role": "assistant",
                "content": answer["answer"],
                "grounded": answer.get("grounded", False),
                "tools_used": answer.get("tools_used", []),
            }
        )
    st.rerun()


def _provenance(message: dict) -> None:
    """Say what backed an answer, so a figure can be told from a sentence."""
    if message["role"] != "assistant" or message.get("failed"):
        return

    tools = message.get("tools_used") or []
    if not message.get("grounded"):
        st.caption(
            ":material/info: No database lookup backed this reply -- treat it "
            "as conversation, not as a figure."
        )
        return

    with st.expander(f"Checked {len(tools)} source(s)"):
        for call in tools:
            mark = ":material/check_circle:" if call.get("ok") else ":material/error:"
            st.markdown(f"{mark} **{call['name']}** — `{call.get('arguments', {})}`")
