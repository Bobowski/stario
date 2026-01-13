"""
Stario Chat - Data Models & Configuration

This module contains:
- Data models (dataclasses for User and Message)
- Configuration constants (username words, avatar colors)
- Helper functions for generating user identities

Note: Storage has been moved to db.py (SQLite-based).
This module now contains only pure data definitions and helpers.
"""

import random
from dataclasses import dataclass

# =============================================================================
# Configuration
# =============================================================================

# Words for generating fun usernames like "HappyPanda" or "SneakyFox"
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

# User avatar colors
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
# Data Models
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
# Helpers
# =============================================================================


def generate_username() -> str:
    """Generate a random fun username like 'HappyPanda'."""
    return f"{random.choice(ADJECTIVES)}{random.choice(ANIMALS)}"


def generate_color() -> str:
    """Pick a random color for the user's avatar."""
    return random.choice(COLORS)
