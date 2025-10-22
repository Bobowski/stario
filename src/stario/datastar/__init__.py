from stario.html import HtmlElement

from .attributes import Actions as Actions
from .attributes import Attributes as Attributes
from .attributes import DatastarActions as DatastarActions
from .attributes import DatastarAttributes as DatastarAttributes
from .events import ElementsPatch as ElementsPatch
from .events import Event as Event
from .events import EventStream as EventStream
from .events import Redirection as Redirection
from .events import ScriptExecution as ScriptExecution
from .events import SignalsPatch as SignalsPatch
from .signals import ParseSignal as ParseSignal
from .signals import ParseSignals as ParseSignals
from .signals import Signal as Signal
from .signals import Signals as Signals

type HtmlEvent = Event[HtmlElement]
type HtmlEventStream = EventStream[HtmlElement]
