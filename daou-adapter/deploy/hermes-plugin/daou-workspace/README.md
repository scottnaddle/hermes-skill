# daou-workspace (Hermes plugin)

Document/slide tools for the daou and slides profiles (`daou_make_document`, …).
SESSION_REV `2026-10-31-shiraki-styles`.

## Slide styles: `style=shiraki-NNN`

- `/슬라이드 style=shiraki-190 pages=3 …` — uses the shiraki.design No.190 YAML design
  spec as the OpenCode Path A visual direction (spec is inlined into the prompt).
  `shiraki-3`, `shiraki-003`, `shiraki003` are equivalent. Range: 001–236.
- `palette=brand` — keep the shiraki layout but swap colors for the theme brand palette
  (Ubion navy/teal for `theme=ubion`). Default = the YAML's own palette.
- `theme=shiraki-NNN` (picker tab) is aliased to `style=shiraki-NNN` on `theme=ubion`.
- Always: Ubion logo on every slide (non-company themes), S-Core Dream for Latin and
  East Asian text (slides + theme fonts). Unknown number → `error=unknown_shiraki_style`.
- Legacy fallback maps shiraki styles to the `corporate` family.

### YAMLs are fetched, not bundled

`assets/shiraki/index.json` lists id / name / category / source URL / thumb URL only.
The YAML bodies are third-party content (https://shiraki.design) and are **not** in
this repo. Fetch them on the plugin host:

    python3 assets/shiraki/fetch_shiraki.py --dest /opt/ubm-template-gate/shiraki

Env: `DAOU_SHIRAKI_DIR` (YAML dir), `DAOU_UBION_LOGO_DIR` (dir with
`logo-coursemos-ubion.png` / `logo-coursemos-ubion-dark.png`).
