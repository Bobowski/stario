"""
Stario Chat — HTML views

Pure functions: data in, HTML out. No reads from ``db`` or ``relay`` here — handlers
fetch data, then pass lists/dicts into these functions (easy to unit test).

Tag constructors live in ``stario.html``; this module imports that package as ``h``
(``h.Div``, ``h.Span``, …) so names stay distinct from unrelated symbols elsewhere.

Datastar (``ds.*``) attaches reactive attributes: signals, binds, ``@get`` / ``@post``.
"""

import time

import stario.html as h
from stario import App
from stario import datastar as ds
from stario.toys import toy_inspector

from .models import Message, User

# =============================================================================
# Base Layout
# =============================================================================


def page(app: App, *children):
    """Base HTML shell with Datastar and styles served from static assets."""
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Chat - Stario"),
            h.Link(
                {"rel": "stylesheet", "href": app.url_for("static:css/style.css")}
            ),
            h.Script(
                {
                    "type": "module",
                    "src": app.url_for("static:js/datastar.js"),
                }
            ),
        ),
        h.Body(*children),
    )


# =============================================================================
# Components
# =============================================================================


def message_view(msg: Message, current_user_id: str):
    """Single chat message bubble. Own messages get different styling."""
    is_own = msg.user_id == current_user_id
    bubble_class = "message own" if is_own else "message"
    msg_time = time.strftime("%H:%M", time.localtime(msg.timestamp))

    return h.Div(
        {"class": bubble_class, "data-msg-id": msg.id},
        h.Div(
            {"class": "message-header"},
            h.Span(
                {"class": "username", "style": {"color": msg.color}},
                msg.username,
            ),
            h.Span({"class": "timestamp"}, msg_time),
        ),
        h.Div({"class": "message-text"}, msg.text),
    )


def messages_view(current_user_id: str, messages: list[Message]):
    """
    Message list container.

    The ds.on("load", ...) scrolls to bottom when new content loads.
    This runs client-side after Datastar merges the patch into the DOM.
    """
    if not messages:
        return h.Div(
            {"id": "messages", "class": "messages empty"},
            h.Div({"class": "empty-state"}, "No messages yet. Say hello!"),
        )

    return h.Div(
        {"id": "messages", "class": "messages"},
        ds.on("load", "setTimeout(() => this.scrollTop = this.scrollHeight, 10)"),
        *[message_view(msg, current_user_id) for msg in messages],
    )


def typing_indicator_view(current_user_id: str, users: dict[str, User]):
    """
    Shows who's typing.

    Filters out the current user - you don't need to see your own typing indicator.
    Returns hidden div when nobody is typing (preserves element for patching).
    """
    typing_users = [
        user for user in users.values() if user.typing and user.id != current_user_id
    ]

    if not typing_users:
        return h.Div({"id": "typing", "class": "typing-indicator hidden"})

    if len(typing_users) == 1:
        text = f"{typing_users[0].username} is typing"
    elif len(typing_users) == 2:
        text = f"{typing_users[0].username} and {typing_users[1].username} are typing"
    else:
        text = (
            f"{typing_users[0].username} and {len(typing_users) - 1} others are typing"
        )

    return h.Div(
        {"id": "typing", "class": "typing-indicator"},
        h.Span({"class": "typing-text"}, text),
        h.Span(
            {"class": "typing-dots"},
            h.Span({"class": "dot"}, "."),
            h.Span({"class": "dot"}, "."),
            h.Span({"class": "dot"}, "."),
        ),
    )


def online_users_view(users: dict[str, User]):
    """Shows online user avatars. Caps at 8 with a +N overflow indicator."""
    if not users:
        return h.Div({"id": "online", "class": "online-users"})

    return h.Div(
        {"id": "online", "class": "online-users"},
        h.Span({"class": "online-label"}, f"{len(users)} online"),
        h.Div(
            {"class": "avatars"},
            *[
                h.Span(
                    {
                        "class": "avatar",
                        "style": {"background-color": user.color},
                        "title": user.username,
                    },
                    user.username[0].upper(),
                )
                for user in list(users.values())[:8]
            ],
            *(
                [h.Span({"class": "avatar more"}, f"+{len(users) - 8}")]
                if len(users) > 8
                else []
            ),
        ),
    )


def input_form_view(app: App):
    """
    Message input with keyboard and button support.

    Key Datastar patterns used here:
    - ds.bind("message"): two-way binds input value to $message signal
    - ds.on("keydown", ...): runs JS on keypress, @post triggers server request
    - ds.attrs({"disabled": "!$message"}): reactively disables button when empty
    """
    send_url = app.url_for("send")
    typing_url = app.url_for("typing")

    return h.Form(
        {"id": "input-form", "class": "input-form"},
        ds.on("submit", "evt.preventDefault()"),
        h.Input(
            {
                "id": "message-input",
                "type": "text",
                "class": "message-input",
                "placeholder": "Type a message...",
                "autocomplete": "off",
                "autofocus": True,
            },
            ds.bind("message"),
            ds.on(
                "keydown",
                f"""
                if (evt.key === 'Enter' && !evt.shiftKey && $message.trim()) {{
                    evt.preventDefault();
                    @post('{send_url}');
                    $message = '';
                }}
                """,
            ),
            ds.on("input", ds.post(typing_url)),
        ),
        h.Button(
            {
                "type": "button",
                "class": "send-button",
            },
            ds.attrs({"disabled": "!$message"}),
            ds.on(
                "click",
                f"""
                if ($message.trim()) {{
                    @post('{send_url}');
                    $message = '';
                    document.getElementById('message-input').focus();
                }}
                """,
            ),
            h.Span(
                {"class": "send-icon"},
                h.SafeString(
                    """<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/></svg>"""
                ),
            ),
        ),
    )


# =============================================================================
# Pages
# =============================================================================


def chat_view(
    app: App,
    user_id: str,
    username: str,
    color: str,
    *,
    messages: list[Message],
    users: dict[str, User],
):
    """
    Main chat page.

    This view is rendered on initial load AND on every SSE patch.
    Datastar efficiently diffs and updates only changed parts of the DOM.

    Args:
        user_id: Current user's ID
        username: Current user's display name
        color: Current user's avatar color
        messages: List of chat messages to display
        users: Dict of online users

    Key setup:
    - ds.signals({...}, ifmissing=True): initializes client state (only if not set)
    - ds.init(ds.get(url_for("subscribe"))): opens SSE connection on page load
    """
    return page(
        app,
        toy_inspector(),  # Dev tool: shows current signals state
        h.Div(
            {"class": "chat-container"},
            ds.signals(
                {
                    "user_id": user_id,
                    "username": username,
                    "color": color,
                    "message": "",
                },
                ifmissing=True,
            ),
            ds.init(ds.get(app.url_for("subscribe"))),
            h.Div(
                {"class": "chat-header"},
                h.Div({"class": "chat-title"}, "Stario Chat 🐾"),
                online_users_view(users),
            ),
            h.Div(
                {"class": "chat-body"},
                messages_view(user_id, messages),
                typing_indicator_view(user_id, users),
            ),
            h.Div(
                {"class": "chat-footer"},
                input_form_view(app),
            ),
        ),
    )
