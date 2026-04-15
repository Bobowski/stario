"""
Domain types for chat (dataclasses) plus demo-only identity helpers.

Persistence uses these types in ``db.py``; handlers construct them from requests;
``views`` only render them. We keep types and small generators together here so
the example stays one short file — a larger app might split ``identity`` or
``seed_data`` out once this grows.
"""

import random
from dataclasses import dataclass

# =============================================================================
# Demo word lists (not product config — swap for auth-provided names in prod)
# =============================================================================

ADJECTIVES = [
    "Happy",
    "Sleepy",
    "Grumpy",
    "Sneezy",
    "Bashful",
    "Dopey",
    "Doc",
    "Swift",
    "Clever",
    "Brave",
    "Gentle",
    "Mighty",
    "Sneaky",
    "Jolly",
    "Fuzzy",
    "Jumpy",
    "Wiggly",
    "Bouncy",
    "Sparkly",
    "Fluffy",
]

ANIMALS = [
    "Panda",
    "Fox",
    "Owl",
    "Cat",
    "Dog",
    "Bear",
    "Wolf",
    "Tiger",
    "Lion",
    "Koala",
    "Bunny",
    "Penguin",
    "Otter",
    "Seal",
    "Duck",
    "Frog",
    "Sloth",
    "Deer",
    "Moose",
    "Falcon",
]

COLORS = [
    "#e74c3c",  # red
    "#e67e22",  # orange
    "#f1c40f",  # yellow
    "#2ecc71",  # green
    "#1abc9c",  # teal
    "#3498db",  # blue
    "#9b59b6",  # purple
    "#e91e63",  # pink
    "#00bcd4",  # cyan
    "#8bc34a",  # lime
]


# =============================================================================
# Data models
# =============================================================================


@dataclass
class Message:
    """A chat message with sender info and timestamp."""

    id: str
    user_id: str
    username: str
    color: str
    text: str
    timestamp: float


@dataclass
class User:
    """A connected user with their display info and typing state."""

    id: str
    username: str
    color: str
    typing: bool = False


# =============================================================================
# Demo identity (new visitor on GET /)
# =============================================================================


def generate_username() -> str:
    """Generate a random fun username like 'HappyPanda'."""
    return f"{random.choice(ADJECTIVES)}{random.choice(ANIMALS)}"


def generate_color() -> str:
    """Pick a random color for the user's avatar."""
    return random.choice(COLORS)
