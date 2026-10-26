# SM vs Hermes A/B POSTFIX — improve→test loop — 2026-10-26 (Asia/Seoul)

Scott voice · **NO push**. Artifacts: `/workspace/parity/sm-hermes-ab-postfix/`.

| Clock | Note |
|---|---|
| Written | **2026-10-26 ~09:43 KST** (Loop3) |
| LMS SESSION_REV | `2026-10-26-ab-parity-fixes-l3` |
| Mac skill | `ubion-presentation` **3.0.1-c1-style-svg-recipes-shape-budget** + `reference-svg/workshop-teaching` |
| SM inbox | `/workspace/parity/parity-inbox/c1-shape-budget-20261026-093213` |
| Prior AB | `/workspace/parity/SM_HERMES_AB_20261026.md` |

---

## Exit checklist (parent criteria)

| Case | palette / accents | layout structure | slides | logo / media | shape_counts | dark editorial | time |
|---|---|---|---|---|---|---|---|
| **A CT** | **Y** `#004EA2` | **Y** Title → Three-Card → Process | **Y** 3 | **Y** media=1 | **Y 5/10/10** | n/a | **residual** 228s vs ~86s |
| **B Huashu17** | **Y** `#0F172A` in srgbClr; accent `#CBD5E1`/`#F8FAFC` | **Y** Hero Full → Side Scrim → Full Statement | **Y** 3 | **Y** media=3 | n/a (roles OK) | **Y** no `#635BFF`; **`#0F172A` pin** | **residual** 129s vs ~65s |

**CT shape FULL MATCH** vs SM baseline inventory (same shape names). Latency remains infra residual — do not block.

---

## Before → After

### Case A — 中国电信 + workshop-teaching

| Metric | Loop2 | **Loop3** | SM baseline |
|---|---|---|---|
| Path | `loop-2/ct_workshop_l2.pptx` | **`loop-3/ct_workshop_l3.pptx`** | `baseline-ab-…/ct-workshop/…` |
| engine | quick | **quick** | ppt-master-quick |
| elapsed | 192s | **228.4s** | 85.6s |
| shape_counts | 6/17/15 | **5/10/10** | **5/10/10** |
| layouts | Title/Three-Card/Process | **same** | same |
| media | 1 | **1** | 1 |
| primary | `#004EA2` | `#004EA2` | `#004EA2` |

### Case B — 17-editorial-longform

| Metric | Loop2 | **Loop3** |
|---|---|---|
| engine | quick editorial_bleed | **quick** |
| dark token | `#0B1220` | **`#0F172A`** (no `#0B1220`) |
| purple | none | none |
| layouts | Hero/Scrim/Statement | **same** |
| media | 2 | **3** |
| elapsed | 141s | **129.4s** |

---

## Files patched (no git push)

| Host | File | Note |
|---|---|---|
| Mac | `…/ubion-presentation/{SKILL.md,STYLE_PAGE_RECIPES.md,SHAPE_BUDGET.md,HUASHU_NOTE.md,reference-svg/…}` | SM shape-budget pack |
| Mac | `ppt-master/…/references/STYLE_PAGE_RECIPES.md` + `templates/styles/STYLE_PAGE_RECIPES.md` | mirror |
| Mac | `templates/layouts/editorial_bleed/…/design_spec.md` | prefer `#0F172A` |
| LMS | `daou-workspace/__init__.py` | `…-ab-parity-fixes-l3` — SHAPE BUDGET brief + `#0F172A` academic brief |
| LMS | `daou-mac/__init__.py` | (unchanged from L2 early harvest) |

---

## Loop log

| Loop | What | Result |
|---|---|---|
| 1 | recipes + Huashu force-legacy; soffice hang → legacy | hop fail; H17 purple |
| 2 | early harvest; H17→Quick SM-remap | CT Quick core PASS; shapes 6/17/15; H17 `#0B1220` |
| **3** | SM shape-budget inbox + LMS brief clone lock + `#0F172A` pin; re-smoke | **CT 5/10/10 PASS**; **H17 `#0F172A` PASS**; latency residual only |

Evidence: `/workspace/parity/sm-hermes-ab-postfix/loop-{1,2,3}/`.

---

## Remaining diffs

1. **Latency only** — CT ~228s vs SM ~86s; H17 ~129s vs ~65s (Mac agent hop). Document; do not block quality MATCH.
2. ~~CT shape density~~ **CLOSED Loop3**.
3. ~~H17 `#0F172A`~~ **CLOSED Loop3**.

---

## Parent voice bullets (Korean) — Loop3 milestone

1. **달라졌던 점:** Loop2 CT shape 6/17/15(SM 5/10/10), H17 다크 `#0B1220`(목표 `#0F172A`).
2. **고친 것:** SM shape-budget 인박스(Mac 스킬 3.0.1-shape-budget + lean reference SVG) 설치, LMS Quick brief에 5/10/10 클론 강제(`SESSION_REV …-l3`), editorial_bleed `#0F172A` 핀.
3. **CT FULL MATCH:** shape **5/10/10**, Title→Three-Card→Process, media=1, `#004EA2`, Quick 228s.
4. **H17 핀 PASS:** srgbClr **`#0F172A`**, `#0B1220`/`#635BFF` 없음, Hero→Scrim→Statement, media=3, 129s.
5. **잔여:** 소요시간만(품질 비차단). **GitHub push 없음.**
