---
brand_id: ubion
kind: brand
summary: 유비온 corporate identity — deep navy and teal for Korean internal guides, HR/benefits, and executive decks
primary_color: "#04337B"
---

# 유비온 (Ubion) Brand Specification

> Identity-only preset. No SVG page roster — pages are composed freely under these constraints.
> DO NOT use DNUE (대구교대) offwhite+teal/amber palettes.

## I. Brand Overview

| Property | Value |
|---|---|
| Brand Name | 유비온 (Ubion) |
| Use Cases | 사내 안내, 복리후생, 교육·성장, 경영 보고, 제안·전략 장표 |
| Tone | 명확·신뢰·실무형. 답 먼저, 수치·근거 동반 |
| Sources | 사내 복리후생 안내 장표·OpenDesign 유비온 브랜드 토큰 (2026-10) |

## II. Color Scheme

| Role | HEX | Provenance | Notes |
|---|---|---|---|
| primary | `#04337B` | brand | Deep navy — titles, section fields, primary cards |
| primary-dark | `#032558` / `#02255A` | brand | Navy dark — section backgrounds, footer bands |
| accent | `#0D8782` | brand | Teal — KPI tiles, secondary cards, accent bars |
| bg | `#FFFFFF` | brand | Default light presentation background |
| surface | `#F5F7FA` | brand | Card / module soft surface |
| border | `#D0D7E2` | brand | Hairline rules, dividers, table borders |
| text | `#1A2332` | brand | Primary body text |
| muted-text | `#666666` / `#5A6872` | brand | Secondary text, captions |
| on-primary | `#FFFFFF` | brand | Text on navy/teal fills |

Keep accent genuinely selective: KPIs, key numbers, secondary card fills. Do not invent amber/orange/DNUE offwhite as identity.

## III. Typography

| Role | Family | Weight |
|---|---|---|
| title | `"Apple SD Gothic Neo", "Malgun Gothic", "Gothic A1", "Noto Sans KR", sans-serif` | 600–700 |
| body | `"Apple SD Gothic Neo", "Malgun Gothic", "Gothic A1", "Noto Sans KR", sans-serif` | 400 |

Prefer installed Korean gothic faces. Avoid Calibri as the deck face when Korean text dominates.

## IV. Logo

- File: `none` (unless user supplies an official asset into `images/`)
- Usage: omit logo chrome when no file is available; set presenting entity as editable text (`유비온 · 사내 안내`)

## V. Voice & Tone

- Formality: professional / 사내 안내
- Person: 저희 / 여러분 (Korean)
- Emoji: forbidden in decks
- Chrome labels: `유비온 · 사내 안내`, section label `섹션`

## VI. Icon Style

- Preference: stroke (`tabler-outline` family when icons are used)
- Devices to prefer: agenda cards (2×2), KPI tiles, filled cards, 2×3 tiles, two-panel compare, navy list box

## VII. Layout Devices (messenger default)

1. Cover — title + 2×2 agenda cards (navy/teal alternating)
2. KPI / 한눈에 — `라벨|값` big-number boxes
3. Section — navy field, label `섹션`
4. Cards / tiles — short items, navy/teal fills
5. Two-column or table for compare / criteria
