---
name: ubion-presentation
description: "발표자료: /슬라이드는 연구(wiki/web) 후 daou_make_document(pptx, engine=opencode 기본 Path A). theme/style/pages/[layout]/[canvas]. engine=quick 제거(거부). OpenCode 실패→engine=legacy."
version: 3.0.5-remove-quick
author: Hermes Agent (daou profile)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [발표자료, PPT, pptx, 슬라이드, Presentation, 유비온, Korean, OpenCode, huashu, PathA]
    category: ubion-work
    related_skills: [ubion-office-documents, ubion-mac-content-studio, huashu-design, grounded-citations, ubion-company-info]
    SESSION_REV: 2026-10-29-remove-quick
---

# 발표자료 (daou → OpenCode Path A 기본 · Quick 제거)

## OpenCode Path A default (SESSION_REV=2026-10-29-remove-quick)

Plugin/SOUL default is **engine=opencode** (huashu HTML→export_deck_pptx). **Do not pass `engine=quick`** — it is removed and rejected with a clear error. Omit `engine` or set `engine=opencode`. OpenCode fail → honest **legacy** (LMS python-pptx) only — never silent Quick / Mac hop.

## Research-before-deck HARD (2026-10-26-slide-research-dense)

`/슬라이드`·발표자료 pptx면 **daou_make_document 호출 전** 아래 체크리스트:

1. 예외? `research=off` 또는 플래그 제외 본문 원고·목차 ≥~400자 → 조사 스킵 가능, 바로 3으로(장수·밀도는 유지).
2. 사내 키워드(휴가·연차·규정·복지·조직·메신저·근태·경조·양식 등)?
   - `company_wiki_search` → hit면 `company_wiki_page`
   - 수치·절차·조항을 blocks/brief에 넣고 Sources에 위키 페이지명
3. 위키 미스 / 일반·외부 주제(AI Agent, 기술, 시장…)?
   - `web_search` ≥1 + `web_extract` ≥2 출처 (**extract 생략 금지**)
   - 정의·핵심·예시·수치 + **Sources(제목·URL)** 를 blocks/brief에. 인용은 `grounded-citations` 가볍게 참고
4. **밀도 HARD:** `pages=N` → **exactly N slides, no fewer**. blocks에 level-1 heading ≥ N, 본문 유용 글자 ~≥120/장. Sources 섹션 필수(조사 시). thin/title-only면 도구 soft-reject → 보강 재시도(invent-from-title 금지).
5. **그 다음**에만 `daou_make_document`(format=`pptx`, **engine=opencode 기본**). 「exactly N slides, no fewer」.


## 디자인 라우터 (SESSION_REV=2026-10-29-remove-quick)

유비온봇 **PPT/슬라이드/발표자료** / **`/슬라이드`** 경로:

1. **연구 게이트** (위 HARD) → 근거를 brief/blocks에 정리
2. **기본** → `daou_make_document`(format `pptx`, **`engine=opencode` 기본** Path A; theme·style·pages·layout·canvas)
   - 트리거: 「발표자료」「PPT」「pptx」「슬라이드」「장표」· `/슬라이드 …`
3. **HTML 프로토·화슈 시안만** → `huashu-design`
4. **폴백** → OpenCode 실패 시 `engine=legacy`(LMS). **`engine=quick` 금지·제거** — 요청되면 거부 메시지; Mac Quick/PPT Master 사이드카 호출 안 함
5. **`gorden-ppt` DEPRECATED — 금지**

Word → `claude-office-docx`; 한글 → `hwpx-skill`.

## When to Use

- "발표자료 만들어줘", "PPT", "슬라이드", `/슬라이드 theme=…`

## 호출 (파라미터 보존)

클라이언트가 준 값을 **버리지 말고** arguments에 그대로 넣는다. `engine=quick`은 넣지 말 것.

```
daou_make_document(
  format="pptx",
  theme=<brand_id|ubion>,
  style=<style_id|workshop-teaching>,
  palette=<brand|생략>,  # style=shiraki-NNN 일 때만: brand = 테마 브랜드색(Ubion navy/teal)
  pages=<N>,
  canvas=<ppt169|ppt43>,
  layout=<presentation_core|report_core|editorial_bleed|생략>,
  engine="opencode",  # default Path A; never engine="quick" (removed)
  title=...,
  blocks=[...근거 반영...],  # 또는 path/brief 규약
)
```

기본값: theme=`ubion`, style=`workshop-teaching`, canvas=`ppt169`, layout=없음.

- **shiraki 스타일**: `/슬라이드 style=shiraki-NNN [palette=brand] pages=N` (No.001–236; shiraki-3 = shiraki-003 = shiraki003). YAML 디자인 스펙이 프롬프트에 인라인되고 기본은 YAML 원본 색, `palette=brand`는 Ubion 팔레트로 교체. 클라이언트 값 `style=`/`palette=`를 그대로 넘긴다. 로고(Ubion)·폰트(S-Core Dream, 한글·영문)는 항상 자동.
- `error=unknown_shiraki_style` → message(유효 범위 포함)를 그대로 안내.

- `status=running` → 「장표 만드는 중이에요(보통 3~8분).」 한 줄 후 턴 종료 (상태 폴링 금지 — SOUL과 동일).
- 결과 파일 절대 경로를 답에 그대로 적어 첨부. 장비명(Mac/VPS/hop) 금지 — 「OpenCode 화슈」/「화슈 Path A」까지. Quick/PPT Master 금지.
- 원고·아젠다·수치·Sources가 있으면 brief/blocks에 포함.
- `error=engine_quick_removed` → 「Quick은 제거됐어요. OpenCode(기본) 또는 legacy로 다시 요청해 주세요.」

## 개요 먼저

파일 전에 개요만 물으면 채팅으로 목차. "좋아요/만들어줘"면 연구 게이트 → OpenCode Path A.

## 폴백: `engine=legacy`

`engine=legacy` 또는 OpenCode 불가 시에만. 사용자에게 간이 엔진임을 고지. theme·style은 가능하면 전달. **연구 게이트는 legacy에도 동일** (예외 제외). Quick으로 조용히 바꾸지 말 것.

## Do not

- 연구 없이 `/슬라이드`를 바로 `daou_make_document`로 보내기 (`research=off`·긴 원고 예외 제외)
- theme/style/layout/canvas를 family·huashu slug로 치환
- `engine=quick` / Mac Quick / PPT Master sidecar / gorden-ppt / DNUE 팔레트 / 테마 브랜드 로고
- 시크릿·토큰 출력, git push
