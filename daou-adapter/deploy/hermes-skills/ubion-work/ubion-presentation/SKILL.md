---
name: ubion-presentation
description: "발표자료: /슬라이드 theme style pages [layout] [canvas] 를 Mac ppt-master Quick으로 위임. 폴백만 daou_make_document(engine=legacy)."
version: 3.0.1-c1-style-svg-recipes
author: Hermes Agent (daou profile)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [발표자료, PPT, pptx, 슬라이드, Presentation, 유비온, Korean, ppt-master, Quick]
    category: ubion-work
    related_skills: [ubion-office-documents, ubion-mac-content-studio, huashu-design]
---

# 발표자료 (daou → Mac ppt-master Quick · C1)


## Mac Quick quality (2026-10-26)

Mac `ubion-presentation` **v3.0.1**: `STYLE_PAGE_RECIPES.md` — `workshop-teaching` → cover/three_card/process; brand logo on cover (`中国电信` 필수). daou는 파라미터 pass-through만; SVG 레시피는 Mac 스킬·ppt-master style/brand design_spec이 강제한다.

## 디자인 라우터 (2026-10-25-c1-quick-mac)

유비온봇 **PPT/슬라이드/발표자료** / **`/슬라이드`** 경로:

1. **기본 (필수)** → Mac `ubion-content` **`ubion-presentation`** (ppt-master **Quick Generate**)
   - 트리거: 「발표자료」「PPT」「pptx」「슬라이드」「장표」· `/슬라이드 …`
   - 실행: `ubion-mac-content-studio` + **`daou_mac_content`**
2. **HTML 프로토·화슈 시안만** → `huashu-design`
3. **폴백** (`engine=legacy` 또는 Mac·185 실패) → `daou_make_document`(format `pptx`) + 「간이 엔진」고지
4. **`gorden-ppt` DEPRECATED — 금지**

Word → `claude-office-docx`; 한글 → `hwpx-skill`.

## When to Use

- "발표자료 만들어줘", "PPT", "슬라이드", `/슬라이드 theme=…`

## Mac 위임 (기본 · 파라미터 보존)

클라이언트가 준 값을 **버리지 말고** request에 그대로 넣는다.

```
daou_mac_content(
  request="[ubion-presentation / ppt-master Quick]
    theme=<brand_id|ubion>
    style=<style_id|workshop-teaching>
    pages=<N>
    canvas=<ppt169|ppt43>
    layout=<presentation_core|report_core|editorial_bleed|생략>
    engine=quick
    주제/원고: …
    DNUE 금지. skill_view ubion-content/ubion-presentation 후 quick-generate.md.
    Brand+Style(+Layout) workspace 설치 → SVG → svg_to_pptx --quick-generate.
    out/에 pptx만. 답에 절대경로+theme/style/layout/canvas/장수.",
  wait_seconds=90
)
```

기본값: theme=`ubion`, style=`workshop-teaching`, canvas=`ppt169`, layout=없음.

- `status=running` → "맥에서 장표 만드는 중이에요(보통 3~8분)." 한 줄 후 턴 종료 (`daou_mac_status` 폴링 금지 — SOUL 영상과 동일).
- 결과 `files[].path` 절대 경로를 답에 그대로 적어 첨부.
- 원고·아젠다·수치가 있으면 request 전문에 포함.

## 개요 먼저

파일 전에 개요만 물으면 채팅으로 목차. "좋아요/만들어줘"면 Mac 위임.

## 폴백: `daou_make_document` pptx

`engine=legacy` 또는 Mac/185 불가 시에만. 사용자에게 간이 엔진임을 고지. theme·style은 가능하면 전달.

## Do not

- `/슬라이드`를 LMS `daou_make_document`로 먼저 보내기 (폴백 제외)
- theme/style/layout/canvas를 family·huashu slug로 치환
- gorden-ppt / DNUE 팔레트
- 시크릿·토큰 출력, git push
