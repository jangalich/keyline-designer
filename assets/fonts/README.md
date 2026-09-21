# Fonts

The site data report's three faces, self-hosted so the PDF embeds the same
files the interactive app serves. Declared via `@font-face` in
`templates/report/report.css` (WeasyPrint); nothing else reads them.

**Vendored from the frontend repository** (`jangalich/keyline-designer-frontend`,
`src/fonts/`, at commit `6ce1a8d`), byte for byte, with the licence texts. The
two repositories deploy separately -- Vercel and a Docker image -- so the
backend cannot read the frontend's copy at runtime; this directory is the
price of that, and the frontend's `src/fonts/README.md` is the record of
where the files came from (Fontsource v5.3.0 packages, latin subset). Keep
the two directories identical; a change to one is a change to both.

| File | Family | Axis / weight | Used at |
| --- | --- | --- | --- |
| `bitter-latin-wght-normal.woff2` | Bitter | variable, `wght` 100–900 | 400, 600 |
| `source-serif-4-latin-wght-normal.woff2` | Source Serif 4 | variable, `wght` 200–900 | 400, 600 |
| `ibm-plex-mono-latin-400-normal.woff2` | IBM Plex Mono | static, 400 | 400 |
| `ibm-plex-mono-latin-500-normal.woff2` | IBM Plex Mono | static, 500 | 500 |

Variable weights render as distinct instances under WeasyPrint 70.0 with
Pango (verified: Bitter and Source Serif 4 each embed a regular and a
semi-bold subset from one file), so no static 400/600 instances are needed.
WeasyPrint is pinned to that version in `requirements.txt` for exactly this
reason.

All three families are SIL Open Font License 1.1: `Bitter-OFL.txt`,
`SourceSerif4-OFL.txt`, `IBMPlexMono-OFL.txt`.
