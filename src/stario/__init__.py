# Version
from importlib.metadata import version

__version__ = version("stario")

# Application
from .application import Stario as Stario

# Datastar
from .datastar import Datastar as Datastar
from .datastar import DatastarAttributes as DatastarAttributes
from .datastar import ElementsPatch as ElementsPatch
from .datastar import Event as Event
from .datastar import EventStream as EventStream
from .datastar import HtmlEvent as HtmlEvent
from .datastar import HtmlEventStream as HtmlEventStream
from .datastar import ParseSignal as ParseSignal
from .datastar import ParseSignals as ParseSignals
from .datastar import Redirection as Redirection
from .datastar import ScriptExecution as ScriptExecution
from .datastar import Signal as Signal
from .datastar import Signals as Signals
from .datastar import SignalsPatch as SignalsPatch

# Routes
from .routes import Command as Command
from .routes import DetachedCommand as DetachedCommand
from .routes import Query as Query
