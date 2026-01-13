"""
Stario Chat - HTML Views

Views are pure functions: data in, HTML out.
They receive messages and users as parameters - no global state access.
This makes them easy to test and enables the closure-based dependency injection.

Stario's HTML helpers work like function calls:
  Div({"class": "foo"}, "child1", child2)  →  <div class="foo">child1...</div>

Datastar attributes (data.*) add reactivity:
  data.signals({...})  - client-side reactive state
  data.bind("field")   - two-way binding to signal
  data.on("event", "code")  - event handler
  at.get/at.post       - trigger server requests
"""

import time

from stario import asset, at, data
from stario.html import (
    Body,
    Button,
    Div,
    Form,
    Head,
    Html,
    Input,
    Link,
    Meta,
    SafeString,
    Script,
    Span,
    Title,
)
from stario.toys import toy_inspector

from .state import Message, User

# =============================================================================
# Base Layout
# =============================================================================


def page(*children):
    """
    Base HTML shell with Datastar loaded.

    asset() returns fingerprinted filenames (e.g., style.a1b2c3.css)
    for automatic cache busting when files change.
    """
    return Html(
        {"lang": "en"},
        Head(
            Meta({"charset": "UTF-8"}),
            Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            Title("Chat - Stario"),
            Link({"rel": "stylesheet", "href": "/static/" + asset("css/style.css")}),
            Script({"type": "module", "src": "/static/" + asset("js/datastar.js")}),
        ),
        Body(*children),
    )


# =============================================================================
# Components
# =============================================================================


def message_view(msg: Message, current_user_id: str):
    """Single chat message bubble. Own messages get different styling."""
    is_own = msg.user_id == current_user_id
    bubble_class = "message own" if is_own else "message"
    msg_time = time.strftime("%H:%M", time.localtime(msg.timestamp))

    return Div(
        {"class": bubble_class, "data-msg-id": msg.id},
        Div(
            {"class": "message-header"},
            Span(
                {"class": "username", "style": {"color": msg.color}},
                msg.username,
            ),
            Span({"class": "timestamp"}, msg_time),
        ),
        Div({"class": "message-text"}, msg.text),
    )


def messages_view(current_user_id: str, messages: list[Message]):
    """
    Message list container.

    The data.on("load", ...) scrolls to bottom when new content loads.
    This runs client-side after Datastar merges the patch into the DOM.
    """
    if not messages:
        return Div(
            {"id": "messages", "class": "messages empty"},
            Div({"class": "empty-state"}, "No messages yet. Say hello!"),
        )

    return Div(
        {"id": "messages", "class": "messages"},
        data.on("load", "setTimeout(() => this.scrollTop = this.scrollHeight, 10)"),
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
        return Div({"id": "typing", "class": "typing-indicator hidden"})

    if len(typing_users) == 1:
        text = f"{typing_users[0].username} is typing"
    elif len(typing_users) == 2:
        text = f"{typing_users[0].username} and {typing_users[1].username} are typing"
    else:
        text = (
            f"{typing_users[0].username} and {len(typing_users) - 1} others are typing"
        )

    return Div(
        {"id": "typing", "class": "typing-indicator"},
        Span({"class": "typing-text"}, text),
        Span(
            {"class": "typing-dots"},
            Span({"class": "dot"}, "."),
            Span({"class": "dot"}, "."),
            Span({"class": "dot"}, "."),
        ),
    )


def online_users_view(users: dict[str, User]):
    """Shows online user avatars. Caps at 8 with a +N overflow indicator."""
    if not users:
        return Div({"id": "online", "class": "online-users"})

    return Div(
        {"id": "online", "class": "online-users"},
        Span({"class": "online-label"}, f"{len(users)} online"),
        Div(
            {"class": "avatars"},
            *[
                Span(
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
                [Span({"class": "avatar more"}, f"+{len(users) - 8}")]
                if len(users) > 8
                else []
            ),
        ),
    )


def input_form_view():
    """
    Message input with keyboard and button support.

    Key Datastar patterns used here:
    - data.bind("message"): two-way binds input value to $message signal
    - data.on("keydown", ...): runs JS on keypress, @post triggers server request
    - data.attr({disabled: "!$message"}): reactively disables button when empty
    """
    return Form(
        {"id": "input-form", "class": "input-form"},
        data.on("submit", "evt.preventDefault()"),
        Input(
            {
                "id": "message-input",
                "type": "text",
                "class": "message-input",
                "placeholder": "Type a message...",
                "autocomplete": "off",
                "autofocus": True,
            },
            data.bind("message"),
            data.on(
                "keydown",
                """
                if (evt.key === 'Enter' && !evt.shiftKey && $message.trim()) {
                    evt.preventDefault();
                    @post('/send');
                    $message = '';
                }
                """,
            ),
            data.on("input", at.post("/typing")),
        ),
        Button(
            {
                "type": "button",
                "class": "send-button",
            },
            data.attr({"disabled": "!$message"}),
            data.on(
                "click",
                """
                if ($message.trim()) {
                    @post('/send');
                    $message = '';
                    document.getElementById('message-input').focus();
                }
                """,
            ),
            Span(
                {"class": "send-icon"},
                SafeString(
                    """<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/></svg>"""
                ),
            ),
        ),
    )


# =============================================================================
# Pages
# =============================================================================


def chat_view(
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
    - data.signals({...}, ifmissing=True): initializes client state (only if not set)
    - data.init(at.get("/subscribe")): opens SSE connection on page load
    """
    return page(
        toy_inspector(),  # Dev tool: shows current signals state
        Div(
            {"class": "chat-container"},
            data.signals(
                {
                    "user_id": user_id,
                    "username": username,
                    "color": color,
                    "message": "",
                },
                ifmissing=True,
            ),
            data.init(at.get("/subscribe")),
            Div(
                {"class": "chat-header"},
                Div({"class": "chat-title"}, "Stario Chat 🐾"),
                online_users_view(users),
            ),
            Div(
                {"class": "chat-body"},
                messages_view(user_id, messages),
                typing_indicator_view(user_id, users),
            ),
            Div(
                {"class": "chat-footer"},
                input_form_view(),
            ),
        ),
    )
