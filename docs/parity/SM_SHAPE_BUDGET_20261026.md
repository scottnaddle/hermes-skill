# SM shape budget lock — workshop-teaching CT Quick — 2026-10-26 (Asia/Seoul)

Scott / 슬라이드 마스터 · **NO push**.

| Clock | Note |
|---|---|
| Written | **2026-10-26 ~09:33 KST** (Asia/Seoul) |
| SESSION_REV | `2026-10-26-c1-style-svg-recipes-shape-budget` |
| Mac skill | ubion-presentation **3.0.1-c1-style-svg-recipes-shape-budget** |
| Evidence | `SM_HERMES_AB_POSTFIX_20261026.md` · `sm-hermes-ab-postfix/loop-2/ct_compare.json` (6/17/15 vs 5/10/10) |
| Inbox | `/workspace/parity/parity-inbox/c1-shape-budget-20261026-093213` |

## Goal

Hermes live Mac CT Quick produced shape_counts **6/17/15**. SM baseline + box style-recipes smoke are **5/10/10**. Roles already match (Title Slide / Three-Card Synthesis / Process Timeline). Tighten recipes + ship Mac inbox with box-proven SVGs so Mac agents **copy structure**, not invent denser chrome.

## What changed

| Path | Change |
|---|---|
| `ppt-master/.../references/STYLE_PAGE_RECIPES.md` | SESSION_REV bump + **Shape budget** section (5/10/10, allowed/forbidden ids, clone reference-svg) |
| `parity/slides-port/mac-skill/STYLE_PAGE_RECIPES.md` | same |
| `parity/slides-port/mac-skill/SKILL.md` | version tag + shape-budget / reference-svg bullet |
| `ppt-master/.../styles/workshop-teaching/.../design_spec.md` VII | concise shape budget appended |
| `parity/theme-ubion/ppt-master-styles/workshop-teaching/.../design_spec.md` | synced (already had VII recipe block) |
| `tools/ppt-master/.../workshop-teaching/.../design_spec.md` | **not** invented — no Quick VII recipe block present |
| `ppt-master` + `tools` `layouts/editorial_bleed/.../design_spec.md` | Dark base plane: prefer `#0F172A` (not `#0B1220`); keep gradient rules |
| Inbox pack | recipes, SKILL, reference SVGs, SHAPE_BUDGET.md, INSTALL.md, HUASHU_NOTE.md |

## Shape budget (summary)

- Target: **5 / 10 / 10**
- Clone `reference-svg/workshop-teaching/{01_cover,02_key_rules,03_checklist}.svg` → rewrite text only
- Forbidden: `cover-footer*`, `page-head-rule`, `card-*-item-*`, `process-head-rule`, `step-card-*` / `step-badge-*` top-level groups

## Huashu `#0F172A`

Touched: editorial_bleed design_spec (ppt-master + tools mirrors). META baseline already `#0F172A`. No in-tree setter for `#0B1220` found (Hermes output only).

## Not done (by brief)

- Git commit/push
- Hermes Mac hop re-run
- LMS plugin edits
