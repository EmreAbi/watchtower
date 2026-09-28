"""Local output actions. Never execute files or use transcript text as a command."""
from __future__ import annotations

import base64
import hashlib
import html
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
import webbrowser

IMAGES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
          '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
DOCUMENTS = {'.md', '.markdown', '.txt', '.json', '.csv', '.log', '.pdf'}
LIMIT = 32 * 1024 * 1024


def local_file(value):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid local file path.')
    if value.lower().startswith('file:'):
        parsed = urlsplit(value)
        if parsed.netloc.lower() not in ('', 'localhost') or parsed.query or parsed.fragment:
            raise ValueError('Only local file links are supported.')
        value = unquote(parsed.path)
        if re.match(r'^/[A-Za-z]:/', value):
            value = value[1:]
    if value.startswith(('\\\\', '//')) or any(ord(c) < 32 for c in value):
        raise ValueError('Only local file paths are supported.')
    # Accept the same /C:/... form used by Codex Markdown output links.
    if re.match(r'^/[A-Za-z]:[/\\]', value):
        value = value[1:]
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() not in DOCUMENTS | IMAGES.keys():
        raise ValueError('Choose a local image, Markdown, PDF or text file.')
    path = path.resolve(strict=True)
    if str(path).startswith(('\\\\', '//')):
        raise ValueError('Network file targets are not supported.')
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('The file is unavailable or larger than 32 MB.')
    return path


def render_preview(path, cache):
    path = local_file(str(path))
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        # Open in the browser only; never dispatch arbitrary file associations.
        return path
    if suffix in IMAGES:
        encoded = base64.b64encode(path.read_bytes()).decode('ascii')
        body = f'<img alt="{html.escape(path.name, quote=True)}" src="data:{IMAGES[suffix]};base64,{encoded}">'
    elif suffix in {'.md', '.markdown'}:
        from markdown_it import MarkdownIt
        md = MarkdownIt('commonmark', {'html': False}).enable('table')
        # No remote/local embeds or active links from generated documents.
        md.renderer.rules['image'] = lambda tokens, idx, *_: html.escape(tokens[idx].content)
        md.renderer.rules['link_open'] = lambda *_: '<span class="link">'
        md.renderer.rules['link_close'] = lambda *_: '</span>'
        body = md.render(path.read_text(encoding='utf-8', errors='replace'))
    else:
        body = '<pre>' + html.escape(path.read_text(encoding='utf-8', errors='replace')) + '</pre>'
    title = html.escape(path.name)
    page = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            f'<title>{title} — Watchtower</title><style>'
            'body{background:#16191f;color:#e6e9ef;font:17px/1.6 system-ui;margin:30px auto;padding:0 28px;max-width:1300px}'
            'img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#232833;padding:18px}'
            'td,th{border:1px solid #50576a;padding:6px 12px}table{border-collapse:collapse}small{overflow-wrap:anywhere;color:#a8b2c8}'
            '.link{color:#9dbbff}h1{font-size:24px}</style></head>'
            f'<body><h1>{title}</h1><small>{html.escape(str(path))}</small><main>{body}</main></body></html>')
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    output = cache / (hashlib.sha256(str(path).encode()).hexdigest()[:24] + '.html')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache, delete=False) as stream:
        stream.write(page)
        temporary = stream.name
    os.replace(temporary, output)
    return output


def action(path, name, cache):
    path = local_file(str(path))
    if name in ('preview', 'open'):
        target = render_preview(path, cache)
        if not webbrowser.open(target.as_uri(), new=2):
            raise ValueError('Browser could not be opened.')
        return 'Opened in browser.'
    if name == 'show_folder':
        if os.name != 'nt':
            raise ValueError('Folder actions currently require Windows.')
        subprocess.Popen([str(Path(os.environ['SystemRoot']) / 'explorer.exe'), '/select,', str(path)])
        return 'Opened containing folder.'
    if name == 'copy_path':
        if os.name != 'nt':
            raise ValueError('Clipboard actions currently require Windows.')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-STA', '-Command',
            "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[System.Text.Encoding]::UTF8; "
            'Set-Clipboard -Value ([Console]::In.ReadToEnd()) -ErrorAction Stop'],
            input=str(path), text=True, encoding='utf-8', stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return 'Copied full path.'
    if name == 'save_as':
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        try:
            target = filedialog.asksaveasfilename(parent=root, title='Save output as',
                initialfile=path.name, defaultextension=path.suffix,
                filetypes=[('Output', '*' + path.suffix)])
        finally:
            root.destroy()
        if not target:
            return 'Save cancelled.'
        destination = Path(target)
        if destination.suffix.lower() != path.suffix.lower():
            raise ValueError('Keep the original file extension.')
        if destination.resolve() != path:
            shutil.copyfile(path, destination)
        return 'Saved output.'
    raise ValueError('Unknown file action.')
