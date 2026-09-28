"""Real Textual themes, rendered locally; no accounts, models or provider calls."""
from types import SimpleNamespace
import unittest

from rich.style import Style
from rich.text import Text
from textual.app import App
from textual.color import Color
from textual.theme import BUILTIN_THEMES, Theme

from appearance import SWATCH_ROLES, ThemePaletteProvider, theme_colors


class ThemePaletteTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, themes=None):
        app = SimpleNamespace(available_themes=themes or BUILTIN_THEMES.copy(), theme="textual-dark")
        return app, ThemePaletteProvider(SimpleNamespace(app=app))

    async def test_discovery_keeps_all_registered_names_callbacks_and_actual_swatches(self):
        app, provider = self.make_provider()
        hits = [hit async for hit in provider.discover()]
        self.assertEqual([hit.text for hit in hits], sorted(app.available_themes))
        for hit in hits:
            with self.subTest(theme=hit.text):
                theme = app.available_themes[hit.text]
                display = hit.display
                self.assertIsInstance(display, Text)
                self.assertTrue(display.plain.startswith(theme.name + "  "))
                self.assertIn("Dark" if theme.dark else "Light", display.plain)
                self.assertIsNone(hit.help)
                self.assertEqual(display.plain.index("██"), 36)
                variables = {**theme.to_color_system().generate(), **theme.variables}
                swatches = [span for span in display.spans if display.plain[span.start:span.end] == "██"]
                opaque = [role for role in SWATCH_ROLES if Color.parse(variables[role]).a != 0]
                self.assertEqual(len(swatches), len(opaque))
                for span, role in zip(swatches, opaque):
                    self.assertEqual(Style.parse(span.style).color, Color.parse(variables[role]).rich_color)
                hit.command()
                self.assertEqual(app.theme, theme.name)

    async def test_search_matches_names_and_retains_highlighting_and_palette(self):
        app, provider = self.make_provider()
        hits = [hit async for hit in provider.search("drac")]
        hit = next(hit for hit in hits if hit.text == "dracula")
        self.assertIsInstance(hit.match_display, Text)
        self.assertTrue(hit.match_display.plain.startswith("dracula"))
        self.assertEqual(hit.match_display.plain[22:26], "Dark")
        self.assertEqual(hit.match_display.plain.index("██"), 36)
        self.assertIsNone(hit.help)
        self.assertEqual(hit.match_display.plain.count("██"), 7)
        self.assertTrue(any(isinstance(span.style, Style) and span.style.reverse for span in hit.match_display.spans))
        hit.command()
        self.assertEqual(app.theme, "dracula")
        self.assertEqual([hit async for hit in provider.search("no-such-watchtower-theme-098")], [])

    async def test_custom_theme_variables_and_literal_names_are_preserved(self):
        theme = Theme(name="custom [red] palette", primary="#123456", variables={"primary": "#abcdef"}, dark=False)
        _app, provider = self.make_provider({theme.name: theme})
        discovered = [hit async for hit in provider.discover()]
        hit = discovered[0]
        self.assertTrue(hit.display.plain.startswith(theme.name + "  "))
        self.assertIn("Light", hit.display.plain)
        first = next(span for span in hit.display.spans if hit.display.plain[span.start:span.end] == "██")
        self.assertEqual(Style.parse(first.style).color, Color.parse("#abcdef").rich_color)
        matches = [hit async for hit in provider.search("custom")]
        self.assertTrue(matches[0].match_display.plain.startswith(theme.name))

    async def test_ansi_palette_keeps_native_colors_and_shows_transparent_surface(self):
        _app, provider = self.make_provider({"ansi-light": BUILTIN_THEMES["ansi-light"]})
        hit = next(iter([hit async for hit in provider.discover()]))
        self.assertIn("Light · ANSI", hit.display.plain)
        self.assertTrue(hit.display.plain.endswith("··"))
        colors = [Style.parse(span.style).color for span in hit.display.spans if hit.display.plain[span.start:span.end] == "██"]
        self.assertTrue(all(color.is_system_defined for color in colors))


class SemanticColorsTests(unittest.TestCase):
    def test_light_and_dark_use_actual_contrast_adjusted_theme_text_roles(self):
        app = App()
        palettes = []
        for name in ("textual-dark", "textual-light"):
            app.theme = name
            colors = theme_colors(app)
            variables = app.get_css_variables()
            for role in ("primary", "secondary", "accent", "success", "warning", "error"):
                self.assertEqual(Style.parse(colors[role]).color, Color.parse(variables["text-" + role]).rich_color)
            for style in colors.values():
                Style.parse(style)
            palettes.append(colors)
        self.assertNotEqual(palettes[0]["foreground"], palettes[1]["foreground"])
        self.assertNotEqual(palettes[0]["muted"], palettes[1]["muted"])
        self.assertNotEqual(palettes[0]["success"], palettes[1]["success"])

    def test_semantic_colors_respect_theme_variable_overrides(self):
        app = App()
        app.register_theme(Theme(name="custom", primary="#123456", variables={"text-success": "#114433"}))
        app.theme = "custom"
        self.assertEqual(theme_colors(app)["success"], "#114433")

    def test_ansi_semantic_roles_remain_native_and_muted_default_is_dim(self):
        app = App()
        app.theme = "ansi-dark"
        colors = theme_colors(app)
        self.assertEqual(Style.parse(colors["primary"]).color, Color.parse("ansi_blue").rich_color)
        self.assertEqual(colors["surface"], "default")
        self.assertEqual(colors["foreground"], "default")
        self.assertTrue(Style.parse(colors["muted"]).dim)
        self.assertTrue(Style.parse(colors["muted"]).color.is_default)


if __name__ == "__main__":
    unittest.main()
