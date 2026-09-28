"""Theme-aware Rich colors and previews from Textual's registered theme objects."""
from rich.text import Text
from textual.color import Color, ColorParseError
from textual.command import DiscoveryHit, Hit
from textual.theme import ThemeProvider


SWATCH_ROLES = ("primary", "secondary", "accent", "success", "warning", "error", "surface")


def _color(value, background):
    """Resolve Textual CSS color/alpha values to Rich without inventing ANSI RGB."""
    value = str(value).strip()
    alpha = 1.0
    color_value, separator, percent = value.rpartition(" ")
    if separator and percent.endswith("%"):
        try:
            alpha = max(0.0, min(1.0, float(percent[:-1]) / 100))
            value = color_value
        except ValueError:
            pass
    if value == "auto":
        if background.ansi is not None:
            return ("dim " if alpha < 1 else "") + "default"
        color = background.get_contrast_text(alpha)
    else:
        color = Color.parse(value)
        # Color.with_alpha rebuilds RGB and discards Textual's ANSI index.
        if color.ansi is not None:
            return ("dim " if color.a * alpha < 1 else "") + color.rich_color.name
        color = color.with_alpha(color.a * alpha)
    if color.a < 1:
        if background.ansi is not None:
            # The actual terminal background is unknown; don't substitute black.
            return "default" if color.a == 0 else "dim " + color.rich_color.name
        color = background + color
    return color.rich_color.name


def theme_colors(app):
    """Return Rich-compatible semantic styles for the app's current theme.

    Text roles use Textual's contrast-adjusted text-* variables. Muted text is
    resolved against the theme surface; terminal-default ANSI colors stay ANSI.
    CSS widgets should continue using Textual variables directly.
    """
    variables = app.get_css_variables()
    background = Color.parse(variables["background"])
    surface = Color.parse(variables["surface"])
    if surface.a < 1:
        surface = background if background.ansi is not None else background + surface
    result = {}
    for role in SWATCH_ROLES[:-1]:
        result[role] = _color(variables.get("text-" + role, variables[role]), surface)
    for role in ("foreground", "background", "surface", "panel"):
        result[role] = _color(variables[role], background)
    result["muted"] = _color(variables.get("text-muted", variables["foreground"]), surface)
    return result


def _theme_display(theme, matcher=None):
    """Keep names as literal text; theme preview colors are independent of search."""
    display = Text(theme.name)
    if matcher is not None:
        # Textual 8's Matcher.highlight parses markup; names are literal here.
        # Its public fuzzy_search supplies the same offsets without that parsing.
        _score, offsets = matcher.fuzzy_search.match(matcher.query, theme.name)
        for offset in offsets:
            if not theme.name[offset].isspace():
                display.stylize(matcher.match_style.rich_style, offset, offset + 1)
    display.append(" " * max(2, 22 - display.cell_len))
    badge = ("Dark" if theme.dark else "Light") + (" · ANSI" if theme.ansi else "")
    display.append(badge.ljust(12), style="dim")
    display.append("  ")
    variables = {**theme.to_color_system().generate(), **theme.variables}
    background = Color.parse(variables["background"])
    for index, role in enumerate(SWATCH_ROLES):
        if index:
            display.append(" ")
        value = variables[role]
        try:
            transparent = Color.parse(value).a == 0
        except ColorParseError:
            transparent = False
        if transparent:
            display.append("··", style="dim")
        else:
            display.append("██", style=_color(value, background))
    return display


class ThemePaletteProvider(ThemeProvider):
    """Built-in theme commands with real palette previews in discovery and search."""

    async def discover(self):
        themes = self.app.available_themes
        for name, callback in self.commands:
            yield DiscoveryHit(_theme_display(themes[name]), callback, text=name)

    async def search(self, query):
        matcher = self.matcher(query)
        themes = self.app.available_themes
        for name, callback in self.commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, _theme_display(themes[name], matcher), callback, text=name)
