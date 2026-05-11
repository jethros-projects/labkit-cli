#!/usr/bin/env python3
from __future__ import annotations

import html
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
OUTPUT = ROOT / "README.html"


def inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def flush_paragraph(lines: list[str], out: list[str]) -> None:
    if lines:
        out.append(f"<p>{inline(' '.join(lines))}</p>")
        lines.clear()


def flush_list(items: list[str], out: list[str]) -> None:
    if items:
        out.append("<ul>")
        out.extend(f"<li>{inline(item)}</li>" for item in items)
        out.append("</ul>")
        items.clear()


def render_markdown(markdown: str) -> str:
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                out.append(
                    '<pre><code class="language-{}">{}</code></pre>'.format(
                        html.escape(code_lang),
                        html.escape("\n".join(code_lines)),
                    )
                )
                in_code = False
                code_lang = ""
                code_lines = []
            else:
                flush_paragraph(paragraph, out)
                flush_list(list_items, out)
                in_code = True
                code_lang = line[3:].strip()
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not line:
            flush_paragraph(paragraph, out)
            flush_list(list_items, out)
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            flush_paragraph(paragraph, out)
            flush_list(list_items, out)
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            continue

        if line.startswith("- "):
            flush_paragraph(paragraph, out)
            list_items.append(line[2:])
            continue

        paragraph.append(line)

    flush_paragraph(paragraph, out)
    flush_list(list_items, out)
    return "\n".join(out)


def page(body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lab Kit CLI</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1115;
      --panel: #171a21;
      --text: #eff1f5;
      --muted: #a8b0bd;
      --line: #2b303b;
      --accent: #4cc9f0;
      --code: #222734;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #fbfbfc;
        --panel: #ffffff;
        --text: #171923;
        --muted: #5c6472;
        --line: #e1e4ea;
        --accent: #0969da;
        --code: #f4f6f8;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.62 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(860px, calc(100vw - 32px));
      margin: 48px auto;
      padding: 0 0 56px;
    }}
    h1, h2, h3 {{
      line-height: 1.15;
      margin: 2rem 0 0.75rem;
    }}
    h1 {{
      margin-top: 0;
      font-size: clamp(2.4rem, 8vw, 4.75rem);
      letter-spacing: 0;
    }}
    h2 {{
      padding-top: 1.25rem;
      border-top: 1px solid var(--line);
      font-size: 1.45rem;
    }}
    p, ul {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--code);
      color: var(--text);
      padding: 0.08rem 0.32rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.92em;
    }}
    pre {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 1rem;
    }}
    pre code {{
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 0;
      color: var(--text);
    }}
    li {{ margin: 0.35rem 0; }}
    strong {{ color: var(--text); }}
  </style>
</head>
<body>
  <main>
{body}
  </main>
</body>
</html>
"""


def main() -> int:
    OUTPUT.write_text(page(render_markdown(README.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
