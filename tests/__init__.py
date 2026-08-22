"""Pruebas del proyecto, sobre la biblioteca estándar `unittest`.

Se ejecutan desde la raíz del repositorio con:

    python -m unittest discover -s tests -t .

Las que necesitan un servidor levantan uno de verdad en un puerto libre de localhost: los
mensajes viajan por un socket TCP y atraviesan el framing completo. No se simula nada.
"""
