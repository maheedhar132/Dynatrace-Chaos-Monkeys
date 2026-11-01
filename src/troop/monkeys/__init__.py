"""The troop.

FIRE monkeys are reversible and canary-scoped; they are what the tool actually
runs. QUARANTINE monkeys are the dangerous ideas — classified and discoverable,
but `Guard.vet` refuses to execute them. Import `TROOP` for the registry.
"""

from __future__ import annotations

from .base import Monkey
from .fire import (
    Ape, Baboon, Capuchin, Colobus, Gelada, Gibbon, Howler, Langur, Loris, Mandrill,
    Mangabey, Marmoset, Saki, Tamarin, Tarsier, Vervet,
)
from .quarantine import QUARANTINE, QuarantinedMonkey
from .watch import Potto, Watcher

FIRE: dict[str, type[Monkey]] = {
    "marmoset": Marmoset,
    "baboon": Baboon,
    "ape": Ape,
    "mandrill": Mandrill,
    "howler": Howler,
    "capuchin": Capuchin,
    "gibbon": Gibbon,
    "langur": Langur,
    "colobus": Colobus,
    "saki": Saki,
    "mangabey": Mangabey,
    "gelada": Gelada,
    "tamarin": Tamarin,
    "loris": Loris,
    "tarsier": Tarsier,
    "vervet": Vervet,
}

WATCHERS: dict[str, type[Watcher]] = {
    "potto": Potto,
}

TROOP: dict[str, object] = {**FIRE, **{name: QUARANTINE[name] for name in QUARANTINE}}

__all__ = [
    "Monkey", "Marmoset", "Baboon", "Ape", "Mandrill", "Howler",
    "Capuchin", "Gibbon", "Langur", "Colobus", "Saki",
    "Mangabey", "Gelada", "Tamarin", "Loris", "Tarsier", "Vervet",
    "Watcher", "Potto", "WATCHERS",
    "QuarantinedMonkey", "QUARANTINE", "FIRE", "TROOP",
]
