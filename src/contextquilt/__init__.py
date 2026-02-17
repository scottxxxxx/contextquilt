"""ContextQuilt: Intelligent AI Gateway with persistent cognitive memory layer."""

__version__ = "0.1.0"

from .gateway import Gateway
from .memory import MemoryLayer, MemoryType

__all__ = ["Gateway", "MemoryLayer", "MemoryType", "__version__"]
