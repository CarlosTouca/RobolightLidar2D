"""Comandos de direção do rover: as mesmas letras do firmware do ESP32 (ComandoDirecao=...).

`sides` repete a tabela de `controlarMotores()` do firmware, em fração de 0 a 1 de cada lado.
Assim a página, o rover de verdade e o simulador falam a mesma língua.
"""
from typing import Literal

Command = Literal["F", "T", "E", "D", "G", "I", "H", "J", "P"]


def sides(cmd: Command, speed: int) -> tuple[float, float]:
    """(esquerda, direita), -1..1, para o comando e a velocidade (0..255, como o slider)."""
    v = speed / 255
    m = v / 2   # diagonais: o lado de dentro anda na metade da velocidade
    return {
        "F": (v, v), "T": (-v, -v), "E": (-v, v), "D": (v, -v),
        "G": (m, v), "I": (v, m), "H": (-m, -v), "J": (-v, -m), "P": (0.0, 0.0),
    }[cmd]
