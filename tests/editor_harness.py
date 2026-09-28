"""Drive the block editor page (src/gui/blockly/editor.html) in a headless
Chromium so its JavaScript can be tested from pytest.

QtWebEngine is what the app uses, but it needs a display stack that CI and
WSL lack, so the tests run the same page in a plain headless Chromium
instead: a harness page embeds the editor in an iframe, feeds it the
catalogue exactly like ``BehaviorEditorPanel`` does (``initEditor``), runs a
list of JavaScript snippets against it, and prints the results into the DOM,
which ``--dump-dom`` hands back. No browser -> the tests skip.
"""

from __future__ import annotations

import glob
import html
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EDITOR_HTML = REPO / "src" / "gui" / "blockly" / "editor.html"


def find_chromium() -> str | None:
    """A headless-capable Chromium/Chrome binary, or None."""
    env = os.environ.get("SOFTEDIBO_CHROME")
    if env and Path(env).is_file():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for pattern in ("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
                    "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"):
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]
    return None


def run_editor_js(scripts: list[str], payload: dict,
                  timeout_s: float = 60.0) -> list:
    """Run each JavaScript snippet against a freshly loaded editor page.

    Every snippet is a function body evaluated with ``w`` bound to the
    editor iframe's window (so ``w.getSpec()``, ``w.workspace`` ...); its
    return value is JSON-encoded into the result list (an exception becomes
    ``{"error": "..."}``). Snippets run in order on the same page.
    """
    chrome = find_chromium()
    if chrome is None:
        raise RuntimeError("no Chromium found (set SOFTEDIBO_CHROME)")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "harness.html"
        page.write_text(_harness_html(scripts, payload), encoding="utf-8")
        cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
               "--allow-file-access-from-files", "--disable-dev-shm-usage",
               "--virtual-time-budget=20000",
               "--dump-dom", page.as_uri()]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s)
    marker_start = '<pre id="out">'
    start = proc.stdout.find(marker_start)
    if start < 0:
        raise RuntimeError("harness produced no output:\n" + proc.stderr[-2000:])
    start += len(marker_start)
    end = proc.stdout.find("</pre>", start)
    raw = html.unescape(proc.stdout[start:end])
    result = json.loads(raw)
    if isinstance(result, dict) and "harness_error" in result:
        raise RuntimeError(result["harness_error"])
    if not isinstance(result, list):
        raise RuntimeError(f"unexpected harness output: {raw[:200]}")
    return result


def _harness_html(scripts: list[str], payload: dict) -> str:
    fns = ",\n".join("function (w) {\n" + s + "\n}" for s in scripts)
    return f"""<!DOCTYPE html>
<html lang="en"><body>
<pre id="out">pending</pre>
<iframe id="f" src="{EDITOR_HTML.as_uri()}" width="1400" height="900"></iframe>
<script>
var PAYLOAD = {json.dumps(payload)};
var SCRIPTS = [{fns}];
var out = document.getElementById('out');
var tries = 0, sent = false;
function poll() {{
  var w = document.getElementById('f').contentWindow;
  try {{
    if (w && typeof w.initEditor === 'function' && !sent) {{
      w.initEditor(PAYLOAD); sent = true;
    }}
    if (w && w.workspace) {{ run(w); return; }}
  }} catch (e) {{
    out.textContent = JSON.stringify({{harness_error: String(e)}}); return;
  }}
  if (++tries > 400) {{
    out.textContent = JSON.stringify({{harness_error: 'editor never became ready'}});
    return;
  }}
  setTimeout(poll, 25);
}}
function run(w) {{
  var results = [];
  SCRIPTS.forEach(function (fn) {{
    try {{ results.push(fn(w)); }}
    catch (e) {{ results.push({{error: String(e && e.stack || e)}}); }}
  }});
  out.textContent = JSON.stringify(results);
}}
poll();
</script></body></html>"""
