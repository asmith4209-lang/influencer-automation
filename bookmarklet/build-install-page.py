#!/usr/bin/env python3
"""
Generate dashboard/static/bookmarklet.html from bookmarklet.min.js.

Emailing or messaging the bookmarklet does not survive the trip: clients hard-wrap
the long line, and the injected newlines land inside string and regex literals, so
the script fails to parse and the bookmark silently does nothing. This page hands
it over as a draggable link instead, so the text is never retyped or re-wrapped.

Run via set-url.sh, which regenerates this whenever the deployment URL changes.
"""

import html
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIN = ROOT / "bookmarklet" / "bookmarklet.min.js"
OUT = ROOT / "dashboard" / "static" / "bookmarklet.html"

code = MIN.read_text().strip()
if not code.startswith("javascript:"):
    raise SystemExit(f"{MIN} does not start with 'javascript:' - refusing to build")

m = re.search(r'var U="([^"]+)"', code)
target = m.group(1) if m else "(unknown)"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install the Add to Sheet bookmark</title>
<style>
  :root{color-scheme:light dark;--bg:#faf8fb;--card:#fff;--ink:#2c2431;--mute:#6b6175;
        --line:#e6e0ea;--accent:#7d5a8c;--ok:#27ae60}
  @media (prefers-color-scheme:dark){
    :root{--bg:#1c1820;--card:#262130;--ink:#f0ecf3;--mute:#a79db2;--line:#3a3345;--accent:#b98fc9}
  }
  *{box-sizing:border-box}
  body{margin:0;padding:32px 20px;background:var(--bg);color:var(--ink);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:620px;margin:0 auto}
  h1{font-size:22px;margin:0 0 6px}
  .sub{color:var(--mute);font-size:14px;margin:0 0 28px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:24px;margin-bottom:20px}
  .drag{display:inline-block;padding:14px 26px;border-radius:10px;background:var(--accent);
        color:#fff;font-weight:700;font-size:16px;text-decoration:none;cursor:grab;
        box-shadow:0 3px 10px rgba(0,0,0,.18)}
  .drag:active{cursor:grabbing}
  ol{margin:0;padding-left:22px}
  li{margin-bottom:10px}
  .note{font-size:13px;color:var(--mute);margin-top:18px}
  code{background:rgba(125,90,140,.12);padding:2px 6px;border-radius:5px;font-size:13px}
  details{margin-top:18px}
  summary{cursor:pointer;color:var(--accent);font-size:14px;font-weight:600}
  textarea{width:100%;height:150px;margin-top:12px;padding:10px;border-radius:8px;
           border:1px solid var(--line);background:var(--bg);color:var(--ink);
           font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
           white-space:pre;overflow-x:auto}
  button{margin-top:10px;padding:9px 16px;border-radius:8px;border:1px solid var(--line);
         background:var(--card);color:var(--ink);font-size:14px;font-weight:600;cursor:pointer}
  .ok{color:var(--ok);font-weight:700}
</style>
</head>
<body>
<div class="wrap">
  <h1>Add to Sheet &mdash; bookmark install</h1>
  <p class="sub">Drag the button onto your bookmarks bar. Nothing to copy or type.</p>

  <div class="card">
    <ol>
      <li>Show the bookmarks bar &mdash; <code>Cmd+Shift+B</code> (Mac) or <code>Ctrl+Shift+B</code> (Windows).</li>
      <li>Drag this button up onto it:</li>
    </ol>
    <p style="margin:20px 0 4px"><a class="drag" href="__BOOKMARKLET__">&#128230; Add to Sheet</a></p>
    <ol start="3">
      <li>Delete the <strong>old</strong> &ldquo;Add to Sheet&rdquo; bookmark so there is only one.</li>
      <li>Open any Amazon product page and click the new bookmark.</li>
    </ol>
    <p class="note">You should see an amber <em>Sending&hellip;</em> banner, then a green
    <span class="ok">Added to row 17</span>. The row number means it really wrote to the sheet.
    Anything red will say what went wrong.</p>
  </div>

  <div class="card">
    <details>
      <summary>Can&rsquo;t drag it? Copy the text instead</summary>
      <p class="note">Use the button &mdash; never copy this out of an email or chat message.
      Those wrap long lines, and the line breaks break the code.</p>
      <textarea id="src" readonly>__BOOKMARKLET_TEXT__</textarea>
      <button id="copy">Copy to clipboard</button>
      <span id="done" class="ok" style="margin-left:10px"></span>
      <p class="note">Paste into the bookmark&rsquo;s <strong>URL</strong> field. It must stay one
      line and start with <code>javascript:</code> &mdash; some browsers strip that prefix, so check
      after saving and type it back if it is missing.</p>
    </details>
  </div>

  <p class="note">Points at: <code>__TARGET__</code></p>
</div>
<script>
document.getElementById('copy').addEventListener('click', function(){
  var ta = document.getElementById('src');
  ta.select();
  navigator.clipboard.writeText(ta.value).then(function(){
    document.getElementById('done').textContent = 'Copied';
  }).catch(function(){
    document.execCommand('copy');
    document.getElementById('done').textContent = 'Copied';
  });
});
</script>
</body>
</html>
"""

page = (PAGE
        .replace("__BOOKMARKLET__", html.escape(code, quote=True))
        .replace("__BOOKMARKLET_TEXT__", html.escape(code, quote=True))
        .replace("__TARGET__", html.escape(target, quote=True)))

OUT.write_text(page)
print(f"wrote {OUT} ({len(page)} bytes) -> {target}")
