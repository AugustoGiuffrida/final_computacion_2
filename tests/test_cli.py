"""Pruebas de la línea de comandos.

Verifican las tres cosas que `argparse` resuelve acá: que los valores se validen antes de
usarlos, que las reglas que `argparse` no puede expresar por sí solo —qué argumento exige
cada acción— se verifiquen igual, y que los parámetros que se envían al servidor sean los
que corresponden a la operación elegida.

`parser.error()` termina el proceso con código 2, así que en las pruebas se manifiesta
como un `SystemExit`.
"""

from __future__ import annotations

import argparse
import unittest

from app.client import cli


def parse(*argv: str) -> argparse.Namespace:
    """Parsea una línea de comandos de prueba.

    Args:
        *argv: Los argumentos, sin el nombre del programa.

    Returns:
        Los argumentos parseados.
    """
    return cli.build_parser().parse_args(list(argv))




# ──────────────────────── validadores de valores ────────────────────────


class ValueValidators(unittest.TestCase):
    """Conversión y validación de cada valor de la línea de comandos."""



    def test_an_impossible_port_is_rejected(self) -> None:
        """Un puerto fuera de 1-65535, o que no sea un número, no se acepta."""
        for raw_value in ("0", "65536", "-1", "nueve mil"):
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli.port_number(raw_value)


    def test_a_valid_port_is_accepted(self) -> None:
        """Un puerto dentro del rango se devuelve como entero."""
        self.assertEqual(cli.port_number("9000"), 9000)


    def test_a_quality_outside_the_range_is_rejected(self) -> None:
        """La calidad va de 1 a 95: por encima, JPEG crece sin mejorar."""
        for raw_value in ("0", "96", "-5", "mucha"):
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli.quality_level(raw_value)


    def test_a_valid_quality_is_accepted(self) -> None:
        """Un nivel de calidad dentro del rango se devuelve como entero."""
        self.assertEqual(cli.quality_level("80"), 80)


    def test_a_non_positive_integer_is_rejected(self) -> None:
        """Los tamaños e intensidades tienen que ser mayores que cero."""
        for raw_value in ("0", "-3", "dos"):
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli.positive_integer(raw_value)




# ──────────────────────── reglas que dependen de la acción ────────────────────────


class ActionRequirements(unittest.TestCase):
    """Reglas que argparse no puede expresar y se verifican después de parsear."""



    def test_submit_without_a_file_is_rejected(self) -> None:
        """`--action submit` sin `--file` no llega a abrir la conexión."""
        parser = cli.build_parser()
        arguments = parser.parse_args(["--user", "augusto", "--action", "submit", "--op", "clean"])

        with self.assertRaises(SystemExit) as raised:
            cli.check_action_requirements(parser, arguments)

        self.assertEqual(raised.exception.code, cli.EXIT_BAD_USAGE)


    def test_submit_without_an_operation_is_rejected(self) -> None:
        """`--action submit` sin `--op` tampoco alcanza."""
        parser = cli.build_parser()
        arguments = parser.parse_args(
            ["--user", "augusto", "--action", "submit", "--file", "foto.jpg"]
        )

        with self.assertRaises(SystemExit):
            cli.check_action_requirements(parser, arguments)


    def test_consulting_without_a_job_id_is_rejected(self) -> None:
        """Consultar o descargar exige el identificador del trabajo."""
        for action in ("status", "download"):
            with self.subTest(action=action):
                parser = cli.build_parser()
                arguments = parser.parse_args(["--user", "augusto", "--action", action])

                with self.assertRaises(SystemExit):
                    cli.check_action_requirements(parser, arguments)


    def test_wait_only_makes_sense_when_submitting(self) -> None:
        """`--wait` con una acción que no es `submit` es un error, no algo que se ignore."""
        parser = cli.build_parser()
        arguments = parser.parse_args(["--user", "augusto", "--action", "history", "--wait"])

        with self.assertRaises(SystemExit):
            cli.check_action_requirements(parser, arguments)


    def test_a_complete_submit_passes_the_checks(self) -> None:
        """Una línea de comandos completa no levanta ningún error."""
        parser = cli.build_parser()
        arguments = parser.parse_args([
            "--user", "augusto", "--action", "submit", "--file", "foto.jpg", "--op", "anonymize",
        ])

        cli.check_action_requirements(parser, arguments)  # no debe lanzar nada




# ──────────────────────── parámetros de la operación ────────────────────────


class OperationParameters(unittest.TestCase):
    """Armado del diccionario de parámetros que viaja en el pedido."""



    def test_only_the_parameters_that_were_written_are_sent(self) -> None:
        """Lo que el usuario no escribió no viaja: el servidor aplica su valor por defecto."""
        parser = cli.build_parser()
        arguments = parser.parse_args([
            "--user", "augusto", "--action", "submit", "--file", "foto.jpg",
            "--op", "anonymize", "--mode", "pixelate",
        ])

        parameters = cli.collect_operation_parameters(parser, arguments, "anonymize")

        self.assertEqual(parameters, {"mode": "pixelate"})


    def test_all_the_parameters_of_an_operation_can_travel_together(self) -> None:
        """`sanitize` acepta los cuatro parámetros y los cuatro llegan."""
        parser = cli.build_parser()
        arguments = parser.parse_args([
            "--user", "augusto", "--action", "submit", "--file", "foto.jpg", "--op", "sanitize",
            "--mode", "blur", "--strength", "15", "--quality", "80", "--max-size", "1920",
        ])

        parameters = cli.collect_operation_parameters(parser, arguments, "sanitize")

        self.assertEqual(parameters, {"mode": "blur", "strength": 15, "quality": 80, "max_size": 1920})


    def test_a_parameter_that_the_operation_does_not_accept_is_an_error(self) -> None:
        """Pasar `--mode` a `clean` se avisa, en vez de ignorarlo en silencio.

        Silenciarlo haría creer que se aplicó, que es la peor de las dos opciones.
        """
        parser = cli.build_parser()
        arguments = parser.parse_args([
            "--user", "augusto", "--action", "submit", "--file", "foto.jpg",
            "--op", "clean", "--mode", "blur",
        ])

        with self.assertRaises(SystemExit):
            cli.collect_operation_parameters(parser, arguments, "clean")


    def test_an_operation_without_parameters_sends_none(self) -> None:
        """`inspect` no acepta parámetros y no envía ninguno."""
        parser = cli.build_parser()
        arguments = parser.parse_args([
            "--user", "augusto", "--action", "submit", "--file", "foto.jpg", "--op", "inspect",
        ])

        self.assertEqual(cli.collect_operation_parameters(parser, arguments, "inspect"), {})




# ──────────────────────── valores por defecto ────────────────────────


class Defaults(unittest.TestCase):
    """Valores por defecto y argumentos obligatorios."""



    def test_the_defaults_come_from_the_configuration(self) -> None:
        """Sin host ni puerto se usan los del módulo de configuración, no números sueltos."""
        from app.common import config

        arguments = parse("--user", "augusto", "--action", "history")

        self.assertEqual(arguments.host, config.DEFAULT_HOST)
        self.assertEqual(arguments.port, config.DEFAULT_PORT)
        self.assertEqual(arguments.timeout, config.DEFAULT_WAIT_TIMEOUT_SECONDS)


    def test_the_action_is_mandatory(self) -> None:
        """Sin `--action` no hay nada que hacer: el cliente ejecuta una acción y termina."""
        with self.assertRaises(SystemExit):
            parse("--user", "augusto")


    def test_the_user_is_mandatory(self) -> None:
        """Sin `--user` no se puede hacer nada: todo pedido lo lleva."""
        with self.assertRaises(SystemExit):
            parse("--action", "history")


if __name__ == "__main__":
    unittest.main()
