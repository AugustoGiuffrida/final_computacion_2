"""Pruebas del cliente visual.

Solo de la lógica propia de la interfaz: cómo interpreta una ruta escrita y cuándo deja
enviar. Todo lo demás —la red, el protocolo, el formato de los valores— ya está probado en
las suites del cliente de terminal, porque es exactamente el mismo código.

Corren sin abrir ninguna ventana: Textual trae un arnés que ejecuta la aplicación de forma
headless y permite consultar sus widgets.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.widgets import Button, Input

from app.tui.application import ImagesApp, ImageTree


class TuiTestCase(unittest.IsolatedAsyncioTestCase):
    """Base que arranca la aplicación sin conexión al servidor.

    El puerto 1 no tiene nada escuchando a propósito: estas pruebas no necesitan servidor,
    y la aplicación tiene que arrancar igual avisando que no pudo conectarse.
    """

    def setUp(self) -> None:
        """Crea el árbol de carpetas temporal de la prueba."""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name).resolve()
        (self.root / "fotos" / "vacaciones").mkdir(parents=True)

        self.addCleanup(self._temporary_directory.cleanup)

    def an_app(self, directory: Path | None = None) -> ImagesApp:
        """Arma la aplicación apuntando a una carpeta.

        Args:
            directory: Con qué carpeta arranca el árbol.

        Returns:
            La aplicación, sin arrancar todavía.
        """
        app = ImagesApp(
            user="ana", host="127.0.0.1", port=1,
            directory=directory or self.root, downloads=self.root,
        )
        app.notify = lambda message, **extra: None  # sin avisos en pantalla

        return app


class WritingAPath(TuiTestCase):
    """Cómo interpreta el campo de carpeta lo que se escribe.

    Se comporta como el `cd` de una terminal: las rutas relativas se miden desde la carpeta
    que se está viendo. Medirlas desde donde arrancó el programa hacía que `..` llevara
    siempre al mismo lugar, sin importar cuántas veces se apretara.
    """

    async def go_to(self, app: ImagesApp, written: str) -> Path:
        """Escribe una ruta en el campo y confirma.

        Args:
            app: La aplicación en marcha.
            written: Lo que se escribe.

        Returns:
            La carpeta que quedó mostrando el árbol.
        """
        field = app.query_one("#root", Input)
        await app.on_input_submitted(Input.Submitted(field, written))

        return Path(str(app.query_one("#tree", ImageTree).path))

    async def test_going_up_can_be_repeated(self) -> None:
        """El error que motivó estas pruebas: `..` subía una vez y después no se movía."""
        app = self.an_app(self.root / "fotos" / "vacaciones")
        async with app.run_test():
            self.assertEqual(await self.go_to(app, ".."), self.root / "fotos")
            self.assertEqual(await self.go_to(app, ".."), self.root)

    async def test_a_relative_path_starts_where_you_are(self) -> None:
        app = self.an_app()
        async with app.run_test():
            self.assertEqual(await self.go_to(app, "fotos"), self.root / "fotos")
            self.assertEqual(
                await self.go_to(app, "vacaciones"), self.root / "fotos" / "vacaciones"
            )

    async def test_an_absolute_path_is_taken_as_is(self) -> None:
        app = self.an_app()
        async with app.run_test():
            self.assertEqual(await self.go_to(app, "/"), Path("/"))

    async def test_the_home_shortcut_and_the_variable_are_expanded(self) -> None:
        """`~` y `$HOME` son las dos formas de escribirlo en una terminal."""
        app = self.an_app()
        async with app.run_test():
            self.assertEqual(await self.go_to(app, "~"), Path.home())
            self.assertEqual(await self.go_to(app, "$HOME"), Path.home())

    async def test_the_field_shows_where_you_ended_up(self) -> None:
        """Tras un `..` el campo diría `..`, que no informa nada."""
        app = self.an_app(self.root / "fotos")
        async with app.run_test():
            await self.go_to(app, "..")

            self.assertEqual(app.query_one("#root", Input).value, str(self.root))

    async def test_a_path_that_is_not_a_folder_is_refused(self) -> None:
        """Y el campo vuelve a la carpeta anterior, para no mentir sobre lo que se ve."""
        app = self.an_app()
        async with app.run_test():
            self.assertEqual(await self.go_to(app, "no_existe"), self.root)
            self.assertEqual(app.query_one("#root", Input).value, str(self.root))


class SendingIsGuarded(TuiTestCase):
    """El botón de enviar solo se enciende cuando hay con qué."""

    async def test_it_starts_off_asking_for_an_image(self) -> None:
        app = self.an_app()
        async with app.run_test():
            button = app.query_one("#send", Button)

            self.assertTrue(button.disabled)
            self.assertIn("imagen", str(button.label))

    async def test_it_turns_on_once_there_is_an_image(self) -> None:
        app = self.an_app()
        async with app.run_test():
            app.selected = self.root / "foto.jpg"
            app.update_send_button()
            button = app.query_one("#send", Button)

            self.assertFalse(button.disabled)
            self.assertIn(app.operation, str(button.label))

    async def test_an_operation_comes_preselected(self) -> None:
        """Sin ninguna elegida, el área de parámetros arranca vacía y se ve raro."""
        app = self.an_app()
        async with app.run_test():
            self.assertIsNotNone(app.operation)


if __name__ == "__main__":
    unittest.main()
