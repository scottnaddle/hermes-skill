# shiraki.design slide styles (2026-10-31, SESSION_REV 2026-10-31-shiraki-styles)

- 236 shiraki.design Slides styles (No.001–236, 16:9) added to the slide style list.
- Usage: `style=shiraki-NNN` (+ optional `palette=brand`); picker tab "시라키 스타일" sends
  `theme=shiraki-NNN`, aliased to the style. See plugin README.
- Engine: OpenCode Path A (html2pptx + pptxgenjs); YAML spec inlined in the prompt.
- Fixes in this rev: Ubion logo passed to every OpenCode prompt (theme=ubion previously
  had none); theme fonts forced to S-Core Dream (latin/ea/cs); density-loop guard.
- Picker catalogs (served by the template gate, not tracked here): `slide-styles.json`
  gains `shiraki` + `shirakiGroups` (styleCount 250), new `slide-templates-shiraki.json`,
  `slide-picker-tabs.json` gains a `shiraki` tab grouped by category.
- Smoke (3 slides each): shiraki-190+brand, shiraki-003, ubion/narrative-keynote →
  engine opencode, logo present, S-Core Dream.
- YAML bodies and thumbnails are not bundled (third-party); use `fetch_shiraki.py`.
