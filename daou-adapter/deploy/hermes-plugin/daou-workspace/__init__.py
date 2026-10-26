"""daou-workspace: write sandbox + document generator for the 유비온봇 (daou) profile.

Policy (Scott, 2026-10-23): writes only inside the bot's per-room work folder
(``/var/lib/hermes-daou-work/<room>/``) or this profile's ``skills/``; everything else is blocked.
Allowed writes are still escalated to the approval gate with ``daou-write: <abs path>`` as the
description, and the adapter approves only when that path is inside the room's folder / skills/.
The self-improvement review fork (no approval bridge) may write learned/_drafts/ via skill_manage
or write_file/patch without the prompt — same confinement, no deletes, no daou_make_document. Reads (read_file / search_files) are confined to the same roots,
and read_file never opens .pdf/.pptx/… (see NO_READ_EXTS). No command execution: documents
are built by ``daou_make_document`` from structured blocks, never from code.
"""

from __future__ import annotations

SESSION_REV = "2026-10-26-ab-parity-fixes-l3"

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

WORK_ROOT = os.path.realpath(os.environ.get("DAOU_WORK_ROOT", "/var/lib/hermes-daou-work"))
PROFILE_HOME = Path(__file__).resolve().parents[2]
SKILLS_DIR = os.path.realpath(PROFILE_HOME / "skills")

# Gated FAQ: background_review may only create/patch drafts here (never approved/ or other skills).
LEARNED_DRAFTS_PREFIX = os.path.realpath(os.path.join(SKILLS_DIR, "ubion-work", "learned", "_drafts"))

# skill_manage must not delete these without Scott/curator (persona + this guard).
PROTECTED_SKILL_PREFIXES = tuple(
    os.path.realpath(os.path.join(SKILLS_DIR, rel))
    for rel in (
        "ubion-work/ubion-company-info",
        "ubion-work/ubion-policy-questions",
        "ubion-work/ubion-presentation",
        "ubion-work/ubion-office-documents",
        "ubion-work/claude-office-docx",
        "ubion-work/hwpx-skill",
        "ubion-work/experience-learning",
        "ubion-work/ubion-room-role",
        "ubion-work/learned/README.md",
        "creative/huashu-design",
        "ubion-policy-questions",
    )
)

# daou adapter sessions are ``daou_<room id, [^A-Za-z0-9_-] -> _>_<base36 ts>``.
_SESSION_RE = re.compile(r"^daou_([A-Za-z0-9_-]+)_[a-z0-9]+$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9가-힣_.\- ]+$")

WRITE_TOOLS = {"write_file", "patch", "daou_make_document"}
READ_TOOLS = {"read_file", "search_files"}
DOC_FORMATS = {"docx", "hwpx", "pdf", "pptx"}
MAX_DOC_BYTES = 20 * 1024 * 1024
# read_file on these lazily installs Hermes' "doc-extract" extra (tools/read_extract.py → pm
# ensure_import), which commits a PM-managed venv for the whole install; on 2026-10-23 that venv's
# Python 3.14 site-packages then broke every gateway boot (/opt/hermes-venv is 3.12). Never trigger it.
NO_READ_EXTS = {".doc", ".docm", ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm", ".xls", ".xlsm",
                ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub", ".pdf"}


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _in_learned_drafts(target: str) -> bool:
    return _under(os.path.realpath(target), LEARNED_DRAFTS_PREFIX)


def _is_protected_skill(target: str) -> bool:
    real = os.path.realpath(target)
    for pref in PROTECTED_SKILL_PREFIXES:
        if real == pref or real.startswith(pref + os.sep):
            return True
    return False


def room_dir(session_id: str) -> str | None:
    m = _SESSION_RE.match(session_id or "")
    return os.path.join(WORK_ROOT, m.group(1)) if m else None


def _resolve(raw: object) -> str | None:
    """Absolute, symlink-resolved path; None for relative / non-string paths."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    p = raw.strip()
    if p.startswith("~") or not os.path.isabs(p):
        return None
    return os.path.realpath(p)


def _skill_target(args: dict) -> str | None:
    """Path skill_manage will touch: skills/[<category>/]<name>[/<file>]; None if anything is unsafe.

    The advertised call shape is a batch (``operations: [{action, name, category?, file_path?}]``,
    top-level ``name`` as the default); every op must be safe, and the approval path is the deepest
    directory common to all of them (still inside skills/).
    """
    ops = args.get("operations")
    if ops is not None:
        if not isinstance(ops, list) or not ops:
            return None
        targets = [_skill_target({**op, "name": op.get("name") or args.get("name")}) if isinstance(op, dict) else None
                   for op in ops]
        if not all(targets):
            return None
        return targets[0] if len(targets) == 1 else os.path.commonpath(targets)
    name = args.get("name")
    category = args.get("category") or ""
    parts = [category, name] if category else [name]
    if not all(isinstance(s, str) and s and _SAFE_SEGMENT.match(s) and s not in (".", "..") for s in parts):
        return None
    fp = args.get("file_path")
    if fp is not None and (not isinstance(fp, str) or os.path.isabs(fp) or ".." in Path(fp).parts):
        return None
    return os.path.join(SKILLS_DIR, *parts, *( [fp] if fp else []))


def _in_background_review() -> bool:
    try:
        from tools.skill_provenance import is_background_review
    except ImportError:
        return False
    return is_background_review()


def _block(msg: str) -> dict:
    return {"action": "block", "message": msg}


def _decide(tool_name: str, args: dict, session_id: str) -> dict | None:
    if tool_name == "skill_manage":
        target = _skill_target(args)
        if target is None:
            return _block("skill_manage 거부: 스킬 이름/카테고리/파일 경로가 올바르지 않아요.")
        ops = args.get("operations") if isinstance(args.get("operations"), list) else [args]
        if _in_background_review():
            # Unattended review: learned/_drafts only; no deletes; no wiki/protected overwrites.
            if any(isinstance(op, dict) and op.get("action") in ("delete", "remove_file") for op in ops):
                return _block("skill_manage 거부: 백그라운드 리뷰에서는 스킬/파일 삭제를 하지 않아요.")
            if not _in_learned_drafts(target):
                return _block(
                    "skill_manage 거부: 백그라운드 리뷰는 ubion-work/learned/_drafts/ 초안만 쓸 수 있어요."
                )
            logger.info("daou-workspace: background-review learned-draft write %s", target)
            return None
        if any(isinstance(op, dict) and op.get("action") in ("delete", "remove_file") for op in ops):
            if _is_protected_skill(target):
                return _block("skill_manage 거부: 보호 스킬은 삭제할 수 없어요 (Scott/큐레이터만).")
        return {"action": "approve", "message": f"daou-write: {target}", "rule_key": "daou-write:skills"}

    if tool_name not in WRITE_TOOLS and tool_name not in READ_TOOLS:
        return None
    room = room_dir(session_id)
    roots = [r for r in (room, SKILLS_DIR) if r]
    path = _resolve(args.get("path"))
    if path is None:
        if tool_name == "search_files" and args.get("path") in (None, "", ".") and room:
            path = room
        else:
            hint = f" 예: {room}/파일명" if room else ""
            return _block(f"{tool_name} 거부: 절대 경로를 써 주세요.{hint}")
    if not any(_under(path, r) for r in roots):
        where = f"{room}/ 또는 {SKILLS_DIR}/" if room else f"{SKILLS_DIR}/"
        return _block(f"{tool_name} 거부: '{path}'는 허용된 작업 폴더 밖이에요. {where} 안에서만 가능해요.")
    if tool_name in READ_TOOLS:
        if tool_name == "read_file" and os.path.splitext(path)[1].lower() in NO_READ_EXTS:
            return _block(f"read_file 거부: {os.path.splitext(path)[1]} 파일은 읽을 수 없어요. "
                          "만든 문서는 다시 열어 확인하지 않아도 돼요 (생성 결과의 path/bytes로 충분).")
        if tool_name == "search_files" and args.get("path") != path:
            return {"action": "modify", "args": {"path": path}}
        return None
    # Background review has no adapter approval bridge (api_server = unattended).
    # Mirror skill_manage: allow write_file/patch only under learned/_drafts; never documents.
    if _in_background_review():
        if tool_name == "daou_make_document":
            return _block("daou_make_document 거부: 백그라운드 리뷰에서는 문서를 만들지 않아요.")
        if not _in_learned_drafts(path):
            return _block(
                f"{tool_name} 거부: 백그라운드 리뷰는 ubion-work/learned/_drafts/ 초안만 쓸 수 있어요."
            )
        logger.info("daou-workspace: background-review learned-draft %s %s", tool_name, path)
        return None
    if room and _under(path, room):
        os.makedirs(room, mode=0o755, exist_ok=True)
        os.chmod(room, 0o755)
    return {"action": "approve", "message": f"daou-write: {path}", "rule_key": "daou-write:file"}


def pre_tool_call(tool_name: str = "", args: dict | None = None, session_id: str = "", **_kw):
    try:
        a = args if isinstance(args, dict) else {}
        # Mutate in place so approve path still carries theme (modify+approve can't both
        # return from one callback; ref.args is the same dict).
        if tool_name == "daou_make_document":
            _inject_theme_into_args(a, session_id or "")
            _inject_style_into_args(a, session_id or "")
            _inject_slide_extras_into_args(a, session_id or "")
        return _decide(tool_name, a, session_id or "")
    except Exception as e:  # fail closed for anything we gate
        logger.warning("daou-workspace: guard error for %s: %s", tool_name, e)
        if tool_name in WRITE_TOOLS or tool_name in READ_TOOLS or tool_name == "skill_manage":
            return _block(f"{tool_name} 거부: 경로 검사 실패")
        return None


def post_tool_call(tool_name: str = "", args: dict | None = None, **_kw):
    """Make files written into the work folder readable by the adapter (non-root DynamicUser)."""
    if tool_name not in WRITE_TOOLS or not isinstance(args, dict):
        return
    path = _resolve(args.get("path"))
    if path and _under(path, WORK_ROOT) and os.path.isfile(path):
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass


# --- ppt-master brand_id router + huashu themes (SESSION_REV=2026-10-25-style-family-router)
# Messenger theme=<brand_id> → ppt-master brands_index / design_spec palette ONLY (identity).
# Messenger style=<style_id> → layout family via _PPT_STYLE_FAMILY (orthogonal to theme).
# Default/fallback theme: ubion. No style → family=corporate.
# Advanced: huashu numeric slugs 01-…~20-… still work when theme= explicit (their own family).
# Brands root: plugins/daou-workspace/assets/ppt-master-brands/ (slim: index + design_spec.md).
# Seed (ubion only): assets/ubion-master.pptx (= original.pptx).

_UBION_BRAND_PALETTE = {
    'primary': '04337B',
    'accent': '0D8782',
    'bg': 'FFFFFF',
    'surface': 'F5F7FA',
    'text': '1A2332',
    'muted': '666666',
    'on_fill': 'FFFFFF',
    'dark_primary': '032558',
    'mode': 'corporate',
    'chrome': 'side_bar',
    'family': 'corporate',
    'brand_id': 'ubion',
    'master': True,
    'footer': '유비온  ·  사내 안내',
}

# Huashu specimen themes ONLY (no brand_ids here — brands come from brands_index).
_HUASHU_PPT_THEMES = {
    "01-neo-swiss-billboard": {'primary': '2D5BFF', 'accent': '2D5BFF', 'bg': 'FFFFFF', 'surface': 'F0F3FF', 'text': '111111', 'muted': '555555', 'on_fill': 'FFFFFF', 'dark_primary': '1A3FCC', 'mode': 'billboard', 'chrome': 'top_rule'},
    "02-black-big-number-stage": {'primary': '2997FF', 'accent': '2997FF', 'bg': '000000', 'surface': '1A1A1A', 'text': 'FFFFFF', 'muted': 'A1A1A6', 'on_fill': '000000', 'dark_primary': '0A0A0A', 'mode': 'dark', 'chrome': 'minimal'},
    "03-mono-brand-type-hero": {'primary': '0A0A0A', 'accent': '0A0A0A', 'bg': '1ED760', 'surface': '16C251', 'text': '0A0A0A', 'muted': '1A3D24', 'on_fill': 'FFFFFF', 'dark_primary': '0A0A0A', 'mode': 'brand_fill', 'chrome': 'minimal'},
    "04-full-bleed-gradient-manifesto": {'primary': 'F4413F', 'accent': 'FF7A18', 'bg': 'FFFFFF', 'surface': 'FFF3ED', 'text': '111111', 'muted': '666666', 'on_fill': 'FFFFFF', 'dark_primary': 'C2186B', 'mode': 'manifesto', 'chrome': 'top_rule'},
    "05-candy-lecture-stage": {'primary': 'FF2D95', 'accent': '00E5FF', 'bg': 'FFFFFF', 'surface': 'FFF0F8', 'text': '1B1424', 'muted': '2A2A2A', 'on_fill': 'FFFFFF', 'dark_primary': '141414', 'mode': 'candy', 'chrome': 'top_rule'},
    "06-playful-collins-editorial": {'primary': 'E8472B', 'accent': '2B6BFF', 'bg': 'FFE01B', 'surface': 'FFF6A0', 'text': '111111', 'muted': '444444', 'on_fill': 'FFFFFF', 'dark_primary': 'B83620', 'mode': 'editorial', 'chrome': 'top_rule'},
    "07-irreverent-pop": {'primary': 'FF4500', 'accent': 'FFE01B', 'bg': 'FF4500', 'surface': 'FFF6D8', 'text': '111111', 'muted': '333333', 'on_fill': 'FFFFFF', 'dark_primary': 'CC3700', 'mode': 'pop', 'chrome': 'minimal'},
    "08-y2k-inflated-wrapped": {'primary': 'FF2E93', 'accent': 'FF8A00', 'bg': 'FF2E93', 'surface': 'FFE600', 'text': '111111', 'muted': '7A2400', 'on_fill': 'FFFFFF', 'dark_primary': 'B03C00', 'mode': 'y2k', 'chrome': 'minimal'},
    "09-bento-grid": {'primary': '0071E3', 'accent': '0071E3', 'bg': 'F5F5F7', 'surface': 'EAF3FF', 'text': '1D1D1F', 'muted': '6E6E73', 'on_fill': 'FFFFFF', 'dark_primary': '0058B0', 'mode': 'bento', 'chrome': 'top_rule'},
    "10-dark-hairline-terminal": {'primary': '7C7CFF', 'accent': '7C7CFF', 'bg': '0D0D0F', 'surface': '262629', 'text': 'EDEDEF', 'muted': '8B8B93', 'on_fill': '0D0D0F', 'dark_primary': '111113', 'mode': 'dark', 'chrome': 'hairline'},
    "11-two-font-consulting": {'primary': '051C2C', 'accent': '00805A', 'bg': 'EFEBE4', 'surface': 'D4DEE6', 'text': '27394A', 'muted': '5A6875', 'on_fill': 'FFFFFF', 'dark_primary': '051C2C', 'mode': 'consulting', 'chrome': 'top_rule'},
    "12-diagram-isotype": {'primary': '0B2545', 'accent': '1B6FD1', 'bg': 'F4F7FB', 'surface': 'E8F0FA', 'text': '0B2545', 'muted': '4A5F7A', 'on_fill': 'FFFFFF', 'dark_primary': '0B2545', 'mode': 'diagram', 'chrome': 'top_rule'},
    "13-diagrammatic-minimalism": {'primary': 'E4572E', 'accent': 'E4572E', 'bg': 'FAF9F6', 'surface': 'EDEBE6', 'text': '222222', 'muted': '6B6B6B', 'on_fill': 'FFFFFF', 'dark_primary': 'B84424', 'mode': 'minimal', 'chrome': 'top_rule'},
    "14-narrative-sparkline": {'primary': 'FF8A1F', 'accent': 'FF8A1F', 'bg': '0D1320', 'surface': '3A4560', 'text': 'E8ECF3', 'muted': 'B8C2D6', 'on_fill': '0D1320', 'dark_primary': '0D1320', 'mode': 'dark', 'chrome': 'hairline'},
    "15-assertion-evidence": {'primary': 'A63A2B', 'accent': 'A63A2B', 'bg': 'FFFEFB', 'surface': 'F7EDEA', 'text': '222222', 'muted': '666666', 'on_fill': 'FFFFFF', 'dark_primary': '7A2B20', 'mode': 'evidence', 'chrome': 'top_rule'},
    "16-institutional-swiss": {'primary': 'E5452B', 'accent': 'E5452B', 'bg': 'FFFFFF', 'surface': 'F5F5F5', 'text': '111111', 'muted': '6A6A6A', 'on_fill': 'FFFFFF', 'dark_primary': 'B83620', 'mode': 'swiss', 'chrome': 'top_rule'},
    "17-editorial-longform": {'primary': '635BFF', 'accent': '635BFF', 'bg': 'FBFAF8', 'surface': 'E8E5DE', 'text': '1B1A22', 'muted': '6A6872', 'on_fill': 'FFFFFF', 'dark_primary': '2B2A33', 'mode': 'longform', 'chrome': 'top_rule'},
    "18-humanist-rounded-cards": {'primary': '0A5C4B', 'accent': 'F6C453', 'bg': 'FBF6EC', 'surface': 'D9F2EA', 'text': '21302B', 'muted': '3E4F49', 'on_fill': 'FFFFFF', 'dark_primary': '0A3D32', 'mode': 'humanist', 'chrome': 'top_rule'},
    "19-dense-research-report": {'primary': '0066FF', 'accent': '0066FF', 'bg': 'FFFFFF', 'surface': 'F2F6FF', 'text': '111111', 'muted': '555555', 'on_fill': 'FFFFFF', 'dark_primary': '0050CC', 'mode': 'dense_research', 'chrome': 'research'},
    "20-all-text-manifesto": {'primary': 'E50914', 'accent': 'E50914', 'bg': '0A0A0A', 'surface': '1A1A1A', 'text': 'FFFFFF', 'muted': 'C9C9C9', 'on_fill': '0A0A0A', 'dark_primary': '0A0A0A', 'mode': 'manifesto_dark', 'chrome': 'minimal'},
}
_HUASHU_THEME_ALIASES = {
    "dense-research-report": "19-dense-research-report",
    "19": "19-dense-research-report",
    "neo-swiss-billboard": "01-neo-swiss-billboard",
    "black-big-number-stage": "02-black-big-number-stage",
    "bento-grid": "09-bento-grid",
    "all-text-manifesto": "20-all-text-manifesto",
    "ubion-corporate": "ubion",
    "corporate": "ubion",
    "navy": "ubion",
}

# --- layout families (SESSION_REV=2026-10-25-huashu-theme-layout-dna) ------------------------
# theme mode → layout family. _write_pptx renders each family with its own structure
# (type, margins, masthead/chrome, grid); "corporate"/theme=ubion keeps Ubion brand master renderers.
_HUASHU_MODE_FAMILY = {
    "corporate": "corporate",
    "longform": "magazine",
    "editorial": "magazine",
    "dense_research": "research",
    "evidence": "research",
    "consulting": "consulting",
    "swiss": "consulting",
    "billboard": "billboard",
    "diagram": "billboard",
    "dark": "dark_stage",
    "manifesto_dark": "dark_stage",
    "bento": "bento",
    "humanist": "bento",
    "manifesto": "manifesto",
    "brand_fill": "manifesto",
    "candy": "manifesto",
    "pop": "pop",
    "y2k": "pop",
    "minimal": "minimal",
}
for _t in _HUASHU_PPT_THEMES.values():
    _t.setdefault("family", _HUASHU_MODE_FAMILY.get(_t.get("mode", "corporate"), "corporate"))
del _t


# --- ppt-master style_id → layout family (SESSION_REV=2026-10-25-style-family-router) --------
# /슬라이드 theme=<brand_id> style=<style_id> pages=N
# Picker: workshop-teaching(default)→corporate; mbb/solution→consulting;
# academic/operating→research; narrative-keynote→billboard.
_PPT_STYLE_FAMILY = {
    "workshop-teaching": "corporate",
    "mbb-consulting": "consulting",
    "solution-proposal": "consulting",
    "academic-research": "research",
    "operating-review": "research",
    "narrative-keynote": "billboard",
    "consulting-decision": "consulting",
    "investor-pitch": "dark_stage",
    "technical-deepdive": "research",
    "product-launch": "bento",
    "science-explainer": "magazine",
    "creative-pitch": "manifesto",
    "duty-manual": "dark_stage",
    "incident-postmortem": "research",
}
_PPT_STYLE_ALIASES = {
    "teaching": "workshop-teaching",
    "consulting": "mbb-consulting",
    "pitch": "investor-pitch",
    "workshop": "workshop-teaching",
    "mbb": "mbb-consulting",
    "investor": "investor-pitch",
    "keynote": "narrative-keynote",
    "research": "academic-research",
    "proposal": "solution-proposal",
    "ops-review": "operating-review",
    "operating": "operating-review",
    "deepdive": "technical-deepdive",
    "tech": "technical-deepdive",
    "launch": "product-launch",
}
_FAM_TO_MODE = {
    "consulting": "consulting",
    "magazine": "longform",
    "research": "dense_research",
    "dark_stage": "dark",
    "bento": "bento",
    "manifesto": "manifesto",
    "billboard": "billboard",
    "pop": "pop",
    "minimal": "minimal",
    "corporate": "corporate",
}

# --- Quick style → SVG layout recipe (SESSION_REV=2026-10-26-ab-parity-fixes) ---------------
# Orthogonal to legacy _PPT_STYLE_FAMILY. Mac Quick uses these to pick layout_id + page roster
# so workshop-teaching ≠ mbb-consulting ≠ narrative-keynote (target SM structures).
_QUICK_STYLE_LAYOUT_RECIPE = {
    "workshop-teaching": {
        "layout": "presentation_core",
        "structure_3": ["cover", "three_card_rules", "process_checklist"],
        "page_types_3": ["title_slide", "three_card", "process_timeline"],
        "prototypes_3": ["01_title_slide", "12_three_card", "14_process_timeline"],
        "brief": (
            "STYLE RECIPE workshop-teaching → layout=presentation_core. "
            "For pages=3 use roster: cover(01_title_slide) → three_card_rules(12_three_card) "
            "→ process_checklist(14_process_timeline). Distinct from consulting/keynote. "
            "SHAPE BUDGET HARD: python-pptx shape_counts MUST be 5/10/10. "
            "Clone $HERMES_SKILL_DIR/reference-svg/workshop-teaching/{01_cover,02_key_rules,03_checklist}.svg "
            "into svg_output/ then rewrite text only. Forbidden top-level ids: cover-footer*, "
            "page-head-rule, card-head-*, card-*-item-*, process-head-rule, step-card-*, step-badge-*. "
            "Read STYLE_PAGE_RECIPES.md Shape budget section before writing SVG."
        ),
    },
    "mbb-consulting": {
        "layout": "presentation_core",
        "structure_3": ["cover", "kpi_dashboard", "two_content"],
        "page_types_3": ["title_slide", "kpi_dashboard", "two_content"],
        "prototypes_3": ["01_title_slide", "13_kpi_dashboard", "04_two_content"],
        "brief": (
            "STYLE RECIPE mbb-consulting → layout=presentation_core. "
            "pages=3: cover → kpi_dashboard → two_content (answer-first exhibits)."
        ),
    },
    "solution-proposal": {
        "layout": "presentation_core",
        "structure_3": ["cover", "comparison", "process_timeline"],
        "page_types_3": ["title_slide", "comparison", "process_timeline"],
        "prototypes_3": ["01_title_slide", "05_comparison", "14_process_timeline"],
        "brief": (
            "STYLE RECIPE solution-proposal → layout=presentation_core. "
            "pages=3: cover → comparison → process_timeline."
        ),
    },
    "narrative-keynote": {
        "layout": "editorial_bleed",
        "structure_3": ["hero_full_cover", "split_bleed", "full_statement_closing"],
        "page_types_3": ["hero_full", "split_bleed", "statement"],
        "prototypes_3": ["01_hero_full", "03_split_bleed", "05_statement"],
        "brief": (
            "STYLE RECIPE narrative-keynote → layout=editorial_bleed. "
            "pages=3: hero_full_cover → split_bleed → full_statement_closing."
        ),
    },
    "academic-research": {
        "layout": "editorial_bleed",
        "structure_3": ["hero_full_cover", "hero_side_scrim_highlights", "full_statement_closing"],
        "page_types_3": ["hero_full", "split_bleed", "statement"],
        "prototypes_3": ["01_hero_full", "03_split_bleed", "05_statement"],
        "brief": (
            "STYLE RECIPE academic-research → layout=editorial_bleed. "
            "pages=3: hero_full_cover → hero_side_scrim_highlights → full_statement_closing "
            "(SM Huashu17 baseline structure). Dark base plane fill prefer #0F172A (not #0B1220)."
        ),
    },
    "operating-review": {
        "layout": "report_core",
        "structure_3": ["cover", "kpi_dashboard", "table_summary"],
        "page_types_3": ["title_slide", "kpi_dashboard", "table_summary"],
        "prototypes_3": ["01_title_slide", "13_kpi_dashboard", "20_table_summary"],
        "brief": (
            "STYLE RECIPE operating-review → layout=report_core (or presentation_core if report_core missing). "
            "pages=3: cover → kpi_dashboard → table_summary."
        ),
    },
    "investor-pitch": {
        "layout": "editorial_bleed",
        "structure_3": ["hero_full_cover", "kpi_dashboard", "full_statement_closing"],
        "page_types_3": ["hero_full", "kpi_dashboard", "statement"],
        "prototypes_3": ["01_hero_full", "13_kpi_dashboard", "05_statement"],
        "brief": (
            "STYLE RECIPE investor-pitch → layout=editorial_bleed. "
            "pages=3: hero cover → traction KPIs → closing statement."
        ),
    },
}


def _is_huashu_specimen_theme(raw) -> bool:
    """True when theme is a Huashu 01–20 specimen slug (not a ppt-master brand_id).

    brand_id path (中国电信, ubion, mckinsey, …) must stay on Quick when requested.
    """
    slug = _normalize_theme_slug(raw)
    if not slug:
        return False
    # brand_id always wins — never force-legacy a real brand folder
    if _lookup_ppt_brand_id(slug):
        return False
    if slug in _HUASHU_PPT_THEMES:
        return True
    # NN-… pattern even if not in dict (future specimens)
    if re.fullmatch(r"\d{2}-[a-z0-9-]+", slug):
        return True
    return False


def _quick_style_recipe(style_raw) -> dict | None:
    sid = _normalize_style_slug(style_raw) or "workshop-teaching"
    return _QUICK_STYLE_LAYOUT_RECIPE.get(sid)


def _hex_lum(h: str) -> float:
    """Relative luminance (0..1) of an RRGGBB hex string."""
    h = str(h).strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _darken_hex(h: str, factor: float = 0.65) -> str:
    h = str(h).strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"{int(r * factor):02X}{int(g * factor):02X}{int(b * factor):02X}"


_PPT_MASTER_BRANDS_ROOTS = (
    Path(__file__).resolve().parent / "assets" / "ppt-master-brands",
    PROFILE_HOME / "skills" / "ubion-work" / "ubion-presentation" / "assets" / "ppt-master-brands",
    Path("/workspace/tools/ppt-master/skills/ppt-master/templates/brands"),
    Path("/workspace/parity/theme-ubion/ppt-master-brands"),
)

_PPT_BRAND_INDEX_CACHE: dict | None = None
_PPT_BRAND_PALETTE_CACHE: dict[str, dict] = {}


def _ppt_master_brands_root() -> Path | None:
    for p in _PPT_MASTER_BRANDS_ROOTS:
        try:
            if (p / "brands_index.json").is_file():
                return p
        except OSError:
            continue
    return None


def _load_ppt_brand_index() -> dict:
    global _PPT_BRAND_INDEX_CACHE
    if _PPT_BRAND_INDEX_CACHE is not None:
        return _PPT_BRAND_INDEX_CACHE
    root = _ppt_master_brands_root()
    if not root:
        _PPT_BRAND_INDEX_CACHE = {}
        return _PPT_BRAND_INDEX_CACHE
    try:
        import json as _json
        _PPT_BRAND_INDEX_CACHE = _json.loads((root / "brands_index.json").read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("daou-workspace: brands_index load failed: %s", e)
        _PPT_BRAND_INDEX_CACHE = {}
    return _PPT_BRAND_INDEX_CACHE


def _ppt_brand_ids() -> list[str]:
    return sorted(_load_ppt_brand_index().keys())


def _lookup_ppt_brand_id(raw: str) -> str | None:
    """Case-insensitive ASCII match; exact match for non-ASCII keys (중국电信 등)."""
    if not raw:
        return None
    idx = _load_ppt_brand_index()
    if raw in idx:
        return raw
    low = raw.lower()
    for k in idx:
        if k.lower() == low:
            return k
    return None


def _parse_brand_design_spec(spec_path: Path, brand_id: str, index_meta: dict | None = None) -> dict:
    """Map ppt-master design_spec.md Color Scheme → corporate renderer palette."""
    text = spec_path.read_text(encoding="utf-8")
    roles: dict[str, str] = {}
    # Table rows: | primary | `#04337B` |  — take first hex per role
    for m in re.finditer(
        r"\|\s*([^|]+?)\s*\|\s*`#([0-9A-Fa-f]{6})`",
        text,
    ):
        role = re.sub(r"\s+", " ", m.group(1).strip().lower())
        hx = m.group(2).upper()
        # normalize role aliases
        role = role.replace("muted-text", "muted").replace("on-primary", "on_fill")
        role = role.replace("primary-dark", "dark_primary").replace("primary dark", "dark_primary")
        if "(" in role:  # e.g. accent (warm)
            role = role.split("(", 1)[0].strip()
        if role not in roles:
            roles[role] = hx
    # frontmatter primary_color fallback
    fm = re.search(r"primary_color:\s*[\"']?#([0-9A-Fa-f]{6})", text)
    primary = roles.get("primary") or (fm.group(1).upper() if fm else None)
    if not primary and index_meta:
        pc = str((index_meta or {}).get("primary_color") or "").strip().lstrip("#")
        if re.fullmatch(r"[0-9A-Fa-f]{6}", pc):
            primary = pc.upper()
    if not primary:
        primary = "04337B"
    accent = roles.get("accent") or roles.get("secondary") or primary
    bg = roles.get("bg") or "FFFFFF"
    surface = roles.get("surface") or "F5F7FA"
    text_c = roles.get("text") or ("FFFFFF" if _hex_lum(bg) < 0.35 else "1A2332")
    muted = roles.get("muted") or "666666"
    on_fill = roles.get("on_fill") or ("1A2332" if _hex_lum(primary) > 0.55 else "FFFFFF")
    dark_primary = roles.get("dark_primary") or _darken_hex(primary, 0.7)

    # Corporate renderer: NAVY=field/title, TEAL=accent highlight.
    # If primary is bright and accent is dark (e.g. AWS orange+navy), use dark as field.
    field, highlight = primary, accent
    if _hex_lum(primary) > 0.45 and accent and _hex_lum(accent) < 0.35:
        field, highlight = accent, primary
        dark_primary = _darken_hex(field, 0.75)
    elif _hex_lum(accent) < 0.08 and _hex_lum(primary) > 0.2:
        # accent is near-black (Accenture/Deloitte) — keep primary as field, use darkened primary as accent bar twin
        highlight = primary
        dark_primary = "111111" if _hex_lum(accent) < 0.08 else dark_primary

    # Brand display name
    bm = re.search(r"\|\s*Brand Name\s*\|\s*([^|]+)\|", text, flags=re.I)
    brand_name = (bm.group(1).strip() if bm else brand_id)
    if brand_id == "ubion":
        footer = "유비온  ·  사내 안내"
    else:
        footer = f"{brand_name}  ·  Presentation"

    return {
        "primary": field,
        "accent": highlight,
        "bg": bg,
        "surface": surface,
        "text": text_c,
        "muted": muted,
        "on_fill": on_fill,
        "dark_primary": dark_primary,
        "mode": "corporate",
        "chrome": "side_bar",
        "family": "corporate",
        "brand_id": brand_id,
        "master": True,
        "footer": footer,
        "brand_name": brand_name,
        "index_primary": primary,
        "spec_primary": roles.get("primary") or primary,
        "spec_accent": roles.get("accent") or accent,
    }


def _load_ppt_brand_palette(brand_id: str) -> dict | None:
    if brand_id in _PPT_BRAND_PALETTE_CACHE:
        return dict(_PPT_BRAND_PALETTE_CACHE[brand_id])
    root = _ppt_master_brands_root()
    idx = _load_ppt_brand_index()
    if not root or brand_id not in idx:
        if brand_id == "ubion":
            return dict(_UBION_BRAND_PALETTE)
        return None
    spec = root / brand_id / "templates" / "design_spec.md"
    if not spec.is_file():
        if brand_id == "ubion":
            return dict(_UBION_BRAND_PALETTE)
        # index-only fallback
        pc = str(idx[brand_id].get("primary_color") or "04337B").strip().lstrip("#").upper()
        pal = dict(_UBION_BRAND_PALETTE)
        pal.update({
            "primary": pc,
            "accent": pc,
            "dark_primary": _darken_hex(pc),
            "brand_id": brand_id,
            "footer": f"{brand_id}  ·  Presentation",
            "brand_name": brand_id,
            "index_primary": pc,
            "spec_primary": pc,
            "spec_accent": pc,
        })
        _PPT_BRAND_PALETTE_CACHE[brand_id] = pal
        return dict(pal)
    try:
        pal = _parse_brand_design_spec(spec, brand_id, idx.get(brand_id))
    except Exception as e:
        logger.warning("daou-workspace: brand spec parse failed %s: %s", brand_id, e)
        if brand_id == "ubion":
            return dict(_UBION_BRAND_PALETTE)
        return None
    _PPT_BRAND_PALETTE_CACHE[brand_id] = pal
    return dict(pal)


def _normalize_theme_slug(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.split("/")[-1]
    if s.endswith(".html") or s.endswith(".jpg"):
        s = s.rsplit(".", 1)[0]
    # Preserve non-ASCII brand ids; lowercase ASCII for stable matching
    if re.fullmatch(r"[A-Za-z0-9 _.\-]+", s):
        s = s.lower().replace("_", "-").strip()
    else:
        s = s.replace("_", "-").strip()
    if s in _HUASHU_THEME_ALIASES:
        return _HUASHU_THEME_ALIASES[s]
    if s in _HUASHU_PPT_THEMES:
        return s
    bid = _lookup_ppt_brand_id(s)
    if bid:
        return bid
    # fuzzy huashu (numeric specimens only)
    for key in _HUASHU_PPT_THEMES:
        if not (len(key) > 2 and key[:2].isdigit()):
            continue
        bare = key.split("-", 1)[-1]
        if key.endswith(s) or s == bare or s.endswith(bare):
            return key
    return s


def _resolve_pptx_theme(raw) -> tuple[str, dict]:
    """Return (slug, palette). theme=<ppt-master brand_id> or huashu slug; default ubion."""
    slug = _normalize_theme_slug(raw)
    if slug:
        # Prefer ppt-master brand_id when both could apply (ubion is a brand, not huashu)
        bid = _lookup_ppt_brand_id(slug)
        if bid:
            pal = _load_ppt_brand_palette(bid)
            if pal:
                return bid, pal
        if slug in _HUASHU_PPT_THEMES:
            return slug, dict(_HUASHU_PPT_THEMES[slug])
    pal = _load_ppt_brand_palette("ubion") or dict(_UBION_BRAND_PALETTE)
    return "ubion", pal


def _find_ubion_master_seed() -> str | None:
    """Locate ubion PPT master seed (original.pptx / ubion-master.pptx)."""
    candidates = (
        Path(__file__).resolve().parent / "assets" / "ubion-master.pptx",
        Path(__file__).resolve().parent / "ubion-master.pptx",
        PROFILE_HOME / "skills" / "ubion-work" / "ubion-presentation" / "assets" / "ubion-master.pptx",
        PROFILE_HOME / "skills" / "ubion-work" / "ubion-presentation" / "ubion-master.pptx",
    )
    for p in candidates:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


def _pptx_clear_slides(prs) -> None:
    """Drop every slide from an opened Presentation (keep masters/layouts/theme)."""
    sldIdLst = prs.slides._sldIdLst  # noqa: SLF001 — python-pptx public-enough API
    for sldId in list(sldIdLst):
        rId = sldId.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        if rId:
            try:
                prs.part.drop_rel(rId)
            except Exception:
                pass
        sldIdLst.remove(sldId)


def _pptx_open_presentation(pal: dict):
    """For ubion brand: open seed pptx (cleared). Else blank Presentation.

    Returns (prs, blank_layout, seed_path_or_None).
    """
    from pptx import Presentation
    from pptx.util import Inches

    use_seed = bool(pal.get("brand_id") == "ubion" and pal.get("master"))
    seed = _find_ubion_master_seed() if use_seed else None
    if seed:
        try:
            prs = Presentation(seed)
            _pptx_clear_slides(prs)
            blank = None
            for layout in prs.slide_layouts:
                name = (layout.name or "").strip().lower()
                if name in ("blank", "빈 화면", "빈화면", "빈 슬라이드"):
                    blank = layout
                    break
            if blank is None:
                blank = prs.slide_layouts[min(6, len(prs.slide_layouts) - 1)]
            prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
            return prs, blank, seed
        except Exception as e:
            logger.warning("daou-workspace: ubion seed open failed (%s); blank prs", e)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    return prs, prs.slide_layouts[6], None


# --- style= resolve (SESSION_REV=2026-10-25-style-family-router) ------------------------------
def _normalize_style_slug(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.split("/")[-1]
    if s.endswith(".html") or s.endswith(".jpg"):
        s = s.rsplit(".", 1)[0]
    s = s.lower().replace("_", "-").strip().rstrip(".,;:)]}\"'")
    if not s:
        return None
    if s in _PPT_STYLE_ALIASES:
        s = _PPT_STYLE_ALIASES[s]
    if s in _PPT_STYLE_FAMILY:
        return s
    for k in _PPT_STYLE_FAMILY:
        if k == s or k.endswith("-" + s) or s.endswith(k):
            return k
    return None


def _resolve_pptx_style(raw) -> tuple[str | None, str]:
    """Return (style_id or None, layout family). No/unknown style → family=corporate."""
    sid = _normalize_style_slug(raw)
    if not sid:
        return None, "corporate"
    return sid, _PPT_STYLE_FAMILY.get(sid, "corporate")


def _apply_style_to_palette(pal: dict, style_raw) -> tuple[dict, str | None, str]:
    """Overlay style family onto a theme palette (palette colors unchanged)."""
    sid, family = _resolve_pptx_style(style_raw)
    out = dict(pal)
    if not sid:
        return out, None, out.get("family") or "corporate"
    out["family"] = family
    out["style_id"] = sid
    out["mode"] = _FAM_TO_MODE.get(family, out.get("mode") or "corporate")
    if family == "dark_stage":
        out["chrome"] = "minimal"
    elif family == "corporate":
        out.setdefault("chrome", out.get("chrome") or "side_bar")
    elif family in ("consulting", "magazine", "research", "bento", "billboard"):
        out["chrome"] = "top_rule"
    return out, sid, family


# --- /슬라이드 key=value parse + Quick sidecar (SESSION_REV=2026-10-25-c1-quick-mac) ----
# Messenger: `/슬라이드 theme=<brand> style=<style_id> pages=N [layout=…] [canvas=…] [engine=quick|legacy]`
# Parser is a general key=value dict (unknown keys ignored). Backward-compat: theme/style/pages.
# engine default = quick (env PPT_ENGINE). Quick path skips _PPT_STYLE_FAMILY; legacy keeps it.
_SLIDE_CMD_RE = re.compile(r"/슬라이드\b", re.IGNORECASE)
_SLIDE_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s]+)")
_SLIDE_KNOWN_KEYS = frozenset({"theme", "style", "pages", "layout", "canvas", "engine", "deck", "notes", "anim"})
_PENDING_THEME_NAME = ".pending_huashu_theme"
_PENDING_STYLE_NAME = ".pending_huashu_style"
_PENDING_SLIDE_KV_NAME = ".pending_slide_kv"
_HARD_THEME_TAG = "[HARD daou-slide-theme]"
_HARD_STYLE_TAG = "[HARD daou-slide-style]"
# Kept for any external grep / older callers
_SLIDE_THEME_RE = re.compile(r"/슬라이드\s+theme=(\S+)", re.IGNORECASE)
_SLIDE_STYLE_RE = re.compile(r"\bstyle=(\S+)", re.IGNORECASE)


def _parse_slide_kv(text: object) -> dict:
    """Parse `/슬라이드 … key=value …` into a dict. Unknown keys kept but ignored by callers."""
    if not isinstance(text, str) or not text.strip():
        return {}
    if not _SLIDE_CMD_RE.search(text):
        # Still allow bare key=value on tool-arg echo lines that include theme=/style= without /슬라이드
        # only when at least one known key is present (backward compat for history scans).
        raw = dict(_SLIDE_KV_RE.findall(text))
        if not raw:
            return {}
        if not any(k.lower() in _SLIDE_KNOWN_KEYS for k in raw):
            return {}
        out = {k.lower(): v.strip().rstrip(".,;:)]}\"'") for k, v in raw.items()}
        return out
    out = {}
    for m in _SLIDE_KV_RE.finditer(text):
        k = m.group(1).lower()
        v = m.group(2).strip().rstrip(".,;:)]}\"'")
        out[k] = v
    return out


def _extract_slide_theme(text: object) -> str | None:
    kv = _parse_slide_kv(text)
    raw = kv.get("theme")
    if not raw:
        # legacy regex fallback when /슬라이드 absent but theme= present
        if isinstance(text, str):
            m = _SLIDE_THEME_RE.search(text) or re.search(r"\btheme=(\S+)", text, re.I)
            if m:
                raw = m.group(1).strip().rstrip(".,;:)]}\"'")
    return _normalize_theme_slug(raw) if raw else None


def _extract_slide_style(text: object) -> str | None:
    kv = _parse_slide_kv(text)
    raw = kv.get("style")
    if not raw and isinstance(text, str):
        m = _SLIDE_STYLE_RE.search(text)
        if m:
            raw = m.group(1).strip().rstrip(".,;:)]}\"'")
    return _normalize_style_slug(raw) if raw else None


def _extract_slide_engine(text: object) -> str | None:
    kv = _parse_slide_kv(text)
    raw = (kv.get("engine") or "").strip().lower()
    if raw in ("quick", "legacy"):
        return raw
    return None


def _pending_slide_kv_path(session_or_chat_id: str) -> str | None:
    room = room_dir(session_or_chat_id or "")
    if not room:
        return None
    return os.path.join(room, _PENDING_SLIDE_KV_NAME)


def _stash_pending_slide_kv(session_or_chat_id: str, kv: dict) -> None:
    path = _pending_slide_kv_path(session_or_chat_id)
    if not path or not isinstance(kv, dict):
        return
    keep = {k: str(v) for k, v in kv.items() if k in _SLIDE_KNOWN_KEYS and v is not None and str(v).strip()}
    if not keep:
        return
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(keep, fh, ensure_ascii=False)
        os.chmod(path, 0o644)
        logger.info("daou-workspace: stashed pending slide kv %s -> %s", keep, path)
    except OSError as e:
        logger.warning("daou-workspace: stash slide kv failed: %s", e)


def _read_pending_slide_kv(session_or_chat_id: str, *, consume: bool = False) -> dict:
    path = _pending_slide_kv_path(session_or_chat_id)
    if not path or not os.path.isfile(path):
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out = {str(k).lower(): str(v) for k, v in raw.items()}
    except (OSError, ValueError):
        return {}
    if consume and out:
        try:
            os.unlink(path)
        except OSError:
            pass
    return out


def _resolve_ppt_engine(params: dict | None = None, session_id: str = "") -> str:
    """quick (default) | legacy. Order: tool arg → pending kv → env PPT_ENGINE → quick."""
    if isinstance(params, dict):
        e = str(params.get("engine") or "").strip().lower()
        if e in ("quick", "legacy"):
            return e
    sid = _normalize_daou_session_id(session_id) if session_id else ""
    if sid:
        kv = _read_pending_slide_kv(sid, consume=False)
        e = str(kv.get("engine") or "").strip().lower()
        if e in ("quick", "legacy"):
            return e
    if isinstance(params, dict):
        path = _resolve(params.get("path")) if params.get("path") else None
        if path and _under(path, WORK_ROOT):
            room = os.path.basename(os.path.dirname(path))
            if room:
                kv = _read_pending_slide_kv(f"daou_{room}_x", consume=False)
                e = str(kv.get("engine") or "").strip().lower()
                if e in ("quick", "legacy"):
                    return e
    env = (os.environ.get("PPT_ENGINE") or "quick").strip().lower()
    return env if env in ("quick", "legacy") else "quick"


def _huashu_sm_quick_remap(theme_raw) -> dict | None:
    """SM-parity Quick remap for select Huashu editorial slugs (dark editorial_bleed).

    SESSION_REV=2026-10-26-ab-parity-fixes-l3: 17-editorial-longform matches SM baseline
    (brand none-like dark #0F172A, academic-research, editorial_bleed) via Mac Quick —
    not LMS magazine purple (#635BFF). Other specimens still force legacy DNA.
    """
    slug = _normalize_theme_slug(theme_raw)
    if not slug or not _is_huashu_specimen_theme(theme_raw):
        return None
    # editorial / longform family → SM dark editorial_bleed path
    if slug in ("17-editorial-longform",) or slug.endswith("editorial-longform") or slug.endswith("-editorial"):
        return {
            "theme_wire": slug,  # Mac derives brand colors from brief
            "style": "academic-research",
            "layout": "editorial_bleed",
            "structure_3": ["hero_full_cover", "hero_side_scrim_highlights", "full_statement_closing"],
            "palette": {
                "primary": "#0F172A",
                "accent": "#CBD5E1",
                "bg": "#0F172A",
                "text": "#F8FAFC",
            },
            "brief": (
                "HUASHU→SM REMAP 17-editorial-longform: DO NOT use magazine purple #635BFF. "
                "Create derived brand (or brand=none) with primary #0F172A, accent #CBD5E1, "
                "bg #0F172A, text #F8FAFC. layout=editorial_bleed style=academic-research. "
                "pages=3 structure: hero_full_cover → hero_side_scrim_highlights → "
                "full_statement_closing (bleed PNGs/scrims OK). NanumMyeongjo titles preferred."
            ),
        }
    return None


def _effective_ppt_engine(params: dict | None = None, session_id: str = "") -> str:
    """Like _resolve_ppt_engine, with Huashu routing:

    - editorial/longform specimens (17-…) → keep Quick for SM dark editorial remap
    - other Huashu specimens → force legacy (Huashu DNA)
    - brand_id (中国电信/ubion/…) → unchanged Quick/legacy from resolve
    """
    eng = _resolve_ppt_engine(params, session_id)
    theme_raw = None
    if isinstance(params, dict):
        theme_raw = params.get("theme")
    if not theme_raw and session_id:
        try:
            theme_raw = _read_pending_theme(session_id, consume=False)
        except Exception:
            theme_raw = None
    if _huashu_sm_quick_remap(theme_raw):
        # Stay on Quick so Mac can render editorial_bleed dark (SM parity)
        if eng == "legacy" and str((params or {}).get("engine") or "").strip().lower() != "legacy":
            # pending/env said legacy but theme wants SM remap — only honor explicit tool engine=legacy
            pass
        logger.info(
            "daou-workspace: huashu editorial theme=%s → Quick SM-remap (dark editorial_bleed)",
            _normalize_theme_slug(theme_raw),
        )
        return "quick" if str((params or {}).get("engine") or "").strip().lower() != "legacy" else "legacy"
    if _is_huashu_specimen_theme(theme_raw):
        if eng != "legacy":
            logger.info(
                "daou-workspace: huashu specimen theme=%s → force engine=legacy (was %s)",
                _normalize_theme_slug(theme_raw),
                eng,
            )
        return "legacy"
    return eng


def _inject_slide_extras_into_args(args: dict, session_id: str = "") -> dict:
    """Mutate args with layout/canvas/engine/pages from pending kv when missing."""
    if not isinstance(args, dict) or str(args.get("format") or "").lower() != "pptx":
        return {}
    sid = _normalize_daou_session_id(session_id)
    kv = _read_pending_slide_kv(sid, consume=False) if sid else {}
    if not kv and args.get("path"):
        path = _resolve(args.get("path"))
        if path and _under(path, WORK_ROOT):
            room = os.path.basename(os.path.dirname(path))
            if room:
                kv = _read_pending_slide_kv(f"daou_{room}_x", consume=False)
    applied = {}
    for key in ("layout", "canvas", "engine", "pages", "deck"):
        if key in args and args.get(key) not in (None, ""):
            continue
        if key in kv and kv[key] not in (None, ""):
            val = kv[key]
            if key == "pages":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    continue
            args[key] = val
            applied[key] = val
    if applied:
        logger.info("daou-workspace: inject slide extras %s session=%s", applied, session_id or "?")
    return applied


def _blocks_to_quick_brief(title: str, blocks: list) -> str:
    """Serialize structured blocks into a short markdown brief for the Quick agent."""
    lines = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "heading":
            lvl = int(b.get("level") or 1)
            prefix = "#" * min(max(lvl, 1), 3)
            lines.append(f"{prefix} {b.get('text') or ''}")
            lay = b.get("layout")
            if lay and lay != "auto":
                lines.append(f"(layout: {lay})")
        elif t == "paragraph":
            lines.append(str(b.get("text") or ""))
        elif t == "bullets":
            for it in b.get("items") or []:
                lines.append(f"- {it}")
        elif t == "table":
            rows = b.get("rows") or []
            for i, row in enumerate(rows[:12]):
                if isinstance(row, list):
                    lines.append("| " + " | ".join(str(c) for c in row) + " |")
                    if i == 0:
                        lines.append("| " + " | ".join("---" for _ in row) + " |")
        lines.append("")
    return "\n".join(lines).strip()[:12000]


def _load_daou_mac_mod():
    """Import sibling daou-mac plugin (failover hop client)."""
    import importlib.util
    mac_path = Path(__file__).resolve().parent.parent / "daou-mac" / "__init__.py"
    if not mac_path.is_file():
        raise FileNotFoundError(f"daou-mac plugin missing: {mac_path}")
    spec = importlib.util.spec_from_file_location("daou_mac_plugin_c1", str(mac_path))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load daou-mac spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _quick_sidecar(
    path: str,
    title: str,
    blocks: list,
    *,
    theme: str | None = None,
    style: str | None = None,
    layout: str | None = None,
    canvas: str | None = None,
    pages: int | None = None,
    deck: str | None = None,
    session_id: str = "",
    wait_seconds: int | None = None,
) -> dict | None:
    """Post a Quick Generate slides job via daou-mac hop; copy .pptx into ``path``.

    Returns a report dict on success, or None to signal legacy ``_write_pptx`` fallback.
    Does NOT call ``_PPT_STYLE_FAMILY`` — theme/style/layout/canvas pass through to the brief.
    Mac skill/SOUL may be WIP: any failure/timeout/empty-out → None (fail-closed).
    """
    import shutil
    import time as _time

    wait = wait_seconds
    if wait is None:
        try:
            # Quick Generate (Mac) typically 2–6 min; smoke Walmart ~282s. 90 was too short → false legacy.
            wait = int(os.environ.get("PPT_QUICK_WAIT", "420"))
        except ValueError:
            wait = 420
    wait = max(15, min(int(wait), 900))

    try:
        mac = _load_daou_mac_mod()
    except Exception as e:
        logger.warning("daou-workspace: quick sidecar cannot load daou-mac: %s", e)
        return None

    # Prefer calling the public tool handler so hop select + bg-follow stay shared.
    room = os.path.basename(os.path.dirname(path))
    # Synthetic session so daou-mac _room() resolves to this room folder
    syn_sid = session_id if (session_id and getattr(mac, "_SESSION_RE").match(session_id)) else f"daou_{room}_quick1"
    brief_body = _blocks_to_quick_brief(title, blocks)
    # NO field drops — always wire theme/style/pages/layout/canvas (defaults match Mac skill).
    theme_v = (str(theme).strip() if theme else "") or "ubion"
    style_v = (str(style).strip() if style else "") or "workshop-teaching"
    canvas_v = (str(canvas).strip() if canvas else "") or "ppt169"
    layout_v = (str(layout).strip() if layout else "")  # empty → fill from style recipe
    pages_v = pages if pages is not None else 6
    try:
        pages_v = int(pages_v)
    except (TypeError, ValueError):
        pages_v = 6
    deck_v = (str(deck).strip() if deck else "")
    # SESSION_REV=2026-10-26-ab-parity-fixes: style → distinct SVG layout recipe
    recipe = _quick_style_recipe(style_v)
    recipe_layout = (recipe or {}).get("layout") or ""
    if not layout_v and recipe_layout:
        layout_v = recipe_layout
    struct = (recipe or {}).get("structure_3") or []
    protos = (recipe or {}).get("prototypes_3") or []
    recipe_brief = (recipe or {}).get("brief") or (
        f"STYLE RECIPE {style_v}: install style root; pick a DISTINCT layout roster vs other styles."
    )
    # Huashu editorial → SM dark editorial_bleed remap (overrides style recipe layout/structure)
    _sm = _huashu_sm_quick_remap(theme_v)
    if _sm:
        style_v = _sm.get("style") or style_v
        layout_v = _sm.get("layout") or layout_v
        struct = _sm.get("structure_3") or struct
        protos = []
        recipe_brief = _sm["brief"]
        logger.info("daou-workspace: quick sidecar SM-remap huashu theme=%s layout=%s", theme_v, layout_v)
    req_lines = [
        "[ubion-presentation / ppt-master Quick]",
        f"theme={theme_v}",
        f"style={style_v}",
        f"pages={pages_v}",
        f"canvas={canvas_v}",
        f"layout={layout_v or '(none)'}",
        "engine=quick",
        f"SESSION_REV={SESSION_REV}",
    ]
    if deck_v:
        req_lines.append(f"deck={deck_v}")
    req_lines.extend([
        f"주제/원고: {title or 'slides'}",
        "DNUE 금지. skill_view ubion-content/ubion-presentation 후 workflows/profiles/quick-generate.md.",
        "Brand+Style(+Layout) workspace 설치 → SVG → svg_to_pptx --quick-generate.",
        "Do NOT use Huashu/_PPT_STYLE_FAMILY for brand_id themes. Apply STYLE RECIPE below.",
        recipe_brief,
    ])
    if struct:
        req_lines.append("PAGE STRUCTURE (target SM parity): " + " → ".join(struct))
    if protos:
        req_lines.append("PROTOTYPES: " + ", ".join(protos) + " (trim layout design_spec page_count/roster to pages).")
    req_lines.extend([
        # Shape budget — SM baseline 5/10/10
        "SHAPE BUDGET (workshop-teaching pages=3): target shape_counts 5/10/10; clone reference-svg/workshop-teaching/*.svg then rewrite text only; see STYLE_PAGE_RECIPES.md.",
        # Cover logo / wordmark — SM baseline uses cover-logo + images/logo.png
        "COVER LOGO (required when brand has images/logo.png): on cover/title_slide put "
        '<image id="cover-logo" href="../images/logo.png" … data-pptx-role="decoration"/> '
        "like SM (中国电信 lockup ~236×65 top-right). Do NOT ship palette-only branding with zero media.",
        "Apply brand design_spec colors (中国电信 primary #004EA2). out/에 pptx만. "
        "답에 절대경로+theme/style/layout/canvas/장수+logo=Y/N.",
        "",
        "--- brief ---",
        brief_body or "(no blocks; invent a short Walmart-chapter-class corporate deck from the title)",
    ])
    request = "\n".join(req_lines) + "\n"
    t0 = _time.time()
    try:
        raw = mac.mac_content(
            {"request": request, "wait_seconds": wait, "inputs": []},
            session_id=syn_sid,
        )
        res = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.warning("daou-workspace: quick sidecar mac_content failed: %s", e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        logger.info("daou-workspace: quick sidecar error/fallback: %s", (res or {})[:200] if isinstance(res, str) else res)
        err = (res or {}).get("error") if isinstance(res, dict) else res
        return {
            "engine": "legacy",
            "quick_fallback": True,
            "quick_fallback_reason": f"mac_error:{str(err)[:300]}",
            "hop": (res or {}).get("hop") if isinstance(res, dict) else None,
            "quick_job": (res or {}).get("job_id") if isinstance(res, dict) else None,
            "rev": "c1-quick-wait-scp",
        }
    status = res.get("status")
    hop = res.get("hop")
    job_id = res.get("job_id")
    if status != "completed":
        # running/failed/lost → fail-closed to legacy for C1 (adapter pptx-follow not wired yet)
        reason = f"status={status} hop={hop} job={job_id} wait_s={wait} elapsed_s={_time.time() - t0:.1f}"
        logger.info(
            "daou-workspace: quick sidecar %s → legacy fallback",
            reason,
        )
        return {
            "engine": "legacy",
            "quick_fallback": True,
            "quick_fallback_reason": reason,
            "quick_job": job_id,
            "hop": hop,
            "elapsed_s": round(_time.time() - t0, 1),
            "rev": "c1-quick-wait-scp",
        }
    files = res.get("files") or []
    pptx_src = None
    for f in files:
        p = f.get("path") if isinstance(f, dict) else f
        if isinstance(p, str) and p.lower().endswith(".pptx") and os.path.isfile(p):
            pptx_src = p
            break
    if not pptx_src:
        logger.info("daou-workspace: quick sidecar completed but no .pptx in files → legacy")
        return {
            "engine": "legacy",
            "quick_fallback": True,
            "quick_fallback_reason": f"completed_no_pptx hop={hop} job={job_id}",
            "quick_job": job_id,
            "hop": hop,
            "rev": "c1-quick-wait-scp",
        }
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        shutil.copy2(pptx_src, path)
        os.chmod(path, 0o644)
    except OSError as e:
        logger.warning("daou-workspace: quick sidecar copy failed: %s", e)
        return None
    if not os.path.isfile(path) or os.path.getsize(path) < 64:
        return None
    return {
        "theme_wired": theme_v,
        "style_wired": style_v,
        "layout_wired": layout_v or None,
        "style_recipe": (recipe or {}).get("structure_3"),
        "engine": "quick",
        "quick_job": job_id,
        "hop": hop,
        "source": pptx_src,
        "fixed": 0,
        "truncated": 0,
        "still_overflow": 0,
        "grown": 0,
        "checked": 0,
        "rev": SESSION_REV,
        "elapsed_s": round(_time.time() - t0, 1),
        "mac_summary": (res.get("mac_summary") or "")[:500],
    }


def _pending_theme_path(session_or_chat_id: str) -> str | None:
    room = room_dir(session_or_chat_id or "")
    if not room:
        return None
    return os.path.join(room, _PENDING_THEME_NAME)


def _stash_pending_theme(session_or_chat_id: str, slug: str) -> None:
    path = _pending_theme_path(session_or_chat_id)
    if not path or not slug:
        return
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(slug + "\n")
        os.chmod(path, 0o644)
        logger.info("daou-workspace: stashed pending huashu theme %s -> %s", slug, path)
    except OSError as e:
        logger.warning("daou-workspace: stash theme failed: %s", e)


def _read_pending_theme(session_or_chat_id: str, *, consume: bool = False) -> str | None:
    path = _pending_theme_path(session_or_chat_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except OSError:
        return None
    slug = _normalize_theme_slug(raw)
    if consume and slug:
        try:
            os.unlink(path)
        except OSError:
            pass
    return slug


def _pending_style_path(session_or_chat_id: str) -> str | None:
    room = room_dir(session_or_chat_id or "")
    if not room:
        return None
    return os.path.join(room, _PENDING_STYLE_NAME)


def _stash_pending_style(session_or_chat_id: str, slug: str) -> None:
    path = _pending_style_path(session_or_chat_id)
    if not path or not slug:
        return
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(slug + "\n")
        os.chmod(path, 0o644)
        logger.info("daou-workspace: stashed pending style %s -> %s", slug, path)
    except OSError as e:
        logger.warning("daou-workspace: stash style failed: %s", e)


def _read_pending_style(session_or_chat_id: str, *, consume: bool = False) -> str | None:
    path = _pending_style_path(session_or_chat_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except OSError:
        return None
    slug = _normalize_style_slug(raw)
    if consume and slug:
        try:
            os.unlink(path)
        except OSError:
            pass
    return slug


def _theme_from_session_history(session_id: str) -> str | None:
    """Latest user message in state.db that carries /슬라이드 theme= (best-effort)."""
    if not session_id:
        return None
    db_path = str(PROFILE_HOME / "state.db")
    if not os.path.isfile(db_path):
        return None
    try:
        import sqlite3
        con = sqlite3.connect(db_path, timeout=1.0)
        try:
            row = con.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content LIKE '%/슬라이드%theme=%' ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        logger.debug("daou-workspace: theme history lookup failed: %s", e)
        return None
    return _extract_slide_theme(row[0]) if row else None


def _style_from_session_history(session_id: str) -> str | None:
    """Latest user message carrying style= (best-effort)."""
    if not session_id:
        return None
    db_path = str(PROFILE_HOME / "state.db")
    if not os.path.isfile(db_path):
        return None
    try:
        import sqlite3
        con = sqlite3.connect(db_path, timeout=1.0)
        try:
            row = con.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content LIKE '%style=%' ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        logger.debug("daou-workspace: style history lookup failed: %s", e)
        return None
    return _extract_slide_style(row[0]) if row else None


def _normalize_daou_session_id(session_id: str) -> str:
    """Map gateway session_key → daou_<room>_<ts> when embedded."""
    sid = session_id or ""
    if _SESSION_RE.match(sid):
        return sid
    m = re.search(r"(daou_[A-Za-z0-9_-]+_[a-z0-9]+)", sid)
    return m.group(1) if m else sid


def _resolve_forced_theme(args: dict, session_id: str = "") -> str | None:
    """Prefer explicit tool arg, then pending stash, then recent user message."""
    session_id = _normalize_daou_session_id(session_id)
    explicit = _normalize_theme_slug(args.get("theme")) if isinstance(args, dict) else None
    if explicit:
        return explicit
    pending = _read_pending_theme(session_id, consume=False)
    if pending:
        return pending
    # Path often encodes the room (…/buddy_312/foo.pptx) — use its parent as room key.
    if isinstance(args, dict):
        path = _resolve(args.get("path"))
        if path and _under(path, WORK_ROOT):
            room = os.path.basename(os.path.dirname(path))
            if room:
                fake = f"daou_{room}_x"
                pending = _read_pending_theme(fake, consume=False)
                if pending:
                    return pending
    return _theme_from_session_history(session_id)


def _inject_theme_into_args(args: dict, session_id: str = "") -> str | None:
    """Mutate args['theme'] when pptx and a forced slug is known. Returns slug or None."""
    if not isinstance(args, dict):
        return None
    if str(args.get("format") or "").lower() != "pptx":
        return None
    slug = _resolve_forced_theme(args, session_id)
    if not slug:
        return None
    prev = args.get("theme")
    if prev != slug:
        args["theme"] = slug
        logger.info(
            "daou-workspace: force-inject theme=%s (was %r) session=%s",
            slug, prev, session_id or "?",
        )
    return slug


def _resolve_forced_style(args: dict, session_id: str = "") -> str | None:
    """Prefer explicit tool arg, then pending stash, then recent user message."""
    session_id = _normalize_daou_session_id(session_id)
    explicit = _normalize_style_slug(args.get("style")) if isinstance(args, dict) else None
    if explicit:
        return explicit
    pending = _read_pending_style(session_id, consume=False)
    if pending:
        return pending
    if isinstance(args, dict):
        path = _resolve(args.get("path"))
        if path and _under(path, WORK_ROOT):
            room = os.path.basename(os.path.dirname(path))
            if room:
                fake = f"daou_{room}_x"
                pending = _read_pending_style(fake, consume=False)
                if pending:
                    return pending
    return _style_from_session_history(session_id)


def _inject_style_into_args(args: dict, session_id: str = "") -> str | None:
    """Mutate args['style'] when pptx and a forced style_id is known."""
    if not isinstance(args, dict):
        return None
    if str(args.get("format") or "").lower() != "pptx":
        return None
    slug = _resolve_forced_style(args, session_id)
    if not slug:
        return None
    prev = args.get("style")
    if prev != slug:
        args["style"] = slug
        logger.info(
            "daou-workspace: force-inject style=%s (was %r) session=%s",
            slug, prev, session_id or "?",
        )
    return slug


def pre_gateway_dispatch(event=None, **_kw):
    """Stash /슬라이드 key=value (theme/style/pages/layout/canvas/engine) + HARD note."""
    try:
        text = getattr(event, "text", None) if event is not None else None
        if not isinstance(text, str):
            return None
        kv = _parse_slide_kv(text)
        slug = _extract_slide_theme(text)
        style_slug = _extract_slide_style(text)
        if not slug and not style_slug and not kv:
            return None
        source = getattr(event, "source", None)
        chat_id = str(getattr(source, "chat_id", "") or "") if source is not None else ""
        if chat_id:
            if slug:
                _stash_pending_theme(chat_id, slug)
            if style_slug:
                _stash_pending_style(chat_id, style_slug)
            if kv:
                _stash_pending_slide_kv(chat_id, kv)
        if _HARD_THEME_TAG in text or _HARD_STYLE_TAG in text:
            return None
        parts = []
        if slug:
            parts.append(f"theme={slug}")
        if style_slug:
            parts.append(f"style={style_slug}")
        for k in ("pages", "layout", "canvas", "engine"):
            if kv.get(k):
                parts.append(f"{k}={kv[k]}")
        arglist = ", ".join(["format=pptx"] + parts)
        note = (
            f"\n\n{_HARD_THEME_TAG} {' '.join(parts)} — MUST call daou_make_document("
            f"{arglist}) and regenerate a NEW pptx now. "
            "Do NOT reuse or point at an existing pptx. "
            "engine=quick (default) uses Mac Quick Generate sidecar; engine=legacy = LMS python-pptx. "
            "Quick path: pass theme/style/layout/canvas through (no _PPT_STYLE_FAMILY)."
        )
        return {"action": "rewrite", "text": text.rstrip() + note}
    except Exception as e:
        logger.warning("daou-workspace: pre_gateway_dispatch theme hook failed: %s", e)
        return None


def pre_llm_call(user_message="", session_id="", **_kw):
    """Inject HARD context when this turn requests /슬라이드 theme=.

    Daou messenger uses api_server ``/api/sessions/.../chat/stream`` → ``_run_agent``,
    which never runs ``pre_gateway_dispatch``. Stash pending here so ``pre_tool_call`` /
    ``make_document`` can still force-inject when the LLM omits ``theme=``.
    """
    try:
        msg = user_message if isinstance(user_message, str) else ""
        # Multimodal: content may be a list of parts
        if not msg and isinstance(user_message, list):
            parts = []
            for p in user_message:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            msg = "\n".join(parts)
        slug = _extract_slide_theme(msg)
        style_slug = _extract_slide_style(msg)
        if not slug or not style_slug:
            # Fallback: scan conversation_history user turns (same turn may already be appended)
            hist = _kw.get("conversation_history") or []
            if isinstance(hist, list):
                for turn in reversed(hist):
                    if not isinstance(turn, dict):
                        continue
                    if turn.get("role") != "user":
                        continue
                    content = turn.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            x if isinstance(x, str) else (x.get("text") or "")
                            for x in content if isinstance(x, (str, dict))
                        )
                    if isinstance(content, str):
                        if not slug:
                            slug = _extract_slide_theme(content)
                        if not style_slug:
                            style_slug = _extract_slide_style(content)
                    if slug and style_slug:
                        break
        sid = session_id or ""
        # session_key form agent:main:api_server:dm:daou_buddy_312_xxx → daou_buddy_312_xxx
        if sid and not _SESSION_RE.match(sid):
            m = re.search(r"(daou_[A-Za-z0-9_-]+_[a-z0-9]+)", sid)
            if m:
                sid = m.group(1)
        kv = _parse_slide_kv(msg)
        if sid:
            if slug:
                _stash_pending_theme(sid, slug)
            if style_slug:
                _stash_pending_style(sid, style_slug)
            if kv:
                _stash_pending_slide_kv(sid, kv)
        if not slug:
            slug = _read_pending_theme(sid or session_id or "", consume=False)
        if not style_slug:
            style_slug = _read_pending_style(sid or session_id or "", consume=False)
        if not kv:
            kv = _read_pending_slide_kv(sid or session_id or "", consume=False)
        if not slug and not style_slug and not kv:
            return None
        parts = []
        call_bits = ["format=pptx"]
        if slug:
            parts.append(f"theme={slug}")
            call_bits.append(f"theme={slug}")
        if style_slug:
            parts.append(f"style={style_slug}")
            call_bits.append(f"style={style_slug}")
        for k in ("pages", "layout", "canvas", "engine"):
            if kv.get(k):
                parts.append(f"{k}={kv[k]}")
                call_bits.append(f"{k}={kv[k]}")
        eng = (kv.get("engine") or os.environ.get("PPT_ENGINE") or "quick").strip().lower()
        ctx = (
            f"{_HARD_THEME_TAG} Inbound requested ppt {' '.join(parts)}. "
            f"Call daou_make_document with {', '.join(call_bits)} "
            "(required — never omit theme=/style=/engine= from the wire). Regenerate; do not reuse an old file. "
            f"Default engine={eng if eng in ('quick','legacy') else 'quick'} "
            "(quick=Mac Quick Generate sidecar; legacy=LMS python-pptx). "
            "Messenger: `/슬라이드 theme=<brand_id> style=<style_id> pages=N [layout=] [canvas=] [engine=quick|legacy]`."
        )
        return {"context": ctx}
    except Exception as e:
        logger.warning("daou-workspace: pre_llm_call theme hook failed: %s", e)
        return None




# --- daou_make_document ---------------------------------------------------------------------------

_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["heading", "paragraph", "bullets", "table"]},
        "text": {"type": "string", "description": "heading / paragraph text"},
        "level": {"type": "integer", "description": "heading level 1-3"},
        "items": {"type": "array", "items": {"type": "string"}, "description": "bullets"},
        "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                 "description": "table rows (first row = header); empty strings for blank cells"},
        "layout": {
            "type": "string",
            "enum": ["auto", "title", "section", "content", "two_column", "cards", "kpi", "tiles"],
            "description": (
                "pptx only (on level-1 heading): title/section/content/two_column/cards/kpi/tiles. "
                "auto (default) infers: title (first/short), section (empty), "
                "kpi (2-4 metric-like items), cards (2-6 short items), "
                "two_column (6+ bullets), else content."
            ),
        },
        "columns": {
            "type": "integer",
            "description": "pptx bullets only: 2 = split items into two columns on a content slide",
        },
    },
    "required": ["type"],
}

SCHEMA = {
    "name": "daou_make_document",
    "description": (
        "[SESSION_REV=2026-10-24-doc-summary-v2] Create a Word (.docx), 한글 (.hwpx), PDF or PowerPoint (.pptx) document from structured blocks and save "
        "it in the room's work folder. [SESSION_REV=2026-10-26-ab-parity-fixes-l3] pptx default engine=quick PPT_QUICK_WAIT=420 (Mac Quick Generate via daou_mac_content hop; fail→legacy _write_pptx). engine=legacy forces LMS python-pptx. Messenger `/슬라이드 theme= style= pages= [layout=] [canvas=] [engine=quick|legacy]`. Quick path: style→SVG layout recipe + cover-logo when brand has images/logo.png; Huashu 01–20 specimen theme forces engine=legacy (SESSION_REV=2026-10-26-ab-parity-fixes). brand_id (中国电信/ubion/…) stays Quick. Word←claude-office; Hangul←hwpx-skill; PPT Quick←Mac/185, legacy←huashu. For pptx: each level-1 heading starts a slide; optional heading.layout = title|section|content|two_column|cards|kpi|tiles (auto if omitted); bullets.columns=2 for two-column lists; tables get styled headers. Use this for "
        "양식/문서/보고서/회의록/발표자료 requests — there is no shell or code execution. `path` must be an absolute path inside the work folder given in the system prompt, "
        "with the extension matching `format`. Always include tool name 'daou_make_document' in the call. 「한 장」/1페이지/one page/한장 → pages=1 (필수; compact layout + content budget). If blocks are huge (many slides/tables), split into multiple files. Reply with the saved path so the file can be delivered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": sorted(DOC_FORMATS)},
            "path": {"type": "string", "description": "Absolute output path, e.g. <work folder>/회의록.hwpx"},
            "title": {"type": "string", "description": "Document title (first heading)"},
            "blocks": {"type": "array", "items": _BLOCK_SCHEMA},
            "pages": {
                "type": "integer",
                "description": (
                    "Max page budget. When user asks 한 장 / 1페이지 / one page / 한장, MUST pass pages=1. "
                    "Enforces compact layout + hard content budget for docx: "
                    "title + 1-2 line intro + ONE compact table + <=3 bullets (or tiny source line). "
                    "Do NOT dump 4-5 full sections. LibreOffice page QC retries with tighter trim."
                ),
            },
            "compact": {
                "type": "boolean",
                "description": "Force one-page compact styling (smaller title, no body eyebrow, tight spacing). Implied by pages=1.",
            },
            "theme": {
                "type": "string",
                "description": (
                    "PPTX only. theme=<ppt-master brand_id> from brands_index "
                    "(ubion, mckinsey, aws, 中国电信, …; default/fallback ubion). "
                    "Palette/identity only — does not change layout family. "
                    "Advanced: huashu specimen slug (01-…~20-…). "
                    "Aliases: ubion-corporate|corporate|navy → ubion. "
                    "Pair with style= for layout (orthogonal)."
                ),
            },
            "style": {
                "type": "string",
                "description": (
                    "PPTX only. style=<ppt-master style_id>. "
                    "engine=legacy: maps to layout family via _PPT_STYLE_FAMILY. "
                    "engine=quick (default): pass-through to Quick brief (no family mapping). "
                    "Picker ids: workshop-teaching, mbb-consulting, solution-proposal, "
                    "academic-research, operating-review, narrative-keynote. "
                    "Orthogonal to theme= (palette)."
                ),
            },
            "layout": {
                "type": "string",
                "description": "PPTX Quick path: layout template id (ppt-master layout root). Ignored by legacy except heading.layout.",
            },
            "canvas": {
                "type": "string",
                "description": "PPTX Quick path: canvas/format token (e.g. 16x9, 16:9, 4x3). Legacy always 16:9.",
            },
            "engine": {
                "type": "string",
                "enum": ["quick", "legacy"],
                "description": (
                    "PPTX only. quick (default, env PPT_ENGINE): Mac/185 Quick Generate sidecar via "
                    "daou_mac_content hop; on failure/timeout falls back to legacy. "
                    "legacy: LMS _write_pptx (python-pptx + Huashu family)."
                ),
            },
        },
        "required": ["format", "path", "blocks"],
    },
}


def _cells(rows: list) -> list[list[str]]:
    out = [[str(c) if c is not None else "" for c in r] for r in rows if isinstance(r, list)]
    width = max((len(r) for r in out), default=0)
    return [r + [""] * (width - len(r)) for r in out if width]


def _bullets(b: dict) -> list[str]:
    return [str(i) for i in b.get("items") or [] if i is not None]



def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on", "compact"}


def _want_one_page(params: dict) -> tuple[bool, int | None]:
    """Return (compact, pages). pages=1 or compact=true → one-pager."""
    pages_raw = params.get("pages")
    pages = None
    if pages_raw is not None and str(pages_raw).strip() != "":
        try:
            pages = int(pages_raw)
        except (TypeError, ValueError):
            pages = None
    compact = _truthy(params.get("compact")) or (pages == 1)
    return compact, pages


_ONE_PAGE_MAX_HEADINGS = 1
_ONE_PAGE_MAX_PARA_CHARS = 120
_ONE_PAGE_MAX_BULLETS_PER = 3
_ONE_PAGE_MAX_BULLET_CHARS = 64
_ONE_PAGE_MAX_TABLE_ROWS = 5  # incl. header
_ONE_PAGE_MAX_CELL_CHARS = 32
_ONE_PAGE_MAX_SOURCE_CHARS = 90
_ONE_PAGE_MAX_PARAS = 1
_ONE_PAGE_MAX_BULLET_BLOCKS = 1


def _trim_text(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def _budget_blocks_one_page(blocks: list, *, aggressive: bool = False) -> list:
    """Cap body volume so a compact A4 docx stays on one page with margin.

    Preferred shape: short intro (<=1-2 lines) + ONE compact table + <=3 bullets
    OR a tiny source line — not 4-5 full sections. Always at most one table.
    aggressive=True: used on regenerate-after-page-count-fail (even tighter).
    """
    max_h = 0 if aggressive else _ONE_PAGE_MAX_HEADINGS
    max_para = 90 if aggressive else _ONE_PAGE_MAX_PARA_CHARS
    max_b = 2 if aggressive else _ONE_PAGE_MAX_BULLETS_PER
    max_bc = 52 if aggressive else _ONE_PAGE_MAX_BULLET_CHARS
    max_tr = 4 if aggressive else _ONE_PAGE_MAX_TABLE_ROWS
    max_cc = 26 if aggressive else _ONE_PAGE_MAX_CELL_CHARS
    max_src = 70 if aggressive else _ONE_PAGE_MAX_SOURCE_CHARS
    max_paras = 1 if aggressive else _ONE_PAGE_MAX_PARAS
    max_bb = 1  # always one bullet list on a one-pager

    out: list[dict] = []
    headings = 0
    paras = 0
    bullet_blocks = 0
    saw_table = False
    skip_body = False  # drop orphans under headings that exceeded the cap
    source_bits: list[str] = []

    def _is_source_text(s: str) -> bool:
        return any(k in s for k in ("http://", "https://", "출처", "근거:", "[공지]", "[규정]"))

    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "heading":
            htext = str(b.get("text") or "")
            # Source-only heading → fold into trailing one-liner, do not consume a section slot
            if any(k in htext for k in ("근거", "출처", "참고", "Source", "References")):
                skip_body = True  # following source bullets collected below
                continue
            if headings >= max_h:
                skip_body = True
                continue
            skip_body = False
            headings += 1
            nb = dict(b)
            nb["text"] = _trim_text(htext, 28)
            nb["level"] = min(max(int(b.get("level") or 1), 1), 2)
            out.append(nb)
        elif t == "paragraph":
            raw = str(b.get("text") or "")
            if _is_source_text(raw):
                source_bits.append(_trim_text(raw, 55))
                continue
            if skip_body:
                continue
            if paras >= max_paras:
                continue
            paras += 1
            out.append({"type": "paragraph", "text": _trim_text(raw, max_para)})
        elif t == "bullets":
            items = _bullets(b)
            src_items = [it for it in items if _is_source_text(it)]
            body_items = [it for it in items if not _is_source_text(it)]
            if src_items:
                source_bits.extend(_trim_text(it, 55) for it in src_items[:2])
            if skip_body or not body_items:
                continue
            if bullet_blocks >= max_bb:
                continue
            bullet_blocks += 1
            out.append({"type": "bullets", "items": [_trim_text(it, max_bc) for it in body_items[:max_b]]})
        elif t == "table":
            if saw_table:
                continue  # always one table on pages=1
            # Keep the first table even if section/heading cap tripped (preferred one-pager shape).
            rows = _cells(b.get("rows") or [])[:max_tr]
            rows = [[_trim_text(c, max_cc) for c in r] for r in rows]
            if rows:
                saw_table = True
                out.append({"type": "table", "rows": rows})
        else:
            if not skip_body:
                out.append(b)
    if source_bits:
        out.append({"type": "paragraph", "text": _trim_text(" · ".join(source_bits[:1]), max_src)})
    return out


def _soffice_available() -> str | None:
    for cand in ("soffice", "libreoffice"):
        from shutil import which
        p = which(cand)
        if p:
            return p
    return None


def _count_docx_pages(docx_path: str) -> int | None:
    """Optional post-render page count via LibreOffice → PDF → pdfinfo. None if unavailable."""
    import shutil
    import subprocess
    import tempfile

    soffice = _soffice_available()
    if not soffice or not shutil.which("pdfinfo"):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="daou-pageqc-") as td:
            r = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", td, docx_path],
                capture_output=True, text=True, timeout=90,
            )
            if r.returncode != 0:
                logger.info("daou-workspace: soffice convert failed: %s", (r.stderr or r.stdout or "")[:200])
                return None
            pdfs = list(Path(td).glob("*.pdf"))
            if not pdfs:
                return None
            info = subprocess.run(["pdfinfo", str(pdfs[0])], capture_output=True, text=True, timeout=30)
            m = re.search(r"^Pages:\s*(\d+)", info.stdout or "", re.M)
            return int(m.group(1)) if m else None
    except Exception as e:
        logger.info("daou-workspace: page count skipped: %s", e)
        return None


def _write_docx(path: str, title: str, blocks: list, *, compact: bool = False) -> None:
    """A4 corporate Word (claude-office patterns + Ubion): cover, callouts, tables, header/footer.

    compact=True (pages=1): skip body eyebrow, smaller title, tighter spacing/margins, denser body.
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor, Twips

    NAVY = RGBColor(0x04, 0x33, 0x7B)
    TEAL = RGBColor(0x0D, 0x87, 0x82)
    SLATE = RGBColor(0x40, 0x40, 0x40)
    MUTED = RGBColor(0x66, 0x66, 0x66)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    FONT = "Malgun Gothic"
    NAVY_HEX = "04337B"
    TEAL_HEX = "0D8782"
    ALT_HEX = "F0FAF9"
    CALLOUT_HEX = "F7F9FC"

    doc = Document()
    for section in doc.sections:
        if compact:
            section.top_margin = Cm(1.0)
            section.bottom_margin = Cm(1.0)
            section.left_margin = Cm(1.4)
            section.right_margin = Cm(1.4)
        else:
            section.top_margin = Cm(1.8)
            section.bottom_margin = Cm(1.8)
            section.left_margin = Cm(2.2)
            section.right_margin = Cm(2.2)
        # claude-office pattern: header/footer chrome + page numbers (Ubion navy/teal).
        # pages=1/compact: skip chrome — it eats vertical space and tip one-pagers over in Daou/Word.
        if compact:
            try:
                section.header.is_linked_to_previous = False
                section.footer.is_linked_to_previous = False
                if section.header.paragraphs:
                    section.header.paragraphs[0].text = ""
                if section.footer.paragraphs:
                    section.footer.paragraphs[0].text = ""
                section.header_distance = Cm(0.4)
                section.footer_distance = Cm(0.4)
            except Exception:
                pass
            continue
        try:
            header = section.header
            header.is_linked_to_previous = False
            hp = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
            hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for _r in list(hp.runs):
                _r._element.getparent().remove(_r._element)
            hr = hp.add_run("유비온  ·  사내 문서")
            hr.font.name = "Malgun Gothic"
            hr.font.size = Pt(9)
            hr.font.bold = True
            hr.font.color.rgb = RGBColor(0x0D, 0x87, 0x82)
            rPr = hr._element.get_or_add_rPr()
            rFonts = rPr.find(qn("w:rFonts"))
            if rFonts is None:
                rFonts = OxmlElement("w:rFonts")
                rPr.insert(0, rFonts)
            for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
                rFonts.set(qn(f"w:{attr}"), "Malgun Gothic")
            footer = section.footer
            footer.is_linked_to_previous = False
            fp = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
            fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for _r in list(fp.runs):
                _r._element.getparent().remove(_r._element)
            fr = fp.add_run("유비온")
            fr.font.name = "Malgun Gothic"
            fr.font.size = Pt(9)
            fr.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            rPr2 = fr._element.get_or_add_rPr()
            rFonts2 = rPr2.find(qn("w:rFonts"))
            if rFonts2 is None:
                rFonts2 = OxmlElement("w:rFonts")
                rPr2.insert(0, rFonts2)
            for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
                rFonts2.set(qn(f"w:{attr}"), "Malgun Gothic")
            sep = fp.add_run("  ·  ")
            sep.font.size = Pt(9)
            sep.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            run = fp.add_run()
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            r = run._r
            fldChar1 = OxmlElement("w:fldChar")
            fldChar1.set(qn("w:fldCharType"), "begin")
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = " PAGE "
            fldChar2 = OxmlElement("w:fldChar")
            fldChar2.set(qn("w:fldCharType"), "end")
            r.append(fldChar1)
            r.append(instr)
            r.append(fldChar2)
        except Exception:
            pass

    def _set_run_font(run, *, size=11, bold=False, color=SLATE, font=FONT):
        run.font.name = font
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            rFonts.set(qn(f"w:{attr}"), font)

    def _shade_cell(cell, hex_color: str):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:fill"), hex_color)
        shd.set(qn("w:val"), "clear")
        tcPr.append(shd)

    def _set_cell_text(cell, text, *, bold=False, color=SLATE, fill=None, size=10.5, pad=None):
        cell.text = ""
        p0 = cell.paragraphs[0]
        run = p0.add_run(text)
        _set_run_font(run, size=size, bold=bold, color=color)
        p0.paragraph_format.space_before = Pt(0)
        p0.paragraph_format.space_after = Pt(0)
        p0.paragraph_format.line_spacing = 1.0
        if fill:
            _shade_cell(cell, fill)
        if pad is not None:
            try:
                tc = cell._tc
                tcPr = tc.get_or_add_tcPr()
                tcMar = OxmlElement("w:tcMar")
                for edge, twips in (("top", pad), ("bottom", pad), ("left", pad), ("right", pad)):
                    el = OxmlElement(f"w:{edge}")
                    el.set(qn("w:w"), str(twips))
                    el.set(qn("w:type"), "dxa")
                    tcMar.append(el)
                tcPr.append(tcMar)
            except Exception:
                pass

    def _p_border_bottom(p, color_hex=TEAL_HEX, sz="12"):
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), sz)
        bottom.set(qn("w:space"), "4")
        bottom.set(qn("w:color"), color_hex)
        pBdr.append(bottom)
        pPr.append(pBdr)

    def _left_bar_paragraph(text, *, fill=CALLOUT_HEX, bar=TEAL_HEX, bold=False, size=11):
        """Callout / pull-quote: shaded paragraph with left accent via table."""
        table = doc.add_table(rows=1, cols=1)
        table.autofit = True
        cell = table.cell(0, 0)
        _shade_cell(cell, fill)
        # left border accent
        tc = cell._tc
        tcPr = tc.get_or_add_tcPr()
        tcBorders = OxmlElement("w:tcBorders")
        for edge, color, width in (
            ("top", "FFFFFF", "0"),
            ("bottom", "FFFFFF", "0"),
            ("right", "FFFFFF", "0"),
            ("left", bar, "24"),
        ):
            el = OxmlElement(f"w:{edge}")
            el.set(qn("w:val"), "single" if width != "0" else "nil")
            el.set(qn("w:sz"), width if width != "0" else "0")
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), color)
            tcBorders.append(el)
        tcPr.append(tcBorders)
        cell.text = ""
        p = cell.paragraphs[0]
        run = p.add_run(text)
        _set_run_font(run, size=size, bold=bold, color=SLATE)
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(4)
        doc.add_paragraph()

    # Base Normal
    try:
        normal = doc.styles["Normal"]
        normal.font.name = FONT
        normal.font.size = Pt(11)
        normal.font.color.rgb = SLATE
        normal.paragraph_format.space_after = Pt(2 if compact else 6)
        normal.paragraph_format.line_spacing = 1.05 if compact else 1.3
        if compact:
            normal.font.size = Pt(9)
        rPr = normal.element.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            rFonts.set(qn(f"w:{attr}"), FONT)
    except KeyError:
        pass

    if title:
        # Cover / title block — compact: header already has eyebrow; skip body duplicate
        if not compact:
            eye = doc.add_paragraph()
            er = eye.add_run("유비온  ·  사내 문서")
            _set_run_font(er, size=10, bold=True, color=TEAL)
            eye.paragraph_format.space_after = Pt(2)

        p = doc.add_paragraph()
        run = p.add_run(str(title))
        _set_run_font(run, size=(13 if compact else 24), bold=True, color=NAVY)
        p.paragraph_format.space_after = Pt(1 if compact else 2)
        p.paragraph_format.space_before = Pt(0)
        _p_border_bottom(p, TEAL_HEX, "8" if compact else "18")
        if not compact:
            doc.add_paragraph()

    for b in blocks:
        t = b.get("type")
        if t == "heading":
            level = min(max(int(b.get("level") or 1), 1), 3)
            text_h = str(b.get("text", ""))
            # Callout-style H2 when text starts with ※ or note markers — keep as heading
            p = doc.add_heading(text_h, level=level)
            color = NAVY if level == 1 else (TEAL if level == 2 else SLATE)
            if compact:
                size = {1: 11, 2: 10, 3: 9.5}[level]
            else:
                size = {1: 16, 2: 13, 3: 11.5}[level]
            for run in p.runs:
                _set_run_font(run, size=size, bold=True, color=color)
            p.paragraph_format.space_before = Pt((4 if level == 1 else 3) if compact else (16 if level == 1 else 12))
            p.paragraph_format.space_after = Pt(1 if compact else 6)
            if level == 1 and not compact:
                _p_border_bottom(p, TEAL_HEX, "10")
        elif t == "paragraph":
            raw = str(b.get("text", ""))
            # pull-quote / callout when prefixed
            if raw.startswith(("※", "▸", "▶", "💡", "Note:", "참고:", "요약:")) and not compact:
                _left_bar_paragraph(raw, bold=False, size=11)
            else:
                p = doc.add_paragraph()
                run = p.add_run(raw)
                _set_run_font(run, size=(9 if compact else 11), color=SLATE)
                p.paragraph_format.space_after = Pt(2 if compact else 8)
                p.paragraph_format.line_spacing = 1.05 if compact else 1.3
        elif t == "bullets":
            for item in _bullets(b):
                p = doc.add_paragraph(style="List Bullet")
                bsize = 9 if compact else 11
                if p.runs:
                    p.runs[0].text = item
                    _set_run_font(p.runs[0], size=bsize, color=SLATE)
                else:
                    run = p.add_run(item)
                    _set_run_font(run, size=bsize, color=SLATE)
                p.paragraph_format.space_after = Pt(0 if compact else 3)
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.line_spacing = 1.05 if compact else 1.15
        elif t == "table":
            rows = _cells(b.get("rows") or [])
            if rows:
                table = doc.add_table(rows=len(rows), cols=len(rows[0]))
                table.style = "Table Grid"
                try:
                    # Prefer fixed layout + equal column widths (docx-js dual-width habit)
                    tbl = table._tbl
                    tblPr = tbl.tblPr if tbl.tblPr is not None else OxmlElement("w:tblPr")
                    if tbl.tblPr is None:
                        tbl.insert(0, tblPr)
                    layout = OxmlElement("w:tblLayout")
                    layout.set(qn("w:type"), "fixed")
                    tblPr.append(layout)
                    usable = int(Cm(18.0 if compact else 16.6).twips)
                    col_w = max(usable // len(rows[0]), 800)
                    for row in table.rows:
                        for cell in row.cells:
                            cell.width = Twips(col_w)
                except Exception:
                    pass
                for i, r in enumerate(rows):
                    for j, v in enumerate(r):
                        cell = table.cell(i, j)
                        csize = 8 if compact else 10.5
                        cpad = 28 if compact else None  # twips; ~0.5mm
                        if i == 0:
                            _set_cell_text(cell, v, bold=True, color=WHITE, fill=NAVY_HEX, size=csize, pad=cpad)
                        else:
                            _set_cell_text(
                                cell, v, color=SLATE,
                                fill=(ALT_HEX if i % 2 == 0 else "FFFFFF"),
                                size=csize, pad=cpad,
                            )
                if not compact:
                    doc.add_paragraph()
    doc.save(path)



def _write_hwpx(path: str, title: str, blocks: list) -> None:
    """A4 corporate 한글 (jkf87/hwpx-skill guidance + Ubion chrome): cover, callouts, tables."""
    from hwpx.document import HwpxDocument

    FONT = "맑은 고딕"
    NAVY = "#04337B"
    TEAL = "#0D8782"
    SLATE = "#404040"
    MUTED = "#666666"
    doc = HwpxDocument.new()
    try:
        doc.set_page_setup(
            paper_size="A4",
            margin_left_mm=22,
            margin_right_mm=22,
            margin_top_mm=18,
            margin_bottom_mm=18,
        )
    except Exception:
        pass

    styles = doc.styles
    eye = styles.ensure_run(bold=True, color=TEAL, font=FONT, size=10)
    h1 = styles.ensure_run(bold=True, color=NAVY, font=FONT, size=20)
    h2 = styles.ensure_run(bold=True, color=TEAL, font=FONT, size=14)
    h3 = styles.ensure_run(bold=True, color=SLATE, font=FONT, size=12)
    body = styles.ensure_run(color=SLATE, font=FONT, size=11)
    bullet = styles.ensure_run(color=SLATE, font=FONT, size=11)
    callout = styles.ensure_run(color=SLATE, font=FONT, size=11)
    th = styles.ensure_run(bold=True, color="#FFFFFF", font=FONT, size=10.5)
    td = styles.ensure_run(color=SLATE, font=FONT, size=10.5)
    rule = styles.ensure_run(color=TEAL, font=FONT, size=8)
    hdr_fill = styles.ensure_border_fill(
        border_color=NAVY, border_width="0.15 mm", fill_color=NAVY
    )
    alt_fill = styles.ensure_border_fill(
        border_color="#CBD5E1", border_width="0.12 mm", fill_color="#F0FAF9"
    )
    cell_fill = styles.ensure_border_fill(
        border_color="#CBD5E1", border_width="0.12 mm", fill_color="#FFFFFF"
    )
    callout_fill = styles.ensure_border_fill(
        border_color=TEAL, border_width="0.4 mm", fill_color="#F7F9FC"
    )
    head_levels = {1: h1, 2: h2, 3: h3}

    if title:
        doc.add_paragraph("유비온  ·  사내 문서", char_pr_id_ref=eye)
        doc.add_heading(str(title), level=1, char_pr_id_ref=h1)
        doc.add_paragraph("━━━━━━━━━━━━━━━━━━━━", char_pr_id_ref=rule)
        doc.add_paragraph("")

    for b in blocks:
        t = b.get("type")
        if t == "heading":
            level = min(max(int(b.get("level") or 1), 1), 3)
            hwpx_level = level + (1 if title else 0)
            hwpx_level = min(hwpx_level, 5)
            doc.add_heading(
                str(b.get("text", "")),
                level=hwpx_level,
                char_pr_id_ref=head_levels[level],
            )
            if level == 1:
                doc.add_paragraph("────────────────", char_pr_id_ref=rule)
        elif t == "paragraph":
            raw = str(b.get("text", ""))
            if raw.startswith(("※", "▸", "▶", "💡", "Note:", "참고:", "요약:")):
                # single-cell callout table
                try:
                    table = doc.add_table(1, 1, border_fill_id_ref=callout_fill)
                    cell = table.cell(0, 0)
                    cell.set_text("")
                    cell.add_paragraph(raw, char_pr_id_ref=callout)
                    doc.add_paragraph("")
                except Exception:
                    doc.add_paragraph(f"▸ {raw}", char_pr_id_ref=callout)
            else:
                doc.add_paragraph(raw, char_pr_id_ref=body)
        elif t == "bullets":
            for item in _bullets(b):
                doc.add_paragraph(f"• {item}", char_pr_id_ref=bullet)
        elif t == "table":
            rows = _cells(b.get("rows") or [])
            if rows:
                table = doc.add_table(len(rows), len(rows[0]), border_fill_id_ref=cell_fill)
                try:
                    table.equalize_column_widths()
                except Exception:
                    pass
                for i, r in enumerate(rows):
                    for j, v in enumerate(r):
                        cell = table.cell(i, j)
                        cell.set_text("")
                        if i == 0:
                            table.set_cell_border_fill(i, j, hdr_fill)
                            cell.add_paragraph(v, char_pr_id_ref=th)
                        else:
                            table.set_cell_border_fill(i, j, alt_fill if i % 2 == 0 else cell_fill)
                            cell.add_paragraph(v, char_pr_id_ref=td)
                doc.add_paragraph("")
    doc.save_to_path(path)



def _write_pdf(path: str, title: str, blocks: list) -> None:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import ListFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    ss = getSampleStyleSheet()
    body = ParagraphStyle("ko", parent=ss["BodyText"], fontName="HYSMyeongJo-Medium", fontSize=10.5, leading=16)
    heads = {lv: ParagraphStyle(f"h{lv}", parent=ss[f"Heading{lv}"], fontName="HYGothic-Medium") for lv in (1, 2, 3)}
    title_style = ParagraphStyle("t", parent=ss["Title"], fontName="HYGothic-Medium")
    story = [Paragraph(escape(title), title_style), Spacer(1, 8)] if title else []
    for b in blocks:
        t = b.get("type")
        if t == "heading":
            story.append(Paragraph(escape(str(b.get("text", ""))), heads[min(max(int(b.get("level") or 1), 1), 3)]))
        elif t == "paragraph":
            story.append(Paragraph(escape(str(b.get("text", ""))).replace("\n", "<br/>"), body))
        elif t == "bullets":
            story.append(ListFlowable([Paragraph(escape(i), body) for i in _bullets(b)], bulletType="bullet"))
        elif t == "table":
            rows = _cells(b.get("rows") or [])
            if rows:
                tbl = Table([[Paragraph(escape(v), body) for v in r] for r in rows], repeatRows=1)
                tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                         ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
                                         ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story.extend([tbl, Spacer(1, 8)])
    SimpleDocTemplate(path, pagesize=A4, title=title or "").build(story)


# --- pptx text-overflow QA (SESSION_REV=2026-10-25-huashu-overflow-qa) -----------------------
# Runs inside _write_pptx right before prs.save(): every text frame's wrapped height is estimated
# with a CJK-aware glyph-width heuristic (no renderer needed), and overflowing frames are fixed in
# order: word_wrap on → shrink runs (≤8 × 10%, role floors) → grow a plain textbox ≤15% when nothing
# sits below it → ellipsis-truncate trailing text. Fonts/geometry of frames that fit are untouched.
_EMU_PER_PT = 12700
_EMU_PER_IN = 914400
_OVF_LINE_H = 1.20          # single line height in em
_OVF_SHRINK = 0.90          # per-step font scale
_OVF_MAX_STEPS = 8
_OVF_FLOOR = {"title": 14.0, "body": 9.0, "footer": 7.0}
_OVF_FOOTER_TOP_IN = 6.9    # frames starting below this are page crumbs / footers
_OVF_BOTTOM_IN = 7.0        # grown frames never cross this line
_OVF_GROW_MAX = 0.15
_OVF_KEEP_CHARS = 20        # truncation always keeps this much of the first paragraph
_OVF_DEFAULT_PT = 18.0      # python-pptx / PowerPoint default run size
_OVF_DETAILS_MAX = 40


def _char_em(ch: str) -> float:
    o = ord(ch)
    if ch in " \t":
        return 0.35
    if o < 128:
        return 0.55
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return 1.0
    if 0x4E00 <= o <= 0x9FFF:
        return 1.0
    return 0.9


def _est_width_pt(text: str, font_pt: float) -> float:
    return sum(_char_em(c) for c in text) * font_pt


def _ovf_run_pt(run, para) -> float:
    if run.font.size is not None:
        return run.font.size.pt
    if para.font.size is not None:
        return para.font.size.pt
    return _OVF_DEFAULT_PT


def _ovf_run_spc_pt(run) -> float:
    """Letter spacing (a:rPr/@spc, 1/100 pt) added after every glyph."""
    try:
        rPr = run._r.rPr
        spc = rPr.get("spc") if rPr is not None else None
        return int(spc) / 100.0 if spc else 0.0
    except Exception:
        return 0.0


def _ovf_para_lines(para, avail_w: float, wrap: bool) -> tuple[int, float, float]:
    """(line count, widest line pt, max font pt) for one paragraph, greedy word wrap.

    Words are space-separated tokens; a token wider than the line breaks per glyph (that is
    how both PowerPoint and LibreOffice treat long Hangul eojeol / numbers in narrow boxes).
    """
    glyphs = []  # (char, width_pt); "\n" = hard break
    max_pt = 0.0
    for run in para.runs:
        size = _ovf_run_pt(run, para)
        spc = _ovf_run_spc_pt(run)
        max_pt = max(max_pt, size)
        for ch in run.text or "":
            if ch in "\n\v\r":
                glyphs.append(("\n", 0.0))
            else:
                glyphs.append((ch, _char_em(ch) * size + spc))
    if max_pt <= 0:
        max_pt = para.font.size.pt if para.font.size is not None else _OVF_DEFAULT_PT
    lines, cur, widest = 1, 0.0, 0.0
    word: list[float] = []

    def _flush_word():
        nonlocal lines, cur, widest
        if not word:
            return
        ww = sum(word)
        if wrap and cur > 0 and cur + ww > avail_w:
            widest = max(widest, cur)
            lines += 1
            cur = 0.0
        if wrap and ww > avail_w:
            for g in word:
                if cur > 0 and cur + g > avail_w:
                    widest = max(widest, cur)
                    lines += 1
                    cur = 0.0
                cur += g
        else:
            cur += ww
        word.clear()

    for ch, w in glyphs:
        if ch == "\n":
            _flush_word()
            widest = max(widest, cur)
            lines += 1
            cur = 0.0
        elif ch in " \t":
            _flush_word()
            cur += w  # trailing spaces hang past the margin, never force a wrap
        else:
            word.append(w)
    _flush_word()
    widest = max(widest, cur)
    return lines, widest, max_pt


def _ovf_measure(shape) -> dict | None:
    """Estimated need vs available box (pt) for a shape's text frame; None if nothing to check."""
    tf = shape.text_frame
    if shape.width is None or shape.height is None:
        return None
    paras = list(tf.paragraphs)
    text = "".join(p.text or "" for p in paras)
    if not text.strip():
        return None
    ml = tf.margin_left or 0
    mr = tf.margin_right or 0
    mt = tf.margin_top or 0
    mb = tf.margin_bottom or 0
    avail_w = max(1.0, (shape.width - ml - mr) / _EMU_PER_PT)
    avail_h = max(1.0, (shape.height - mt - mb) / _EMU_PER_PT)
    wrap = tf.word_wrap is not False  # None → bodyPr default (square) wraps
    need_h, widest, max_pt = 0.0, 0.0, 0.0
    last = len(paras) - 1
    for i, p in enumerate(paras):
        n, w, pt = _ovf_para_lines(p, avail_w, wrap)
        widest = max(widest, w)
        max_pt = max(max_pt, pt)
        ls = p.line_spacing
        if ls is None:
            line_h = pt * _OVF_LINE_H
        elif isinstance(ls, float):
            line_h = pt * _OVF_LINE_H * ls
        else:  # Length → exact line pitch
            line_h = ls / _EMU_PER_PT
        need_h += n * line_h
        if i > 0 and p.space_before is not None:
            need_h += p.space_before / _EMU_PER_PT
        if i < last and p.space_after is not None:
            need_h += p.space_after / _EMU_PER_PT
    return {
        "avail_w": avail_w, "avail_h": avail_h, "need_h": need_h, "widest": widest,
        "wrap": wrap, "max_pt": max_pt, "text": text,
    }


def _ovf_overflows(m: dict) -> bool:
    if m["need_h"] > m["avail_h"] * 1.03 + 1.0:
        return True
    return (not m["wrap"]) and m["widest"] > m["avail_w"] * 1.02


def _ovf_role(shape, max_pt: float) -> str:
    if shape.top is not None and shape.top >= _OVF_FOOTER_TOP_IN * _EMU_PER_IN and max_pt <= 12:
        return "footer"
    return "title" if max_pt >= 20 else "body"


def _ovf_grow_limit(slide_shapes, shape, slide_w: int, slide_h: int) -> int | None:
    """Max bottom (EMU) a plain top-anchored textbox may grow to without touching anything below
    it or leaving its card; None when growing is not safe."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.enum.text import MSO_ANCHOR

    if slide_shapes is None or shape.shape_type != MSO_SHAPE_TYPE.TEXT_BOX:
        return None
    if shape.rotation:
        return None
    if shape.text_frame.vertical_anchor not in (None, MSO_ANCHOR.TOP):
        return None
    s_l, s_t = shape.left, shape.top
    s_r, s_b = s_l + shape.width, s_t + shape.height
    limit = min(int(_OVF_BOTTOM_IN * _EMU_PER_IN), slide_h)
    for other in slide_shapes:
        if other.shape_id == shape.shape_id:
            continue
        if None in (other.left, other.top, other.width, other.height):
            continue
        o_l, o_t = other.left, other.top
        o_r, o_b = o_l + other.width, o_t + other.height
        if o_r <= s_l or o_l >= s_r or o_b <= s_t:
            continue  # beside or entirely above
        if other.width >= 0.9 * slide_w and other.height >= 0.9 * slide_h:
            continue  # full-slide background
        if o_l <= s_l and o_t <= s_t and o_r >= s_r and o_b >= s_b:
            limit = min(limit, o_b)  # card / panel holding the text: stay inside it
        elif o_t >= s_t:
            limit = min(limit, o_t)  # next element below
        else:
            limit = min(limit, s_b)  # partial overlap: don't touch geometry
    limit = min(limit, s_t + int(shape.height * (1 + _OVF_GROW_MAX)))
    return limit if limit > s_b else None


def _ovf_set_kept(runs, orig: list[str], keep: int) -> None:
    """Keep the first `keep` chars across runs and end with '…' (orig = untouched run texts)."""
    left = keep
    last_kept = None
    for r, t in zip(runs, orig):
        if left <= 0:
            r.text = ""
            continue
        r.text = t[:left]
        left -= len(r.text)
        if r.text:
            last_kept = r
    if last_kept is not None:
        last_kept.text = last_kept.text.rstrip(" ,.·:;—–-") + "…"


def _ovf_truncate(shape) -> bool:
    """Ellipsis-truncate trailing text until the frame fits. Returns True if it now fits."""
    tf = shape.text_frame
    for _ in range(16):
        m = _ovf_measure(shape)
        if m is None or not _ovf_overflows(m):
            return True
        paras = [p for p in tf.paragraphs if (p.text or "").strip()]
        if not paras:
            return True
        p = paras[-1]
        is_first = len(paras) == 1
        runs = list(p.runs)
        orig = [r.text or "" for r in runs]
        full = "".join(orig).rstrip("…")
        keep_min = min(len(full), _OVF_KEEP_CHARS) if is_first else 1

        def _fits(k):
            _ovf_set_kept(runs, orig, k)
            mm = _ovf_measure(shape)
            return mm is None or not _ovf_overflows(mm)

        if keep_min < len(full) and _fits(keep_min):
            lo, hi = keep_min, len(full) - 1  # lo always fits
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _fits(mid):
                    lo = mid
                else:
                    hi = mid - 1
            _ovf_set_kept(runs, orig, lo)
            return True
        if is_first:
            if keep_min < len(full):
                _ovf_set_kept(runs, orig, keep_min)
            else:
                for r, t in zip(runs, orig):
                    r.text = t
            return False
        # whole trailing paragraph goes; mark the elision on the one before it
        for r, t in zip(runs, orig):
            r.text = t
        p._p.getparent().remove(p._p)
        prev_runs = [r for r in paras[-2].runs if r.text]
        if prev_runs and not prev_runs[-1].text.endswith("…"):
            prev_runs[-1].text = prev_runs[-1].text.rstrip(" ,.·:;—–-") + "…"
    m = _ovf_measure(shape)
    return m is None or not _ovf_overflows(m)


def _ovf_iter_text_shapes(shapes, top_level=True):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _ovf_iter_text_shapes(shape.shapes, top_level=False)
        elif shape.has_text_frame:
            yield shape, top_level


def _pptx_fix_text_overflow(prs) -> dict:
    """Overflow QA pass over a built Presentation (in memory, before save). See block comment."""
    from pptx.util import Pt

    report = {"fixed": 0, "truncated": 0, "grown": 0, "still_overflow": 0, "checked": 0, "details": []}
    slide_w, slide_h = prs.slide_width, prs.slide_height
    for s_idx, slide in enumerate(prs.slides, start=1):
        for shape, top_level in list(_ovf_iter_text_shapes(slide.shapes)):
            m = _ovf_measure(shape)
            if m is None:
                continue
            report["checked"] += 1
            if not _ovf_overflows(m):
                continue
            tf = shape.text_frame
            before = {"need_h": round(m["need_h"], 1), "box_h": round(m["avail_h"], 1),
                      "font": round(m["max_pt"], 1)}
            fixes = []
            # a. wrap
            if tf.word_wrap is not True:
                tf.word_wrap = True
                fixes.append("wrap")
                m = _ovf_measure(shape)
            # b. shrink all runs, floored by role (never raises a run already under the floor)
            if m is not None and _ovf_overflows(m):
                role = _ovf_role(shape, m["max_pt"])
                runs = [(r, _ovf_run_pt(r, p)) for p in tf.paragraphs for r in p.runs]
                floors = [min(_OVF_FLOOR[role], pt) for _r, pt in runs]
                cur = [pt for _r, pt in runs]
                for _step in range(_OVF_MAX_STEPS):
                    nxt = [max(fl, round(c * _OVF_SHRINK * 2) / 2) for c, fl in zip(cur, floors)]
                    if nxt == cur:
                        break
                    cur = nxt
                    for (r, _pt), c in zip(runs, cur):
                        r.font.size = Pt(c)
                    if "shrink" not in fixes:
                        fixes.append("shrink")
                    m = _ovf_measure(shape)
                    if m is None or not _ovf_overflows(m):
                        break
            # d. grow a plain textbox into free space below (≤ +15%)
            if m is not None and _ovf_overflows(m) and top_level:
                limit = _ovf_grow_limit(slide.shapes, shape, slide_w, slide_h)
                if limit is not None:
                    tf_pad = (tf.margin_top or 0) + (tf.margin_bottom or 0)
                    want = shape.top + int(m["need_h"] * _EMU_PER_PT) + tf_pad
                    new_b = min(limit, want)
                    if new_b > shape.top + shape.height:
                        shape.height = new_b - shape.top
                        fixes.append("grow")
                        report["grown"] += 1
                        m = _ovf_measure(shape)
            # c. ellipsis-truncate trailing text
            if m is not None and _ovf_overflows(m):
                text_before = m["text"]
                _ovf_truncate(shape)
                m = _ovf_measure(shape)
                if m is not None and m["text"] != text_before:
                    fixes.append("truncate")
                    report["truncated"] += 1
            fits = m is None or not _ovf_overflows(m)
            report["fixed" if fits else "still_overflow"] += 1
            if len(report["details"]) < _OVF_DETAILS_MAX:
                after_pt = max((_ovf_run_pt(r, p) for p in tf.paragraphs for r in p.runs), default=0.0)
                report["details"].append({
                    "slide": s_idx,
                    "shape": shape.name,
                    "fix": fixes,
                    "font_before": before["font"],
                    "font_after": round(after_pt, 1),
                    "need_h": before["need_h"],
                    "box_h": before["box_h"],
                    "ok": fits,
                    "text": _trim_text("".join(p.text or "" for p in tf.paragraphs), 40),
                })
    return report


def _write_pptx(path: str, title: str, blocks: list, *, theme: str | None = None, style: str | None = None) -> dict:
    """16:9 deck. theme=<brand_id|huashu> = palette; style=<style_id> = layout family.

    Layouts: title/section/content/two_column/cards/kpi/tiles. Brand ids from
    brands_index use design_spec palette (identity). style= selects family via
    _PPT_STYLE_FAMILY (omit → corporate). Huashu numeric slugs keep specimen
    DNA unless style= overrides. Overflow QA runs for every path before save.
    """
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.xmlchemy import OxmlElement
    from pptx.util import Inches, Pt, Emu

    theme_slug, pal = _resolve_pptx_theme(theme)
    pal, style_id, style_family = _apply_style_to_palette(pal, style)

    def _rgb(h: str):
        h = h.strip().lstrip("#")
        return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    NAVY = _rgb(pal["primary"])
    TEAL = _rgb(pal["accent"])
    NAVY_DARK = _rgb(pal["dark_primary"])
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    SLATE = _rgb(pal["text"])
    MUTED = _rgb(pal["muted"])
    GREY_LIGHT = RGBColor(0xBF, 0xBF, 0xBF)
    ROW_ALT = _rgb(pal["surface"])
    CARD_BG = _rgb(pal["surface"])
    SLIDE_BG = _rgb(pal["bg"])
    CHROME = pal.get("chrome", "side_bar")
    MODE = pal.get("mode", "corporate")
    FONT = "Malgun Gothic"
    FOOTER_LABEL = str(pal.get("footer") or "유비온  ·  사내 안내")

    SW, SH = Inches(13.333), Inches(7.5)
    MARGIN_X = Inches(0.55)
    CONTENT_W = Inches(12.23)

    prs, blank, _ubion_seed_used = _pptx_open_presentation(pal)
    prs.slide_width, prs.slide_height = SW, SH

    def _set_run(run, text, size, bold=False, color=SLATE, font=FONT):
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = font
        try:
            rPr = run._r.get_or_add_rPr()
            ea_tag = "{http://schemas.openxmlformats.org/drawingml/2006/main}ea"
            for el in rPr.findall(ea_tag):
                rPr.remove(el)
            ea = OxmlElement("a:ea")
            ea.set("typeface", font)
            rPr.append(ea)
        except Exception:
            pass

    def _fill_solid(shape, color):
        shape.fill.solid()
        shape.fill.fore_color.rgb = color

    def _no_line(shape):
        shape.line.fill.background()

    def _add_rect(slide, left, top, width, height, color):
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
        _fill_solid(shape, color)
        _no_line(shape)
        return shape

    def _add_round(slide, left, top, width, height, color):
        shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
        _fill_solid(shape, color)
        _no_line(shape)
        try:
            shape.adjustments[0] = 0.08
        except Exception:
            pass
        return shape

    def _add_textbox(slide, left, top, width, height):
        box = slide.shapes.add_textbox(left, top, width, height)
        # overflow-qa: python-pptx textboxes default to wrap="none" → long runs spill sideways
        box.text_frame.word_wrap = True
        return box

    def _paint_bg(slide):
        if MODE != "corporate" or str(pal.get("bg", "FFFFFF")).upper() != "FFFFFF":
            _add_rect(slide, Inches(0), Inches(0), SW, SH, SLIDE_BG)

    def _chrome_content(slide):
        _paint_bg(slide)
        if CHROME == "side_bar":
            _add_rect(slide, Inches(0), Inches(0), Inches(0.08), SH, TEAL)
            _add_rect(slide, Inches(0), Inches(0), SW, Inches(0.045), NAVY)
        elif CHROME == "research":
            _add_rect(slide, Inches(0.55), Inches(0.42), Inches(12.23), Inches(0.03), TEAL)
        elif CHROME == "top_rule":
            _add_rect(slide, Inches(0), Inches(0), SW, Inches(0.06), NAVY)
        elif CHROME == "hairline":
            _add_rect(slide, Inches(0.55), Inches(0.35), Inches(12.23), Inches(0.015), TEAL)
        elif CHROME == "minimal":
            pass
        else:
            _add_rect(slide, Inches(0), Inches(0), SW, Inches(0.045), NAVY)

    def _footer(slide, page, total, dark=False):
        # left brand crumb + right page
        left = _add_textbox(slide, MARGIN_X, Inches(7.08), Inches(8.5), Inches(0.3))
        lr = left.text_frame.paragraphs[0].add_run()
        _set_run(lr, FOOTER_LABEL, 10, color=(GREY_LIGHT if dark else MUTED))
        box = _add_textbox(slide, Inches(10.2), Inches(7.08), Inches(2.6), Inches(0.3))
        p = box.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        run = p.add_run()
        _set_run(run, f"{page} / {total}", 10, color=(GREY_LIGHT if dark else MUTED))

    def _title_bar(slide, heading):
        box = _add_textbox(slide, MARGIN_X, Inches(0.28), CONTENT_W, Inches(0.95))
        tf = box.text_frame
        tf.word_wrap = True
        run = tf.paragraphs[0].add_run()
        # slightly larger title for denser bokri feel
        _set_run(run, heading, 30, bold=True, color=NAVY)
        _add_rect(slide, MARGIN_X, Inches(1.18), Inches(1.35), Inches(0.055), TEAL)

    def _split_kpi(item):
        for sep in ("|", "：", ":", "—", "–"):
            if sep in item:
                a, b = item.split(sep, 1)
                return a.strip(), b.strip()
        return item.strip(), ""

    def _split_card(item):
        for sep in (" — ", " – ", " - ", "—", "–", ":", "："):
            if sep in item:
                a, b = item.split(sep, 1)
                return a.strip(), b.strip()
        return item.strip(), ""

    def _body_texts(body):
        texts = []
        has_table = False
        for b in body:
            t = b.get("type")
            if t == "bullets":
                texts.extend(_bullets(b))
            elif t == "paragraph":
                texts.append(str(b.get("text") or ""))
            elif t == "heading":
                texts.append(str(b.get("text") or ""))
            elif t == "table":
                has_table = True
        texts = [t for t in texts if t and str(t).strip()]
        return texts, has_table

    def _infer_layout(layout_raw, is_first, body):
        if layout_raw and layout_raw != "auto":
            return layout_raw
        texts, has_table = _body_texts(body)
        if has_table:
            return "content"
        if not texts:
            return "title" if is_first else "section"
        if is_first and len(texts) <= 5 and all(len(t) <= 80 for t in texts):
            return "title"

        def _looks_kpi(item: str) -> bool:
            s = item.strip()
            if "|" in s:
                return True
            for sep in (":", "：", "—", "–"):
                if sep in s:
                    a, b = s.split(sep, 1)
                    b = b.strip()
                    if b and len(b) <= 28 and any(ch.isdigit() for ch in b):
                        return True
            if len(s) <= 40 and any(ch.isdigit() for ch in s):
                if any(tok in s for tok in ("%","원","일","명","건","배","억","만","점","위","학기")):
                    return True
            return False

        n = len(texts)
        if 2 <= n <= 4 and all(len(t) <= 52 for t in texts):
            kpi_hits = sum(1 for t in texts if _looks_kpi(t))
            if kpi_hits >= max(2, n - 1):
                return "kpi"
        # 6 short chips → filled 2×3 tiles (bokri slide3)
        if n == 6 and all(len(t) <= 48 for t in texts):
            return "tiles"
        if 2 <= n <= 6 and all(len(t) <= 72 for t in texts):
            sep_hits = sum(1 for t in texts if any(s in t for s in ("—", "–", " - ", ":", "：")))
            avg = sum(len(t) for t in texts) / n
            mx = max(len(t) for t in texts)
            if sep_hits >= (n + 1) // 2 or (avg <= 36 and mx <= 48):
                return "cards"
        if n >= 6 and all(len(t) <= 90 for t in texts):
            return "two_column"
        return "content"

    slides_spec = []
    if title:
        i = 0
        lead = []
        while i < len(blocks) and blocks[i].get("type") != "heading":
            lead.append(blocks[i])
            i += 1
        slides_spec.append({"heading": str(title), "layout": "title", "body": lead, "force_title": True})
        blocks_rest = blocks[i:]
        if (
            blocks_rest
            and blocks_rest[0].get("type") == "heading"
            and min(max(int(blocks_rest[0].get("level") or 1), 1), 3) == 1
            and str(blocks_rest[0].get("text") or "").strip() == str(title).strip()
        ):
            blocks_rest = blocks_rest[1:]
            j = 0
            while j < len(blocks_rest) and not (
                blocks_rest[j].get("type") == "heading"
                and min(max(int(blocks_rest[j].get("level") or 1), 1), 3) == 1
            ):
                slides_spec[0]["body"].append(blocks_rest[j])
                j += 1
            blocks_rest = blocks_rest[j:]
    else:
        blocks_rest = blocks

    cur = None
    for b in blocks_rest:
        t = b.get("type")
        if t == "heading" and min(max(int(b.get("level") or 1), 1), 3) == 1:
            cur = {
                "heading": str(b.get("text") or ""),
                "layout": str(b.get("layout") or "auto"),
                "body": [],
                "force_title": False,
            }
            slides_spec.append(cur)
        else:
            if cur is None:
                cur = {"heading": "", "layout": "auto", "body": [], "force_title": False}
                slides_spec.append(cur)
            cur["body"].append(b)

    if not slides_spec:
        slides_spec.append({"heading": "", "layout": "content", "body": [], "force_title": False})

    total = len(slides_spec)

    def _render_title(slide, heading, body, page):
        # White cover with navy title + optional 2×2 agenda cards (bokri slide1)
        _add_rect(slide, Inches(0), Inches(0), SW, SH, WHITE)
        _add_rect(slide, Inches(0), Inches(0), Inches(0.12), SH, NAVY)
        _add_rect(slide, Inches(0), Inches(0), SW, Inches(0.08), TEAL)

        eyebrow = _add_textbox(slide, MARGIN_X, Inches(0.45), CONTENT_W, Inches(0.35))
        er = eyebrow.text_frame.paragraphs[0].add_run()
        _set_run(er, FOOTER_LABEL, 12, bold=True, color=TEAL)

        box = _add_textbox(slide, MARGIN_X, Inches(0.9), CONTENT_W, Inches(1.35))
        tf = box.text_frame
        tf.word_wrap = True
        run = tf.paragraphs[0].add_run()
        _set_run(run, heading or "Presentation", 40, bold=True, color=NAVY)

        subs = []
        for b in body:
            if b.get("type") == "bullets":
                subs.extend(_bullets(b))
            elif b.get("type") in ("paragraph", "heading"):
                subs.append(str(b.get("text") or ""))
        subs = [s for s in subs if s and str(s).strip()]

        # 3–5 short items → 2×2 agenda cards (pad to 4 if 3)
        agenda = [s for s in subs if len(s) <= 80][:5]
        use_cards = 3 <= len(agenda) <= 5
        if use_cards:
            cards = list(agenda[:4])
            while len(cards) < 4:
                cards.append("")
            fills = [NAVY, TEAL, TEAL, NAVY]
            # geometry close to bokri: ~4.34×1.74 cards, start ~y=2.35
            cw, ch = Inches(5.85), Inches(1.65)
            gap_x, gap_y = Inches(0.28), Inches(0.22)
            origin_x, origin_y = MARGIN_X, Inches(2.45)
            for i, item in enumerate(cards):
                if not item:
                    continue
                r, c = divmod(i, 2)
                left = origin_x + c * (cw + gap_x)
                top = origin_y + r * (ch + gap_y)
                fill = fills[i]
                _add_rect(slide, left, top, cw, ch, fill)
                num = _add_textbox(slide, left + Inches(0.2), top + Inches(0.35), Inches(0.9), Inches(0.9))
                np = num.text_frame.paragraphs[0]
                np.alignment = PP_ALIGN.CENTER
                nr = np.add_run()
                _set_run(nr, f"{i+1:02d}", 28, bold=True, color=WHITE)
                # vertical divider
                _add_rect(slide, left + Inches(1.2), top + Inches(0.3), Inches(0.02), Inches(1.05), WHITE)
                title_p, body_p = _split_card(item)
                tb = _add_textbox(
                    slide, left + Inches(1.4), top + Inches(0.28),
                    cw - Inches(1.65), ch - Inches(0.45),
                )
                ttf = tb.text_frame
                ttf.word_wrap = True
                tr = ttf.paragraphs[0].add_run()
                _set_run(tr, title_p or item, 16, bold=True, color=WHITE)
                if body_p:
                    p2 = ttf.add_paragraph()
                    p2.space_before = Pt(4)
                    r2 = p2.add_run()
                    _set_run(r2, body_p[:60], 12, color=WHITE)
            # footer line under cards
            foot = [s for s in subs if s not in agenda[:4]]
            foot_text = foot[0] if foot else ""
            if not foot_text and len(agenda) == 5:
                foot_text = agenda[4]
            if foot_text:
                fb = _add_textbox(slide, MARGIN_X, Inches(6.15), CONTENT_W, Inches(0.55))
                fr = fb.text_frame.paragraphs[0].add_run()
                _set_run(fr, foot_text, 13, color=MUTED)
        else:
            # compact subtitle band (no English chrome)
            if not subs:
                subs = ["핵심 메시지를 한눈에"]
            _add_rect(slide, MARGIN_X, Inches(2.55), CONTENT_W, Inches(2.4), CARD_BG)
            _add_rect(slide, MARGIN_X, Inches(2.55), Inches(0.1), Inches(2.4), TEAL)
            sub = _add_textbox(
                slide, MARGIN_X + Inches(0.35), Inches(2.75),
                CONTENT_W - Inches(0.55), Inches(2.0),
            )
            stf = sub.text_frame
            stf.word_wrap = True
            for i, line in enumerate(subs[:5]):
                p = stf.paragraphs[0] if i == 0 else stf.add_paragraph()
                p.space_after = Pt(8)
                run = p.add_run()
                _set_run(run, line, 16 if i else 18, color=SLATE)
        _footer(slide, page, total, dark=False)

    def _render_section(slide, heading, body, page):
        _add_rect(slide, Inches(0), Inches(0), SW, SH, NAVY)
        _add_rect(slide, Inches(0), Inches(0), Inches(0.14), SH, TEAL)
        label = _add_textbox(slide, MARGIN_X, Inches(2.2), CONTENT_W, Inches(0.35))
        lr = label.text_frame.paragraphs[0].add_run()
        _set_run(lr, "섹션", 12, bold=True, color=TEAL)
        _add_rect(slide, MARGIN_X, Inches(2.65), Inches(1.6), Inches(0.07), TEAL)
        box = _add_textbox(slide, MARGIN_X, Inches(2.9), CONTENT_W, Inches(1.8))
        tf = box.text_frame
        tf.word_wrap = True
        run = tf.paragraphs[0].add_run()
        _set_run(run, heading or "Section", 36, bold=True, color=WHITE)
        text = None
        for b in body:
            if b.get("type") in ("paragraph", "heading"):
                text = str(b.get("text") or "")
            elif b.get("type") == "bullets":
                items = _bullets(b)
                text = items[0] if items else None
            if text:
                break
        if text:
            sub = _add_textbox(slide, MARGIN_X, Inches(5.0), CONTENT_W, Inches(1.0))
            run = sub.text_frame.paragraphs[0].add_run()
            _set_run(run, text, 16, color=GREY_LIGHT)
        _footer(slide, page, total, dark=True)

    def _add_bullets_box(slide, items, left, top, width, height, size=16):
        box = _add_textbox(slide, left, top, width, height)
        tf = box.text_frame
        tf.word_wrap = True
        for i, item in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(9)
            run = p.add_run()
            _set_run(run, f"•  {item}", size, color=SLATE)
        return box

    def _style_table(shape, rows):
        tbl = shape.table
        ncols = len(rows[0])
        for j in range(ncols):
            tbl.columns[j].width = int(CONTENT_W / ncols)
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                cell = tbl.cell(i, j)
                cell.text = ""
                cell.fill.solid()
                cell.fill.fore_color.rgb = NAVY if i == 0 else (ROW_ALT if i % 2 == 0 else WHITE)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                p = cell.text_frame.paragraphs[0]
                p.alignment = PP_ALIGN.LEFT
                run = p.add_run()
                _set_run(run, v, 13, bold=(i == 0), color=WHITE if i == 0 else SLATE)

    def _render_cards(slide, items, top):
        items = items[:6]
        n = max(len(items), 1)
        cols = 3 if n > 4 else (2 if n > 1 else 1)
        rows_n = (n + cols - 1) // cols
        gap_x, gap_y = Inches(0.22), Inches(0.2)
        card_w = (CONTENT_W - gap_x * (cols - 1)) / cols
        avail = Inches(5.35) - gap_y * (rows_n - 1)
        card_h = min(Inches(2.35), avail / max(rows_n, 1))
        for idx, item in enumerate(items):
            r, c = divmod(idx, cols)
            left = MARGIN_X + c * (card_w + gap_x)
            ctop = top + r * (card_h + gap_y)
            fill = NAVY if (r + c) % 2 == 0 else TEAL
            # solid filled cards with accent (bokri density)
            _add_rect(slide, left, ctop, card_w, card_h, fill)
            title_p, body_p = _split_card(item)
            tb = _add_textbox(
                slide, left + Inches(0.22), ctop + Inches(0.28),
                card_w - Inches(0.4), card_h - Inches(0.45),
            )
            tf = tb.text_frame
            tf.word_wrap = True
            run = tf.paragraphs[0].add_run()
            _set_run(run, title_p, 15, bold=True, color=WHITE)
            if body_p:
                p2 = tf.add_paragraph()
                p2.space_before = Pt(6)
                run2 = p2.add_run()
                _set_run(run2, body_p, 12, color=WHITE)

    def _render_kpi(slide, items, top):
        items = items[:4]
        n = max(len(items), 1)
        gap = Inches(0.22)
        box_w = (CONTENT_W - gap * (n - 1)) / n
        box_h = Inches(2.55)
        for idx, item in enumerate(items):
            label, value = _split_kpi(item)
            if not value:
                value, label = label, ""
            # prefer big number: if label looks like the value, swap when digit-heavy
            if value and label and any(ch.isdigit() for ch in label) and not any(ch.isdigit() for ch in value):
                label, value = value, label
            left = MARGIN_X + idx * (box_w + gap)
            fill = NAVY if idx % 2 == 0 else TEAL
            _add_rect(slide, left, top, box_w, box_h, fill)
            if label:
                lb = _add_textbox(slide, left + Inches(0.2), top + Inches(0.28), box_w - Inches(0.4), Inches(0.5))
                run = lb.text_frame.paragraphs[0].add_run()
                _set_run(run, label, 13, bold=True, color=WHITE)
            vb = _add_textbox(slide, left + Inches(0.2), top + Inches(0.9), box_w - Inches(0.4), Inches(1.2))
            vp = vb.text_frame.paragraphs[0]
            vp.alignment = PP_ALIGN.LEFT
            run = vp.add_run()
            # larger numbers like bokri ($50 / 44pt)
            size = 36 if len(value) <= 8 else (28 if len(value) <= 14 else 22)
            _set_run(run, value, size, bold=True, color=WHITE)

    def _render_tiles(slide, items, top, paras):
        # left text + 2×3 filled tiles (bokri slide3 geometry scaled)
        items = items[:6]
        left_w = Inches(3.6)
        if paras:
            pb = _add_textbox(slide, MARGIN_X, top, left_w, Inches(2.2))
            tf = pb.text_frame
            tf.word_wrap = True
            for i, text in enumerate(paras[:3]):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.space_after = Pt(8)
                run = p.add_run()
                _set_run(run, text, 14, color=MUTED)
        tw, th = Inches(2.55), Inches(2.35)
        gap_x, gap_y = Inches(0.18), Inches(0.18)
        ox = MARGIN_X + left_w + Inches(0.25)
        for idx, item in enumerate(items):
            r, c = divmod(idx, 3)
            left = ox + c * (tw + gap_x)
            ctop = top + r * (th + gap_y)
            fill = NAVY if (idx % 2 == 0) else TEAL
            _add_rect(slide, left, ctop, tw, th, fill)
            title_p, body_p = _split_card(item)
            tb = _add_textbox(
                slide, left + Inches(0.18), ctop + Inches(0.35),
                tw - Inches(0.35), th - Inches(0.55),
            )
            tf = tb.text_frame
            tf.word_wrap = True
            run = tf.paragraphs[0].add_run()
            _set_run(run, title_p, 15, bold=True, color=WHITE)
            if body_p:
                p2 = tf.add_paragraph()
                p2.space_before = Pt(8)
                r2 = p2.add_run()
                _set_run(r2, body_p, 12, color=WHITE)

    def _render_content(slide, heading, body, page, layout):
        _chrome_content(slide)
        _title_bar(slide, heading)
        top = Inches(1.4)
        paras, bullet_blocks, tables = [], [], []
        for b in body:
            t = b.get("type")
            if t == "heading":
                paras.append(str(b.get("text") or ""))
            elif t == "paragraph":
                paras.append(str(b.get("text") or ""))
            elif t == "bullets":
                bullet_blocks.append(b)
            elif t == "table":
                tables.append(_cells(b.get("rows") or []))

        use_two = layout == "two_column" or (
            len(bullet_blocks) == 1 and int(bullet_blocks[0].get("columns") or 1) == 2
        )

        items = []
        for bb in bullet_blocks:
            items.extend(_bullets(bb))

        if layout == "cards":
            _render_cards(slide, items or paras[:6], top)
        elif layout == "kpi":
            _render_kpi(slide, items or list(paras), top)
        elif layout == "tiles":
            _render_tiles(slide, items or paras[:6], top, paras if items else [])
        elif use_two:
            mid = max(1, (len(items) + 1) // 2)
            col_w = Inches(5.85)
            gap = Inches(0.35)
            if paras:
                pb = _add_textbox(slide, MARGIN_X, top, CONTENT_W, Inches(0.45))
                run = pb.text_frame.paragraphs[0].add_run()
                _set_run(run, paras[0], 14, color=MUTED)
                top = Inches(1.95)
            # light card panels behind columns
            _add_rect(slide, MARGIN_X, top, col_w, Inches(4.7), CARD_BG)
            _add_rect(slide, MARGIN_X, top, Inches(0.08), Inches(4.7), NAVY)
            _add_rect(slide, MARGIN_X + col_w + gap, top, col_w, Inches(4.7), CARD_BG)
            _add_rect(slide, MARGIN_X + col_w + gap, top, Inches(0.08), Inches(4.7), TEAL)
            _add_bullets_box(
                slide, items[:mid], MARGIN_X + Inches(0.25), top + Inches(0.2),
                col_w - Inches(0.4), Inches(4.4),
            )
            _add_bullets_box(
                slide, items[mid:], MARGIN_X + col_w + gap + Inches(0.25),
                top + Inches(0.2), col_w - Inches(0.4), Inches(4.4),
            )
        else:
            if paras:
                pb = _add_textbox(slide, MARGIN_X, top, CONTENT_W, Inches(0.55) * min(len(paras), 2))
                tf = pb.text_frame
                tf.word_wrap = True
                for i, text in enumerate(paras[:3]):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.space_after = Pt(6)
                    run = p.add_run()
                    _set_run(run, text, 15, color=MUTED)
                top += Inches(0.4) * min(len(paras), 2) + Inches(0.08)
            for bb in bullet_blocks:
                b_items = _bullets(bb)
                cols = int(bb.get("columns") or 1)
                if cols == 2 and len(b_items) > 2:
                    mid = (len(b_items) + 1) // 2
                    col_w = Inches(5.85)
                    _add_bullets_box(slide, b_items[:mid], MARGIN_X, top, col_w, Inches(4.6))
                    _add_bullets_box(
                        slide, b_items[mid:], MARGIN_X + col_w + Inches(0.35), top, col_w, Inches(4.6)
                    )
                    top += Inches(4.6)
                else:
                    h = min(Inches(0.42) * max(len(b_items), 1) + Inches(0.15), Inches(5.1))
                    _add_bullets_box(slide, b_items, MARGIN_X, top, CONTENT_W, h)
                    top += h + Inches(0.12)
            for rows in tables:
                if not rows:
                    continue
                row_h = Inches(0.4)
                th = row_h * len(rows)
                shape = slide.shapes.add_table(len(rows), len(rows[0]), MARGIN_X, top, CONTENT_W, th)
                _style_table(shape, rows)
                top += th + Inches(0.15)
        _footer(slide, page, total, dark=False)

    # --- huashu layout DNA (SESSION_REV=2026-10-25-huashu-theme-layout-dna) ---------------
    # family == "corporate" → the renderers above, untouched. Any other family gets its own
    # title/section/content renderers: typography, margins, chrome and grid all differ.
    FAMILY = pal.get("family") or _HUASHU_MODE_FAMILY.get(MODE, "corporate")
    F_SERIF = "NanumMyeongjo"
    F_SANS = "Noto Sans CJK KR"
    F_MONO = "Liberation Mono"
    F_POP = "NanumSquare"
    INK = _rgb("111111")
    HAIR = _rgb("D0D0D0") if _hex_lum(pal["bg"]) > 0.5 else _rgb("3A3A3E")
    SLUG_SHORT = theme_slug.split("-", 1)[-1] if theme_slug[:2].isdigit() else theme_slug
    SLUG_NO = theme_slug[:2] if theme_slug[:2].isdigit() else "00"
    SW_IN = 13.333

    _PROFILES = {
        #               title font, body font, number font, left/right margin (in)
        "magazine": (F_SERIF, F_SANS, F_SERIF, 1.1, 0.7),
        "research": (F_SANS, F_SANS, F_MONO, 0.55, 0.55),
        "consulting": (F_SERIF, F_SANS, F_SERIF, 0.8, 0.8),
        "billboard": (F_POP, F_SANS, F_POP, 0.7, 0.7),
        "dark_stage": (F_SANS, F_SANS, F_SANS, 0.9, 0.9),
        "bento": (F_POP, F_SANS, F_POP, 0.6, 0.6),
        "manifesto": (F_POP, F_SANS, F_POP, 0.8, 0.8),
        "pop": (F_POP, F_POP, F_POP, 0.7, 0.7),
        "minimal": (F_SANS, F_SANS, F_SANS, 1.2, 1.2),
    }
    T_FONT, B_FONT, N_FONT, ML, MR = _PROFILES.get(FAMILY, _PROFILES["research"])
    if FAMILY == "dark_stage" and CHROME == "hairline":
        N_FONT = F_MONO  # terminal / sparkline specimens: mono figures
    CW = SW_IN - ML - MR  # content width (in)

    def _I(v):
        return int(Inches(v))

    def _style(run, text, size, *, bold=False, italic=False, color=SLATE, font=None, spc=None):
        _set_run(run, text, size, bold=bold, color=color, font=font or B_FONT)
        if italic:
            run.font.italic = True
        if spc is not None:
            try:
                run._r.get_or_add_rPr().set("spc", str(int(spc * 100)))
            except Exception:
                pass

    def _txt(slide, x, y, w, h, lines, size, *, font=None, color=SLATE, bold=False, italic=False,
             align=None, anchor=None, spc=None, after=None, marker=None, marker_color=None,
             line_spacing=None):
        box = _add_textbox(slide, _I(x), _I(y), _I(w), _I(h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = 0
        tf.margin_top = tf.margin_bottom = 0
        if anchor is not None:
            tf.vertical_anchor = anchor
        if isinstance(lines, str):
            lines = [lines]
        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            if align is not None:
                p.alignment = align
            if after is not None:
                p.space_after = Pt(after)
            if line_spacing:
                p.line_spacing = line_spacing
            if marker:
                mk = marker(i) if callable(marker) else marker
                _style(p.add_run(), mk, size, bold=True, color=marker_color or TEAL, font=font)
            _style(p.add_run(), str(line), size, bold=bold, italic=italic, color=color,
                   font=font, spc=spc)
        return box

    def _rect(slide, x, y, w, h, color):
        return _add_rect(slide, _I(x), _I(y), _I(w), _I(h), color)

    def _rrect(slide, x, y, w, h, color, radius=0.08, line=None, line_w=1.0):
        s = _add_round(slide, _I(x), _I(y), _I(w), _I(h), color)
        try:
            s.adjustments[0] = radius
        except Exception:
            pass
        if line is not None:
            s.line.color.rgb = line
            s.line.width = Pt(line_w)
        return s

    def _frame(slide, x, y, w, h, line, fill=None, weight=0.75):
        s = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _I(x), _I(y), _I(w), _I(h))
        if fill is None:
            s.fill.background()
        else:
            _fill_solid(s, fill)
        s.line.color.rgb = line
        s.line.width = Pt(weight)
        return s

    def _bg(slide, color=None):
        _rect(slide, 0, 0, SW_IN, 7.5, color if color is not None else SLIDE_BG)

    def _on(hex_fill):
        return WHITE if _hex_lum(hex_fill) < 0.55 else INK

    def _collect(body):
        paras, items, tables = [], [], []
        for b in body:
            t = b.get("type")
            if t in ("heading", "paragraph"):
                s = str(b.get("text") or "").strip()
                if s:
                    paras.append(s)
            elif t == "bullets":
                items.extend(s for s in _bullets(b) if s.strip())
            elif t == "table":
                rows = _cells(b.get("rows") or [])
                if rows:
                    tables.append(rows)
        return paras, items, tables

    _NUM_RE = re.compile(r"[+\-]?\d[\d,.]*\s*(?:%|배|억|만|원|명|건|점|일|위|개|x|X|\+)?")

    def _kv(item):
        """'label: 92%' → ('92%', 'label'); value is the digit-bearing side."""
        a, b = _split_kpi(item)
        if not b:
            m = _NUM_RE.search(a)
            if m and m.group(0).strip():
                return m.group(0).strip(), (a[:m.start()] + a[m.end():]).strip(" :·-—")
            return a, ""
        if any(ch.isdigit() for ch in a) and not any(ch.isdigit() for ch in b):
            return a, b
        return b, a

    def _first_number(texts):
        for t in _kpi_items(texts):
            v, lbl = _kv(t)
            if any(ch.isdigit() for ch in v) and len(v) <= 10:
                return v, lbl
        return None, None

    _KPI_RE = re.compile(r"\d\s*(?:%|점|명|건|억|만|원|배|개|위|x\b)|[|:：]\s*[+\-]?\d")

    def _kpi_items(texts):
        return [t for t in texts if len(t) <= 60 and _KPI_RE.search(t)]

    def _est_h(text, size, width, spacing=1.3):
        """Rough wrapped height (in) of text at size pt in width in (CJK ≈ 1em/char)."""
        per_line = max(1, int(width * 72 / (size * 0.92)))
        lines = sum(max(1, -(-len(t) // per_line)) for t in ([text] if isinstance(text, str) else text))
        return lines * size / 72 * spacing

    def _table(slide, rows, x, y, w, *, size=12, row_h=0.38, head_fill=None, head_color=WHITE,
               body_fill=None, alt_fill=None, color=None, font=None, head_font=None):
        shape = slide.shapes.add_table(len(rows), len(rows[0]), _I(x), _I(y), _I(w), _I(row_h * len(rows)))
        tbl = shape.table
        ncols = len(rows[0])
        for j in range(ncols):
            tbl.columns[j].width = _I(w / ncols)
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                cell = tbl.cell(i, j)
                cell.text = ""
                fill = (head_fill if head_fill is not None else NAVY) if i == 0 else (
                    alt_fill if (alt_fill is not None and i % 2 == 0) else (body_fill or SLIDE_BG))
                cell.fill.solid()
                cell.fill.fore_color.rgb = fill
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_left = cell.margin_right = _I(0.08)
                p = cell.text_frame.paragraphs[0]
                p.alignment = PP_ALIGN.LEFT
                _style(p.add_run(), v, size, bold=(i == 0),
                       color=head_color if i == 0 else (color or SLATE),
                       font=(head_font or font) if i == 0 else font)
        return row_h * len(rows)

    def _chunks(seq, n):
        n = max(1, n)
        k = (len(seq) + n - 1) // n
        return [seq[i * k:(i + 1) * k] for i in range(n)] if k else [[] for _ in range(n)]

    # ---------------------------------------------------------------- magazine
    def _mag_mast(slide, page):
        _txt(slide, ML, 0.28, 6.0, 0.3, "유비온  ·  JOURNAL", 9, font=B_FONT, color=MUTED,
             bold=True, spc=3)
        _txt(slide, SW_IN - MR - 5.0, 0.28, 5.0, 0.3, f"NO.{page:02d}  —  {SLUG_SHORT.upper()}", 9,
             font=B_FONT, color=MUTED, align=PP_ALIGN.RIGHT, spc=2)
        _rect(slide, ML, 0.6, CW, 0.012, SLATE)
        _rect(slide, ML, 0.64, CW, 0.006, SLATE)

    def _mag_footer(slide, page, heading=""):
        _rect(slide, ML, 7.0, CW, 0.008, HAIR)
        _txt(slide, ML, 7.08, 8.0, 0.3, f"유비온  ·  {heading[:40]}" if heading else "유비온", 9,
             font=T_FONT, italic=True, color=MUTED)
        _txt(slide, SW_IN - MR - 2.0, 7.08, 2.0, 0.3, f"— {page} —", 9, font=T_FONT,
             color=MUTED, align=PP_ALIGN.RIGHT)

    def _mag_title(slide, heading, body, page):
        _bg(slide)
        _mag_mast(slide, page)
        paras, items, _t = _collect(body)
        subs = paras + items
        _txt(slide, ML, 1.05, 7.5, 0.3, "ESSAY  ·  유비온 사내 안내", 11, color=TEAL, bold=True, spc=2)
        _txt(slide, ML, 1.45, 7.9, 2.0, heading or "Presentation", 46, font=T_FONT, bold=True,
             color=SLATE, line_spacing=1.05)
        _txt(slide, ML, 3.55, 7.9, 0.3, "글  유비온 교육사업본부   ·   사내 배포용", 11, color=MUTED,
             italic=True)
        _rect(slide, ML, 4.0, 7.9, 0.01, HAIR)
        lead = paras[:1] + items[:4] if paras else items[:6]
        if lead:
            cols = _chunks(lead, 2)
            for c, chunk in enumerate(cols):
                if chunk:
                    _txt(slide, ML + c * 4.05, 4.2, 3.85, 2.6, chunk, 13, color=SLATE, after=8,
                         line_spacing=1.2)
        # right side card: huge accent numeral + caption
        num, lbl = _first_number(items + paras)
        cx, cw = ML + 8.35, CW - 8.35
        _rect(slide, cx, 1.05, cw, 5.7, CARD_BG)
        _rect(slide, cx, 1.05, cw, 0.06, TEAL)
        _txt(slide, cx + 0.3, 1.4, cw - 0.6, 0.3, "IN THIS ISSUE", 9, bold=True, color=MUTED, spc=3)
        _txt(slide, cx + 0.3, 1.8, cw - 0.6, 1.9, num or f"{max(len(subs), 1):02d}", 80 if not num or len(num) <= 4 else 54,
             font=N_FONT, bold=True, color=TEAL)
        _txt(slide, cx + 0.3, 3.8, cw - 0.6, 2.6, [lbl] if (num and lbl) else (items[:3] or ["핵심 메시지를 한눈에"]),
             12, color=SLATE, italic=bool(num and lbl), after=6)
        _mag_footer(slide, page)

    def _mag_section(slide, heading, body, page):
        _bg(slide)
        _mag_mast(slide, page)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 1.4, 4.0, 3.0, f"{page:02d}", 150, font=N_FONT, bold=True, color=TEAL)
        _txt(slide, ML + 4.2, 2.3, CW - 4.2, 0.3, "CHAPTER", 10, bold=True, color=MUTED, spc=4)
        _txt(slide, ML + 4.2, 2.7, CW - 4.2, 1.8, heading or "Section", 40, font=T_FONT, bold=True,
             color=SLATE)
        _rect(slide, ML + 4.2, 4.6, 1.2, 0.03, TEAL)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML + 4.2, 4.85, CW - 4.2, 1.2, sub, 16, font=T_FONT, italic=True, color=MUTED)
        _mag_footer(slide, page, heading)

    def _mag_content(slide, heading, body, page, layout):
        _bg(slide)
        _mag_mast(slide, page)
        paras, items, tables = _collect(body)
        _txt(slide, ML, 0.85, CW, 0.9, heading, 30, font=T_FONT, bold=True, color=SLATE)
        _rect(slide, ML, 1.72, 0.8, 0.04, TEAL)
        top = 2.0
        rail = layout not in ("cards", "tiles", "kpi") and not tables
        main_w = CW - 3.35 if rail else CW
        if layout == "kpi":
            vals = (items or paras)[:4]
            n = max(len(vals), 1)
            w = CW / n
            for i, it in enumerate(vals):
                v, lbl = _kv(it)
                x = ML + i * w
                if i:
                    _rect(slide, x, top + 0.3, 0.01, 3.2, HAIR)
                _txt(slide, x + 0.25, top + 0.4, w - 0.4, 1.6, v, 60 if len(v) <= 6 else 40, font=N_FONT,
                     bold=True, color=TEAL)
                _txt(slide, x + 0.25, top + 2.1, w - 0.4, 1.2, lbl, 14, font=T_FONT, italic=True, color=SLATE)
            _mag_footer(slide, page, heading)
            return
        if layout in ("cards", "tiles"):
            vals = (items or paras)[:6]
            ncol = 3 if (layout == "tiles" or len(vals) > 4) else 2
            if layout == "tiles" and items and paras:
                _txt(slide, ML, top, CW, 0.4, paras[0], 14, font=T_FONT, italic=True, color=MUTED)
                top += 0.55
            colw = (CW - 0.4 * (ncol - 1)) / ncol
            rows_n = max(1, (len(vals) + ncol - 1) // ncol)
            rh = min(2.3, (6.7 - top) / rows_n)
            for i, it in enumerate(vals):
                r, c = divmod(i, ncol)
                x, y = ML + c * (colw + 0.4), top + r * rh
                _rect(slide, x, y, colw, 0.012, SLATE)
                _txt(slide, x, y + 0.15, 0.9, 0.6, f"{i + 1:02d}", 22, font=N_FONT, bold=True, color=TEAL)
                t1, t2 = _split_card(it)
                _txt(slide, x + 0.8, y + 0.18, colw - 0.8, 0.5, t1, 16, font=T_FONT, bold=True, color=SLATE)
                if t2:
                    _txt(slide, x + 0.8, y + 0.65, colw - 0.8, rh - 0.8, t2, 12, color=MUTED, line_spacing=1.2)
            _mag_footer(slide, page, heading)
            return
        long_para = next((p for p in paras if len(p) > 90), None)
        if long_para:
            _rect(slide, ML, top, 0.06, 1.25, TEAL)
            _txt(slide, ML + 0.3, top, main_w - 0.3, 1.25, f"“{long_para[:150]}”", 17, font=T_FONT,
                 italic=True, color=SLATE, anchor=MSO_ANCHOR.MIDDLE, line_spacing=1.15)
            top += 1.5
        flow = [p for p in paras if p is not long_para]
        for rows in tables:
            if flow:
                _txt(slide, ML, top, main_w, 0.4, flow.pop(0), 13, font=T_FONT, italic=True, color=MUTED)
                top += 0.5
            h = _table(slide, rows, ML, top, main_w, size=12, head_fill=SLIDE_BG, head_color=SLATE,
                       body_fill=SLIDE_BG, alt_fill=CARD_BG, font=B_FONT, head_font=T_FONT)
            _rect(slide, ML, top + 0.38, main_w, 0.015, SLATE)
            top += h + 0.25
        blocks_txt = flow + [f"■  {it}" for it in items]
        if blocks_txt:
            ncol = 2 if (len(blocks_txt) >= 3 or layout == "two_column" or sum(map(len, blocks_txt)) > 160) else 1
            colw = (main_w - 0.4 * (ncol - 1)) / ncol
            for c, chunk in enumerate(_chunks(blocks_txt, ncol)):
                if not chunk:
                    continue
                x = ML + c * (colw + 0.4)
                if c:
                    _rect(slide, x - 0.2, top, 0.008, 6.75 - top, HAIR)
                _txt(slide, x, top, colw, 6.8 - top, chunk, 14, color=SLATE, after=10, line_spacing=1.25)
        if rail:
            rx = ML + main_w + 0.35
            _rect(slide, rx, 2.0, 0.01, 4.75, HAIR)
            num, lbl = _first_number(paras + items)
            _txt(slide, rx + 0.35, 2.0, 2.9, 0.3, "BY THE NUMBERS" if num else "NOTE", 9, bold=True,
                 color=MUTED, spc=3)
            _txt(slide, rx + 0.35, 2.35, 2.9, 1.6, num or f"{page:02d}", 64 if not num or len(num) <= 5 else 40,
                 font=N_FONT, bold=True, color=TEAL)
            cap = lbl if num else ((items or paras or [heading])[0])
            if cap:
                _txt(slide, rx + 0.35, 4.1, 2.9, 2.4, cap[:120], 13, font=T_FONT, italic=True, color=MUTED,
                     line_spacing=1.2)
        _mag_footer(slide, page, heading)

    # ---------------------------------------------------------------- research
    def _res_header(slide, page):
        _txt(slide, ML, 0.18, 6.0, 0.28, "유비온  ·  REPORT", 10, bold=True, color=SLATE, spc=1)
        _txt(slide, SW_IN - MR - 6.0, 0.18, 6.0, 0.28, f"{SLUG_SHORT}  ·  리서치 리포트  ·  {page:02d}",
             9, font=F_MONO, color=MUTED, align=PP_ALIGN.RIGHT)
        _rect(slide, ML, 0.5, CW, 0.07, TEAL)

    def _res_footer(slide, page):
        _rect(slide, ML, 7.02, CW, 0.008, HAIR)
        _txt(slide, ML, 7.1, 8.0, 0.25, "Source: 유비온 사내 자료  ·  Internal use only", 8, font=F_MONO,
             color=MUTED)
        _txt(slide, SW_IN - MR - 2.0, 7.1, 2.0, 0.25, f"p. {page}/{total}", 8, font=F_MONO,
             color=MUTED, align=PP_ALIGN.RIGHT)

    def _res_panel(slide, x, y, w, h, label, lines, *, size=12, mark="▪ "):
        _frame(slide, x, y, w, h, HAIR, fill=SLIDE_BG, weight=0.75)
        _rect(slide, x, y, w, 0.04, TEAL)
        _txt(slide, x + 0.15, y + 0.14, w - 0.3, 0.25, label, 8, font=F_MONO, bold=True, color=TEAL, spc=1)
        if lines:
            _txt(slide, x + 0.15, y + 0.45, w - 0.3, h - 0.55, lines, size, color=SLATE, after=4,
                 marker=mark, marker_color=TEAL)

    def _res_title(slide, heading, body, page):
        _bg(slide)
        _res_header(slide, page)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 0.85, 8.0, 0.25, "RESEARCH REPORT  /  연구 보고서", 10, font=F_MONO, bold=True,
             color=TEAL, spc=1)
        _txt(slide, ML, 1.2, 7.6, 1.7, heading or "Report", 34, font=T_FONT, bold=True, color=SLATE)
        abstract = paras[:2] or items[:2] or ["핵심 메시지를 한눈에"]
        _res_panel(slide, ML, 3.15, 7.6, 2.0, "ABSTRACT", abstract, size=12, mark="")
        kpis = _kpi_items(items + paras)[:4]
        gx = ML + 7.85
        gw = CW - 7.85
        if kpis:
            cw2 = (gw - 0.15) / 2
            for i, it in enumerate(kpis):
                r, c = divmod(i, 2)
                x, y = gx + c * (cw2 + 0.15), 1.2 + r * 2.0
                v, lbl = _kv(it)
                _frame(slide, x, y, cw2, 1.85, HAIR, fill=CARD_BG)
                _txt(slide, x + 0.15, y + 0.15, cw2 - 0.3, 0.3, (lbl or "KPI")[:24], 9, bold=True, color=MUTED)
                _txt(slide, x + 0.15, y + 0.6, cw2 - 0.3, 0.9, v, 30 if len(v) <= 6 else 20, font=N_FONT,
                     bold=True, color=TEAL)
        else:
            _res_panel(slide, gx, 1.2, gw, 3.95, "KEY POINTS", (items or paras[2:])[:6] or ["—"], size=12)
        metas = [("발행", "유비온"), ("분류", "사내 / Internal"), ("구성", f"{total} pages"),
                 ("버전", SLUG_NO)]
        mw = CW / len(metas)
        for i, (k, v) in enumerate(metas):
            x = ML + i * mw
            _frame(slide, x, 5.5, mw, 0.95, HAIR)
            _txt(slide, x + 0.15, 5.6, mw - 0.3, 0.25, k, 8, font=F_MONO, color=MUTED)
            _txt(slide, x + 0.15, 5.9, mw - 0.3, 0.4, v, 13, bold=True, color=SLATE)
        _res_footer(slide, page)

    def _res_section(slide, heading, body, page):
        _bg(slide)
        _res_header(slide, page)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 1.6, 4.0, 1.4, f"§{page:02d}", 72, font=F_MONO, bold=True, color=TEAL)
        _txt(slide, ML, 3.1, CW, 1.2, heading or "Section", 32, font=T_FONT, bold=True, color=SLATE)
        _rect(slide, ML, 4.3, CW, 0.07, TEAL)
        sub = (paras + items)[:2]
        if sub:
            _txt(slide, ML, 4.6, CW * 0.7, 1.3, sub, 14, color=MUTED, after=4)
        _res_footer(slide, page)

    def _res_content(slide, heading, body, page, layout):
        _bg(slide)
        _res_header(slide, page)
        paras, items, tables = _collect(body)
        _txt(slide, ML, 0.72, CW, 0.6, heading, 26, font=T_FONT, bold=True, color=SLATE)
        top = 1.42
        if paras and not (layout == "kpi" and not items):
            lede = paras[0]
            _txt(slide, ML, top, CW, 0.5, lede if len(lede) <= 200 else lede[:197] + "…", 12, color=MUTED)
            top += _est_h(lede[:200], 12, CW) + 0.2
            paras = paras[1:]
        bottom = 6.85
        if layout == "kpi":
            vals = (items or paras)[:4]
            n = max(len(vals), 1)
            w = (CW - 0.15 * (n - 1)) / n
            for i, it in enumerate(vals):
                v, lbl = _kv(it)
                x = ML + i * (w + 0.15)
                _frame(slide, x, top, w, 2.6, HAIR, fill=CARD_BG)
                _rect(slide, x, top, w, 0.05, TEAL)
                _txt(slide, x + 0.2, top + 0.2, w - 0.4, 0.3, f"M{i + 1:02d}  {lbl}"[:40], 9, font=F_MONO,
                     bold=True, color=MUTED)
                _txt(slide, x + 0.2, top + 0.8, w - 0.4, 1.4, v, 40 if len(v) <= 6 else 26, font=N_FONT,
                     bold=True, color=TEAL)
            _res_footer(slide, page)
            return
        for rows in tables:
            if top > bottom - 0.8:
                break
            row_h = min(0.34, (bottom - top) / len(rows))
            h = _table(slide, rows, ML, top, CW, size=11, row_h=row_h, head_fill=NAVY, head_color=_on(pal["primary"]),
                       body_fill=SLIDE_BG, alt_fill=CARD_BG, font=B_FONT)
            top += h + 0.2
        vals = items or (paras if layout in ("cards", "tiles", "two_column") else [])
        rest = [] if vals is paras else paras
        if rest and top < bottom - 0.8:
            h = min(1.6, 0.3 + 0.35 * len(rest))
            _res_panel(slide, ML, top, CW, h, "NOTE", rest[:4], size=11, mark="")
            top += h + 0.15
        if vals and top < bottom - 0.8:
            if layout in ("cards", "tiles"):
                ncol = 3 if len(vals) > 4 or layout == "tiles" else 2
                rows_n = max(1, (len(vals[:6]) + ncol - 1) // ncol)
                w = (CW - 0.15 * (ncol - 1)) / ncol
                h = min(2.4, (bottom - top - 0.15 * (rows_n - 1)) / rows_n)
                for i, it in enumerate(vals[:6]):
                    r, c = divmod(i, ncol)
                    t1, t2 = _split_card(it)
                    x, y = ML + c * (w + 0.15), top + r * (h + 0.15)
                    _frame(slide, x, y, w, h, HAIR, fill=SLIDE_BG)
                    _rect(slide, x, y, 0.05, h, TEAL)
                    _txt(slide, x + 0.2, y + 0.15, w - 0.35, 0.25, f"{chr(65 + i)} · {i + 1:02d}", 8,
                         font=F_MONO, bold=True, color=TEAL)
                    _txt(slide, x + 0.2, y + 0.45, w - 0.35, 0.4, t1, 15, bold=True, color=SLATE)
                    if t2:
                        _txt(slide, x + 0.2, y + 0.9, w - 0.35, h - 1.0, t2, 11, color=MUTED)
            else:
                ncol = 3 if len(vals) >= 6 else (2 if len(vals) >= 2 else 1)
                w = (CW - 0.15 * (ncol - 1)) / ncol
                for c, chunk in enumerate(_chunks(vals, ncol)):
                    if chunk:
                        ph = min(bottom - top, 0.75 + 0.36 * max(len(x) for x in _chunks(vals, ncol)))
                        _res_panel(slide, ML + c * (w + 0.15), top, w, ph,
                                   f"{chr(65 + c)} · FINDINGS {c + 1:02d}", chunk, size=12)
        _res_footer(slide, page)

    # ---------------------------------------------------------------- consulting
    def _con_top(slide, page):
        _txt(slide, ML, 0.25, 4.0, 0.25, "유비온", 10, bold=True, color=NAVY, spc=1)
        _txt(slide, SW_IN - MR - 3.0, 0.25, 3.0, 0.25, f"{page}", 10, color=MUTED, align=PP_ALIGN.RIGHT)

    def _con_footer(slide, page):
        _rect(slide, ML, 6.98, CW, 0.008, HAIR)
        _txt(slide, ML, 7.06, 9.0, 0.25, "Source: 유비온 사내 자료 분석", 8, color=MUTED, italic=True)
        _txt(slide, SW_IN - MR - 2.0, 7.06, 2.0, 0.25, "유비온 | CONFIDENTIAL", 8, color=MUTED,
             align=PP_ALIGN.RIGHT, spc=1)

    def _con_head(slide, heading, kick):
        _txt(slide, ML, 0.62, CW, 0.28, kick.upper(), 10, bold=True, color=TEAL, spc=2)
        _txt(slide, ML, 0.92, CW, 1.0, heading, 28, font=T_FONT, bold=True, color=NAVY)
        _rect(slide, ML, 1.9, 0.9, 0.04, TEAL)

    def _con_title(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        _rect(slide, 0, 0, SW_IN, 0.06, NAVY)
        _txt(slide, ML, 0.55, 6.0, 0.3, "유비온", 12, bold=True, color=NAVY, spc=1)
        _txt(slide, ML, 1.9, 7.4, 0.3, "BRIEFING  ·  사내 안내", 11, bold=True, color=TEAL, spc=3)
        _txt(slide, ML, 2.3, 7.4, 2.4, heading or "Presentation", 44, font=T_FONT, bold=True, color=NAVY,
             line_spacing=1.05)
        _rect(slide, ML, 4.75, 1.1, 0.05, TEAL)
        if paras:
            _txt(slide, ML, 5.0, 7.0, 1.2, paras[:2], 15, color=SLATE, after=4)
        agenda = (items or paras[2:])[:5]
        if agenda:
            ax, aw = ML + 8.0, CW - 8.0
            _txt(slide, ax, 1.9, aw, 0.3, "CONTENTS", 10, bold=True, color=MUTED, spc=3)
            for i, it in enumerate(agenda):
                y = 2.35 + i * 0.85
                _rect(slide, ax, y, aw, 0.008, HAIR)
                _txt(slide, ax, y + 0.15, 0.6, 0.5, f"{i + 1:02d}", 16, font=T_FONT, bold=True, color=TEAL)
                _txt(slide, ax + 0.65, y + 0.15, aw - 0.65, 0.6, it, 13, color=SLATE)
        _con_footer(slide, page)

    def _con_section(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        _rect(slide, 0, 0, 4.4, 7.5, NAVY)
        _txt(slide, 0.8, 2.3, 3.2, 0.3, "EXHIBIT", 11, bold=True, color=_on(pal["primary"]), spc=4)
        _txt(slide, 0.8, 2.7, 3.2, 1.8, f"{page:02d}", 96, font=T_FONT, bold=True, color=_on(pal["primary"]))
        _txt(slide, 5.0, 2.6, SW_IN - 5.0 - MR, 1.8, heading or "Section", 38, font=T_FONT, bold=True,
             color=NAVY)
        _rect(slide, 5.0, 4.5, 1.0, 0.05, TEAL)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, 5.0, 4.8, SW_IN - 5.0 - MR, 1.2, sub, 16, color=SLATE)

    def _con_card(slide, x, y, w, h, t1, t2, idx, big=False):
        _rect(slide, x, y, w, h, CARD_BG)
        _rect(slide, x, y, w, 0.05, TEAL if idx % 2 == 0 else NAVY)
        if big:
            _txt(slide, x + 0.2, y + 0.25, w - 0.4, 0.9, t1, 34 if len(t1) <= 6 else 22, font=T_FONT,
                 bold=True, color=NAVY)
            if t2:
                _txt(slide, x + 0.2, y + 1.15, w - 0.4, h - 1.25, t2, 11, color=SLATE)
            return
        _txt(slide, x + 0.2, y + 0.2, w - 0.4, 0.5, t1, 14, bold=True, color=NAVY)
        if t2:
            _txt(slide, x + 0.2, y + 0.65, w - 0.4, h - 0.75, t2, 11, color=SLATE)

    def _con_content(slide, heading, body, page, layout):
        _bg(slide)
        _con_top(slide, page)
        paras, items, tables = _collect(body)
        kick = {"kpi": "Key figures", "cards": "Priorities", "tiles": "Portfolio",
                "two_column": "Roadmap"}.get(layout, "Insight")
        _con_head(slide, heading, f"Exhibit {page}  ·  {kick}")
        top = 2.15
        if tables:
            if paras:
                _txt(slide, ML, top, CW, 0.4, paras[0], 13, color=SLATE)
                top += 0.55
            for rows in tables:
                h = _table(slide, rows, ML, top, CW, size=12, head_fill=NAVY, head_color=_on(pal["primary"]),
                           body_fill=SLIDE_BG, alt_fill=CARD_BG, font=B_FONT)
                top += h + 0.2
            _con_footer(slide, page)
            return
        lw = 4.9
        gx = ML + lw + 0.45
        gw = CW - lw - 0.45
        cards = list(items)
        left = list(paras)
        if not left and cards and layout not in ("kpi", "cards", "tiles"):
            left = [cards.pop(0)]
        overflow = cards[6:]
        cards = cards[:6]
        if not cards and len(left) > 1:
            cards, left = left[1:7], left[:1]
        if left:
            _txt(slide, ML, top, lw, 0.9, left[0], 18 if len(left[0]) <= 80 else 14, font=T_FONT, bold=True,
                 color=NAVY, line_spacing=1.15)
            rest = left[1:] + overflow
            if rest:
                _txt(slide, ML, top + 1.5, lw, 6.8 - top - 1.5, rest, 12, color=SLATE, after=6,
                     marker="—  ", marker_color=TEAL)
        elif overflow:
            _txt(slide, ML, top, lw, 4.5, overflow, 14, color=SLATE)
        else:
            gx, gw = ML, CW
        if cards:
            ncol = (2 if len(cards) > 1 else 1) if gw < CW else min(len(cards), 3 if len(cards) > 4 else 4)
            rows_n = (len(cards) + ncol - 1) // ncol
            cw2 = (gw - 0.2 * (ncol - 1)) / ncol
            ch = min(2.2, (6.8 - top - 0.2 * (rows_n - 1)) / rows_n)
            for i, it in enumerate(cards):
                r, c = divmod(i, ncol)
                if layout == "kpi":
                    t1, t2 = _kv(it)
                else:
                    t1, t2 = _split_card(it)
                _con_card(slide, gx + c * (cw2 + 0.2), top + r * (ch + 0.2), cw2, ch, t1, t2, i,
                          big=(layout == "kpi"))
        _con_footer(slide, page)

    # ---------------------------------------------------------------- billboard
    def _bb_header(slide, page):
        _txt(slide, ML, 0.14, 6.0, 0.25, f"UBION / {page:02d}", 9, font=F_MONO, bold=True, color=SLATE)
        _txt(slide, SW_IN - MR - 6.0, 0.14, 6.0, 0.25, f"{SLUG_SHORT.upper()} / {total:02d}", 9,
             font=F_MONO, color=MUTED, align=PP_ALIGN.RIGHT)
        _rect(slide, ML, 0.42, CW, 0.025, SLATE)

    def _bb_footer(slide, page):
        _txt(slide, ML, 7.1, 6.0, 0.25, "유비온 — 사내 안내", 9, font=F_MONO, color=MUTED)

    def _bb_strip(slide, vals, y=5.45, h=1.4):
        vals = vals[:4]
        if not vals:
            return
        _rect(slide, ML, y, CW, 0.025, SLATE)
        w = CW / len(vals)
        for i, it in enumerate(vals):
            v, lbl = _kv(it)
            x = ML + i * w
            if i:
                _rect(slide, x, y + 0.2, 0.01, h - 0.3, HAIR)
            _txt(slide, x + (0.25 if i else 0), y + 0.2, w - 0.4, 0.75, v, 30 if len(v) <= 7 else 20,
                 font=N_FONT, bold=True, color=TEAL)
            _txt(slide, x + (0.25 if i else 0), y + 0.95, w - 0.4, 0.4, lbl[:30], 11, font=F_MONO, color=MUTED)

    def _bb_title(slide, heading, body, page):
        _bg(slide)
        _bb_header(slide, page)
        paras, items, _t = _collect(body)
        subs = paras + items
        num, lbl = _first_number(items + paras)
        kpis = _kpi_items(items)
        if num:
            _txt(slide, ML, 0.7, CW * 0.62, 3.2, num, 150 if len(num) <= 4 else 110, font=N_FONT, bold=True,
                 color=TEAL, line_spacing=0.9)
            _txt(slide, ML + CW * 0.64, 1.3, CW * 0.36, 0.3, (lbl or "KEY FIGURE").upper(), 10, font=F_MONO,
                 bold=True, color=MUTED)
            _txt(slide, ML + CW * 0.64, 1.7, CW * 0.36, 2.6, heading or "Presentation", 32, bold=True,
                 color=SLATE, font=T_FONT)
            if paras:
                _txt(slide, ML, 4.25, CW * 0.6, 0.9, paras[0], 15, color=MUTED)
        else:
            _txt(slide, ML, 0.9, CW, 3.6, heading or "Presentation", 72, font=T_FONT, bold=True, color=SLATE,
                 line_spacing=0.95)
            if paras:
                _txt(slide, ML, 4.4, CW * 0.65, 0.9, paras[0], 16, color=MUTED)
        strip = (kpis[1:] if num and len(kpis) > 4 else kpis)[:4] or [s for s in subs if s not in paras[:1]][:4]
        _bb_strip(slide, strip)
        _bb_footer(slide, page)

    def _bb_section(slide, heading, body, page):
        _bg(slide)
        _bb_header(slide, page)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 0.8, 6.0, 3.6, f"{page:02d}", 220, font=N_FONT, bold=True, color=TEAL, line_spacing=0.85)
        _txt(slide, ML, 4.6, CW, 1.2, heading or "Section", 44, font=T_FONT, bold=True, color=SLATE)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML, 5.85, CW * 0.7, 0.8, sub, 16, color=MUTED)
        _bb_footer(slide, page)

    def _bb_content(slide, heading, body, page, layout):
        _bg(slide)
        _bb_header(slide, page)
        paras, items, tables = _collect(body)
        _txt(slide, ML, 0.65, CW, 1.0, heading, 36, font=T_FONT, bold=True, color=SLATE)
        top = 1.8
        if layout == "kpi":
            vals = (items or paras)[:4]
            n = max(len(vals), 1)
            w = CW / n
            for i, it in enumerate(vals):
                v, lbl = _kv(it)
                x = ML + i * w
                _rect(slide, x, top + 0.2, w - 0.3, 0.12, TEAL if i == 0 else SLATE)
                _txt(slide, x, top + 0.55, w - 0.3, 2.0, v, 66 if len(v) <= 5 else 40, font=N_FONT, bold=True,
                     color=TEAL if i == 0 else SLATE)
                _txt(slide, x, top + 2.7, w - 0.3, 0.8, lbl, 14, font=F_MONO, color=MUTED)
            _bb_footer(slide, page)
            return
        if paras and layout != "tiles":
            ph = _est_h(paras[0], 20, CW * 0.75, 1.25)
            _txt(slide, ML, top, CW * 0.75, ph, paras[0], 20, color=SLATE, bold=True)
            top += ph + 0.3
        for rows in tables:
            h = _table(slide, rows, ML, top, CW, size=13, row_h=0.42, head_fill=SLATE, head_color=SLIDE_BG,
                       body_fill=SLIDE_BG, alt_fill=CARD_BG, font=B_FONT, head_font=F_MONO)
            top += h + 0.2
        kpis = _kpi_items(items)
        if layout in ("cards", "tiles"):
            vals = (items or paras)[:6]
            ncol = 3 if len(vals) > 4 or layout == "tiles" else max(len(vals), 1)
            if layout == "tiles" and paras and items:
                _txt(slide, ML, top, CW, 0.4, paras[0], 14, font=F_MONO, color=MUTED)
                top += 0.55
            rows_n = max(1, (len(vals) + ncol - 1) // ncol)
            w = CW / ncol
            h = (6.85 - top) / rows_n
            for i, it in enumerate(vals):
                r, c = divmod(i, ncol)
                x, y = ML + c * w, top + r * h
                t1, t2 = _split_card(it)
                _rect(slide, x, y, w - 0.3, 0.06, TEAL if i == 0 else SLATE)
                _txt(slide, x, y + 0.15, 1.0, 0.4, f"{i + 1:02d}", 11, font=F_MONO, bold=True, color=TEAL)
                _txt(slide, x, y + 0.5, w - 0.35, 0.8, t1, 22 if layout == "cards" else 18, font=T_FONT,
                     bold=True, color=SLATE)
                if t2:
                    _txt(slide, x, y + 1.25, w - 0.35, max(0.4, h - 1.35), t2, 13, color=MUTED)
        elif items:
            strip = kpis[:4] if len(kpis) >= 2 else []
            listed = [it for it in items if it not in strip]
            bottom = 5.3 if strip else 6.85
            if listed:
                ncol = 2 if len(listed) > 4 else 1
                w = (CW - 0.5 * (ncol - 1)) / ncol
                for c, chunk in enumerate(_chunks(listed, ncol)):
                    if chunk:
                        _txt(slide, ML + c * (w + 0.5), top, w, bottom - top, chunk, 20, color=SLATE, after=10,
                             marker="→ ", marker_color=TEAL)
            if strip:
                _bb_strip(slide, strip)
        _bb_footer(slide, page)

    # ---------------------------------------------------------------- dark stage
    def _ds_footer(slide, page, heading=""):
        _rect(slide, ML, 7.0, CW, 0.006, HAIR)
        _txt(slide, ML, 7.08, 8.0, 0.25, f"유비온{('  ·  ' + heading[:40]) if heading else ''}", 9,
             font=F_MONO if CHROME == "hairline" else B_FONT, color=MUTED)
        _txt(slide, SW_IN - MR - 2.0, 7.08, 2.0, 0.25, f"{page:02d} / {total:02d}", 9, font=F_MONO,
             color=MUTED, align=PP_ALIGN.RIGHT)

    def _ds_title(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        num, lbl = _first_number(items + paras)
        if num:
            _txt(slide, ML, 0.9, CW, 3.2, num, 160 if len(num) <= 4 else 110, font=N_FONT, bold=True,
                 color=TEAL, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
            if lbl:
                _txt(slide, ML, 4.15, CW, 0.4, lbl, 16, color=MUTED, align=PP_ALIGN.CENTER)
            _txt(slide, ML, 4.8, CW, 1.4, heading or "Presentation", 34, font=T_FONT, bold=True, color=SLATE,
                 align=PP_ALIGN.CENTER)
        else:
            words = (heading or "Presentation").split()
            if len(words) <= 1:
                stacked = words or ["Presentation"]
            else:
                half = (len(words) + 1) // 2
                stacked = [" ".join(words[:half]), " ".join(words[half:])]
            _txt(slide, ML, 1.3, CW, 4.0, stacked, 72, font=T_FONT, bold=True, color=SLATE, line_spacing=0.95)
            _rect(slide, ML, 5.4, 1.2, 0.05, TEAL)
            if paras or items:
                _txt(slide, ML, 5.6, CW * 0.7, 1.0, (paras or items)[:1], 16, color=MUTED)
        _ds_footer(slide, page)

    def _ds_section(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 2.4, CW, 0.35, f"— {page:02d}", 12, font=F_MONO, bold=True, color=TEAL,
             align=PP_ALIGN.CENTER, spc=2)
        _txt(slide, ML, 2.9, CW, 1.6, heading or "Section", 48, font=T_FONT, bold=True, color=SLATE,
             align=PP_ALIGN.CENTER)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML + 1.5, 4.6, CW - 3.0, 1.0, sub, 16, color=MUTED, align=PP_ALIGN.CENTER)
        _ds_footer(slide, page)

    def _ds_content(slide, heading, body, page, layout):
        _bg(slide)
        paras, items, tables = _collect(body)
        _txt(slide, ML, 0.55, CW, 0.3, f"{page:02d}", 11, font=F_MONO, bold=True, color=TEAL, spc=2)
        _txt(slide, ML, 0.85, CW, 1.0, heading, 32, font=T_FONT, bold=True, color=SLATE)
        top = 2.0
        if layout == "kpi":
            vals = (items or paras)[:4]
            n = max(len(vals), 1)
            w = CW / n
            for i, it in enumerate(vals):
                v, lbl = _kv(it)
                x = ML + i * w
                vs = (72 if len(v) <= 4 else 44) * (0.75 if N_FONT == F_MONO else 1)
                _txt(slide, x, top + 0.6, w, 1.8, v, int(vs), font=N_FONT, bold=True,
                     color=TEAL if i == 0 else SLATE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
                _txt(slide, x, top + 2.6, w, 0.8, lbl, 14, color=MUTED, align=PP_ALIGN.CENTER)
            _ds_footer(slide, page, heading)
            return
        if paras:
            ph = _est_h(paras[0], 18, CW * 0.8, 1.45)
            _txt(slide, ML, top, CW * 0.8, ph, paras[0], 18, color=MUTED, line_spacing=1.2)
            top += ph + 0.3
        for rows in tables:
            h = _table(slide, rows, ML, top, CW, size=12, head_fill=CARD_BG, head_color=TEAL,
                       body_fill=SLIDE_BG, alt_fill=CARD_BG, color=SLATE, font=B_FONT, head_font=N_FONT)
            top += h + 0.2
        vals = items or (paras[1:] if layout in ("cards", "tiles") else [])
        if layout in ("cards", "tiles") and vals:
            vals = vals[:6]
            ncol = 3 if len(vals) > 4 or layout == "tiles" else max(len(vals), 1)
            rows_n = max(1, (len(vals) + ncol - 1) // ncol)
            w = (CW - 0.2 * (ncol - 1)) / ncol
            h = min(2.4, (6.8 - top - 0.2 * (rows_n - 1)) / rows_n)
            for i, it in enumerate(vals):
                r, c = divmod(i, ncol)
                x, y = ML + c * (w + 0.2), top + r * (h + 0.2)
                t1, t2 = _split_card(it)
                _rect(slide, x, y, w, h, CARD_BG)
                _rect(slide, x, y, 0.03, h, TEAL)
                _txt(slide, x + 0.25, y + 0.25, w - 0.45, 0.6, t1, 17, bold=True, color=SLATE)
                if t2:
                    _txt(slide, x + 0.25, y + 0.85, w - 0.45, h - 0.95, t2, 12, color=MUTED)
        elif items:
            ncol = 2 if len(items) > 4 else 1
            w = (CW - 0.6 * (ncol - 1)) / ncol
            for c, chunk in enumerate(_chunks(items, ncol)):
                if chunk:
                    _txt(slide, ML + c * (w + 0.6), top, w, 6.8 - top, chunk, 20, color=SLATE, after=12,
                         marker="—  ", marker_color=TEAL)
        _ds_footer(slide, page, heading)

    # ---------------------------------------------------------------- bento
    GAP = 0.25

    def _bt_tile(slide, x, y, w, h, fill_hex=None, fill=None, label=None, title=None, text=None,
                 tsize=18, num=False):
        f = fill if fill is not None else (_rgb(fill_hex) if fill_hex else WHITE)
        _rrect(slide, x, y, w, h, f, radius=0.06)
        fg = _on(fill_hex) if fill_hex else SLATE
        sub = fg if fill_hex else MUTED
        yy = y + 0.25
        if label:
            _txt(slide, x + 0.3, yy, w - 0.6, 0.3, label, 10, bold=True, color=sub, spc=1)
            yy += 0.4
        if title:
            _txt(slide, x + 0.3, yy, w - 0.6, min(h - (yy - y) - 0.2, 1.8), title, tsize,
                 font=N_FONT if num else T_FONT, bold=True, color=(TEAL if num and not fill_hex else fg))
            per_line = max(1, int((w - 0.6) * 72 / (tsize * 0.95)))
            yy += 0.15 + tsize / 72 * 1.3 * min(3, -(-len(title) // per_line))
        if text:
            _txt(slide, x + 0.3, yy, w - 0.6, max(0.3, y + h - yy - 0.2), text, 12, color=sub, after=4)

    def _bt_header(slide, heading):
        _txt(slide, ML, 0.45, CW, 0.8, heading, 28, font=T_FONT, bold=True, color=SLATE)

    def _bt_footer(slide, page):
        _txt(slide, SW_IN - MR - 2.0, 7.1, 2.0, 0.25, f"{page} / {total}", 9, color=MUTED, align=PP_ALIGN.RIGHT)
        _txt(slide, ML, 7.1, 5.0, 0.25, "유비온", 9, bold=True, color=MUTED)

    def _bt_grid(slide, vals, top, layout):
        """Asymmetric bento: first tile spans two rows, rest fill a 2-col grid on the right."""
        vals = vals[:5]
        bottom = 6.9
        H = bottom - top
        if not vals:
            return
        if len(vals) == 1:
            t1, t2 = (_kv(vals[0]) if layout == "kpi" else _split_card(vals[0]))
            _bt_tile(slide, ML, top, CW, H, fill_hex=pal["primary"], title=t1, text=t2, tsize=40,
                     num=(layout == "kpi"))
            return
        bw = CW * 0.42
        rx = ML + bw + GAP
        rw = CW - bw - GAP
        rest = vals[1:]
        t1, t2 = (_kv(vals[0]) if layout == "kpi" else _split_card(vals[0]))
        _bt_tile(slide, ML, top, bw, H, fill_hex=pal["primary"], label="01", title=t1, text=t2,
                 tsize=60 if layout == "kpi" else 28, num=(layout == "kpi"))
        ncol = 2 if len(rest) > 1 else 1
        rows_n = (len(rest) + ncol - 1) // ncol
        th = (H - GAP * (rows_n - 1)) / rows_n
        for i, it in enumerate(rest):
            r, c = divmod(i, ncol)
            span = ncol == 2 and i == len(rest) - 1 and len(rest) % 2 == 1
            w = rw if span else (rw - GAP * (ncol - 1)) / ncol
            x = rx + c * ((rw - GAP) / ncol + GAP)
            a, b = (_kv(it) if layout == "kpi" else _split_card(it))
            alt = (i % 3 == 1)
            _bt_tile(slide, x, top + r * (th + GAP), w, th,
                     fill_hex=(pal["surface"] if alt else None), fill=(None if alt else WHITE),
                     label=f"{i + 2:02d}", title=a, text=b, tsize=34 if layout == "kpi" else 18,
                     num=(layout == "kpi"))

    def _bt_title(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        lw = CW * 0.62
        _bt_tile(slide, ML, 0.6, lw, 6.3, fill_hex=pal["primary"])
        _txt(slide, ML + 0.5, 1.0, lw - 1.0, 0.3, "유비온  ·  사내 안내", 12, bold=True,
             color=_on(pal["primary"]), spc=1)
        _txt(slide, ML + 0.5, 1.6, lw - 1.0, 3.0, heading or "Presentation", 44, font=T_FONT, bold=True,
             color=_on(pal["primary"]))
        if paras:
            _txt(slide, ML + 0.5, 5.0, lw - 1.0, 1.4, paras[:1], 15, color=_on(pal["primary"]))
        rx, rw = ML + lw + GAP, CW - lw - GAP
        side = (items or paras[1:])[:3]
        if not side:
            side = ["핵심 메시지를 한눈에"]
        th = (6.3 - GAP * (len(side) - 1)) / len(side)
        for i, it in enumerate(side):
            a, b = _kv(it) if any(ch.isdigit() for ch in it) else _split_card(it)
            alt = i % 2 == 0
            _bt_tile(slide, rx, 0.6 + i * (th + GAP), rw, th,
                     fill_hex=(pal["surface"] if alt else None), fill=(None if alt else WHITE),
                     label=f"{i + 1:02d}", title=a, text=b, tsize=28 if any(ch.isdigit() for ch in a) else 18,
                     num=any(ch.isdigit() for ch in a))
        _bt_footer(slide, page)

    def _bt_section(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        _bt_tile(slide, ML + 1.5, 1.4, CW - 3.0, 4.6, fill_hex=pal["surface"])
        _txt(slide, ML + 2.1, 2.0, CW - 4.2, 0.4, f"PART {page:02d}", 12, bold=True, color=TEAL, spc=2)
        _txt(slide, ML + 2.1, 2.55, CW - 4.2, 1.8, heading or "Section", 40, font=T_FONT, bold=True, color=SLATE)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML + 2.1, 4.5, CW - 4.2, 1.2, sub, 16, color=MUTED)
        _bt_footer(slide, page)

    def _bt_content(slide, heading, body, page, layout):
        _bg(slide)
        paras, items, tables = _collect(body)
        _bt_header(slide, heading)
        top = 1.45
        if tables:
            if paras:
                _txt(slide, ML, top, CW, 0.4, paras[0], 13, color=MUTED)
                top += 0.5
            for rows in tables:
                h = 0.4 * len(rows)
                _rrect(slide, ML, top, CW, h + 0.4, WHITE, radius=0.04)
                _table(slide, rows, ML + 0.2, top + 0.2, CW - 0.4, size=12, row_h=0.4, head_fill=WHITE,
                       head_color=TEAL, body_fill=WHITE, alt_fill=_rgb(pal["bg"]), font=B_FONT)
                top += h + 0.6
            _bt_footer(slide, page)
            return
        if layout in ("kpi", "cards", "tiles"):
            vals = items or paras
            if layout == "tiles" and items and paras:
                _txt(slide, ML, top, CW, 0.4, paras[0], 13, color=MUTED)
                top += 0.5
            if len(vals) > 5:
                ncol, rows_n = 3, 2
                tw = (CW - GAP * 2) / 3
                th = (6.9 - top - GAP) / 2
                for i, it in enumerate(vals[:6]):
                    r, c = divmod(i, 3)
                    a, b = _split_card(it)
                    big = i == 0
                    _bt_tile(slide, ML + c * (tw + GAP), top + r * (th + GAP), tw, th,
                             fill_hex=pal["primary"] if big else (pal["surface"] if (r + c) % 2 else None),
                             fill=None if big or (r + c) % 2 else WHITE, label=f"{i + 1:02d}", title=a, text=b,
                             tsize=22)
            else:
                _bt_grid(slide, vals, top, layout)
            _bt_footer(slide, page)
            return
        # content / two_column: text tile + bullet tiles
        vals = items
        if paras:
            pw = CW * 0.42 if vals else CW
            _bt_tile(slide, ML, top, pw, 6.9 - top, fill_hex=pal["surface"], label="SUMMARY")
            _txt(slide, ML + 0.3, top + 0.7, pw - 0.6, 6.9 - top - 0.9, paras[:3], 15, color=SLATE,
                 after=8, line_spacing=1.2)
            x0, w0 = ML + pw + GAP, CW - pw - GAP
        else:
            x0, w0 = ML, CW
        if vals:
            ncol = 2 if (len(vals) > 3 or not paras) and len(vals) > 1 else 1
            chunks = _chunks(vals, ncol)
            cw2 = (w0 - GAP * (ncol - 1)) / ncol
            for c, chunk in enumerate(chunks):
                if chunk:
                    x = x0 + c * (cw2 + GAP)
                    _rrect(slide, x, top, cw2, 6.9 - top, WHITE, radius=0.05)
                    _txt(slide, x + 0.3, top + 0.3, cw2 - 0.6, 6.9 - top - 0.5, chunk, 15, color=SLATE,
                         after=10, marker="●  ", marker_color=TEAL)
        _bt_footer(slide, page)

    # ---------------------------------------------------------------- manifesto
    MF_FILL_HEX = pal["bg"] if MODE == "brand_fill" else pal["primary"]
    MF_ON = _on(MF_FILL_HEX)

    def _chip_w(t):
        return min(4.2, 0.5 + 0.17 * min(len(t), 26))

    def _chips_fit(texts):
        return sum(_chip_w(t) + 0.15 for t in texts[:5]) <= CW + 0.15 and len(texts) <= 5

    def _mf_chips(slide, texts, y, fill_hex=None, outline=None, bg=None):
        x = ML
        for t in texts[:5]:
            t = t if len(t) <= 26 else t[:25] + "…"
            w = min(4.2, 0.5 + 0.17 * len(t))
            if x + w > SW_IN - MR:
                break
            if fill_hex:
                _rrect(slide, x, y, w, 0.5, _rgb(fill_hex), radius=0.5)
                fg = _on(fill_hex)
            else:
                _rrect(slide, x, y, w, 0.5, bg if bg is not None else SLIDE_BG, radius=0.5,
                           line=outline or SLATE, line_w=1.5)
                fg = outline or SLATE
            _txt(slide, x, y, w, 0.5, t, 12, bold=True, color=fg, align=PP_ALIGN.CENTER,
                 anchor=MSO_ANCHOR.MIDDLE)
            x += w + 0.15

    def _mf_title(slide, heading, body, page):
        _bg(slide, _rgb(MF_FILL_HEX))
        paras, items, _t = _collect(body)
        _txt(slide, ML, 0.5, CW, 0.3, "유비온 MANIFESTO", 12, bold=True, color=MF_ON, spc=4)
        words = (heading or "Presentation").split()
        per = max(1, (len(words) + 2) // 3)
        stacked = [" ".join(words[i:i + per]) for i in range(0, len(words), per)] or ["Presentation"]
        size = 80 if max(len(s) for s in stacked) <= 10 else 60
        _txt(slide, ML, 1.1, CW, 4.3, stacked, size, font=T_FONT, bold=True, color=MF_ON, line_spacing=0.9)
        chips = [s for s in (items or paras) if len(s) <= 40]
        if paras and len(paras[0]) > 40:
            _txt(slide, ML, 5.45, CW * 0.7, 0.6, paras[0], 15, color=MF_ON)
        _mf_chips(slide, chips, 6.3, outline=MF_ON, bg=_rgb(MF_FILL_HEX))

    def _mf_section(slide, heading, body, page):
        _bg(slide)
        paras, items, _t = _collect(body)
        _rect(slide, ML, 2.6, 0.9, 0.1, TEAL)
        _txt(slide, ML, 2.9, CW, 2.0, heading or "Section", 54, font=T_FONT, bold=True, color=SLATE)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML, 4.9, CW * 0.7, 1.0, sub, 16, color=MUTED)
        _txt(slide, SW_IN - MR - 2.0, 7.0, 2.0, 0.3, f"{page:02d}", 11, bold=True, color=SLATE,
             align=PP_ALIGN.RIGHT)

    def _mf_content(slide, heading, body, page, layout):
        _bg(slide)
        if CHROME == "top_rule":
            _rect(slide, 0, 0, SW_IN, 0.12, NAVY)
        paras, items, tables = _collect(body)
        _txt(slide, ML, 0.5, CW, 1.2, heading, 40, font=T_FONT, bold=True, color=SLATE)
        top = 1.75
        if paras:
            ph = _est_h(paras[0], 20, CW * 0.85, 1.35)
            _txt(slide, ML, top, CW * 0.85, ph, paras[0], 20, bold=True, color=TEAL, line_spacing=1.1)
            top += ph + 0.3
        for rows in tables:
            h = _table(slide, rows, ML, top, CW, size=13, row_h=0.42, head_fill=NAVY,
                       head_color=_on(pal["primary"]), body_fill=SLIDE_BG, alt_fill=CARD_BG, font=B_FONT)
            top += h + 0.2
        if layout == "kpi":
            vals = (items or paras)[:4]
            n = max(len(vals), 1)
            w = CW / n
            for i, it in enumerate(vals):
                v, lbl = _kv(it)
                _txt(slide, ML + i * w, top + 0.3, w - 0.3, 1.8, v, 72 if len(v) <= 4 else 48, font=N_FONT,
                     bold=True, color=NAVY if i % 2 == 0 else TEAL)
                _txt(slide, ML + i * w, top + 2.2, w - 0.3, 0.6, lbl, 15, bold=True, color=SLATE)
        elif layout in ("cards", "tiles"):
            vals = (items or paras[1:])[:6]
            ncol = 3 if len(vals) > 4 or layout == "tiles" else max(len(vals), 1)
            rows_n = max(1, (len(vals) + ncol - 1) // ncol)
            w = (CW - 0.2 * (ncol - 1)) / ncol
            h = min(2.4, (6.85 - top - 0.2 * (rows_n - 1)) / rows_n)
            for i, it in enumerate(vals):
                r, c = divmod(i, ncol)
                x, y = ML + c * (w + 0.2), top + r * (h + 0.2)
                fill_hex = pal["primary"] if i % 2 == 0 else pal["accent"]
                t1, t2 = _split_card(it)
                _rect(slide, x, y, w, h, _rgb(fill_hex))
                _txt(slide, x + 0.25, y + 0.2, w - 0.5, 0.9, t1, 22, font=T_FONT, bold=True, color=_on(fill_hex))
                if t2:
                    _txt(slide, x + 0.25, y + 1.0, w - 0.5, h - 1.1, t2, 12, color=_on(fill_hex))
        elif items:
            if all(len(s) <= 26 for s in items) and _chips_fit(items):
                _mf_chips(slide, items, top + 0.1, fill_hex=None, outline=SLATE)
            else:
                n = len(items)
                size = 26 if n <= 4 else 18
                _txt(slide, ML, top, CW, 6.85 - top, items[:8], size, font=T_FONT, bold=True, color=SLATE,
                     after=6, marker=lambda i: f"{i + 1:02d}  ", marker_color=TEAL)
        _txt(slide, SW_IN - MR - 2.0, 7.05, 2.0, 0.3, f"{page:02d}", 11, bold=True, color=SLATE,
             align=PP_ALIGN.RIGHT)

    # ---------------------------------------------------------------- pop
    POP_ON = _on(pal["primary"])

    def _pop_sticker(slide, x, y, w, h, title_t, body_t, i, size=20):
        fills = [pal["surface"], pal["accent"], "FFFFFF"]
        fh = fills[i % len(fills)]
        _rect(slide, x + 0.08, y + 0.08, w, h, INK)  # hard drop shadow
        s = _rrect(slide, x, y, w, h, _rgb(fh), radius=0.12, line=INK, line_w=2.5)
        tilt = (-2.5, 1.8, -1.2, 2.4, -1.8, 1.2)[i % 6]
        s.rotation = tilt
        tb = _txt(slide, x + 0.25, y + 0.2, w - 0.5, 0.9, title_t, size, font=T_FONT, bold=True, color=_on(fh))
        tb.rotation = tilt
        if body_t:
            bb = _txt(slide, x + 0.25, y + 0.2 + size / 72 * 1.6, w - 0.5, h - 0.4 - size / 72 * 1.6, body_t, 13,
                      color=_on(fh))
            bb.rotation = tilt

    def _pop_title(slide, heading, body, page):
        _bg(slide, NAVY)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 0.5, CW, 0.3, "유비온!!", 14, bold=True, color=POP_ON, spc=2)
        _txt(slide, ML, 1.0, CW * 0.68, 4.8, heading or "Presentation", 80, font=T_FONT, bold=True,
             color=POP_ON, line_spacing=0.9)
        side = (items or paras)[:3]
        for i, it in enumerate(side):
            a, b = _kv(it) if any(ch.isdigit() for ch in it) else _split_card(it)
            _pop_sticker(slide, ML + CW * 0.7, 0.9 + i * 1.95, CW * 0.3, 1.6, a, b, i,
                         size=30 if any(ch.isdigit() for ch in a) else 18)
        if paras and items:
            _txt(slide, ML, 6.2, CW * 0.65, 0.7, paras[0], 16, bold=True, color=POP_ON)

    def _pop_section(slide, heading, body, page):
        _bg(slide, TEAL)
        fg = _on(pal["accent"])
        paras, items, _t = _collect(body)
        _txt(slide, ML, 1.0, CW, 2.4, f"#{page:02d}", 120, font=T_FONT, bold=True, color=fg)
        _txt(slide, ML, 3.6, CW, 1.6, heading or "Section", 60, font=T_FONT, bold=True, color=fg)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML, 5.4, CW * 0.7, 1.0, sub, 18, bold=True, color=fg)

    def _pop_content(slide, heading, body, page, layout):
        _bg(slide)
        paras, items, tables = _collect(body)
        fg = _on(pal["bg"])
        _txt(slide, ML, 0.4, CW, 1.2, heading, 44, font=T_FONT, bold=True, color=fg)
        top = 1.7
        if paras:
            ph = _est_h(paras[0], 18, CW * 0.8)
            _txt(slide, ML, top, CW * 0.8, ph, paras[0], 18, bold=True, color=fg)
            top += ph + 0.3
        for rows in tables:
            _rect(slide, ML + 0.08, top + 0.08, CW, 0.42 * len(rows), INK)
            h = _table(slide, rows, ML, top, CW, size=13, row_h=0.42, head_fill=INK, head_color=WHITE,
                       body_fill=WHITE, alt_fill=_rgb(pal["surface"]), color=INK, font=B_FONT)
            top += h + 0.3
        vals = (items or (paras[1:] if layout in ("cards", "tiles", "kpi") else []))
        if len(vals) > 6 and layout not in ("cards", "tiles", "kpi"):
            for c, chunk in enumerate(_chunks(vals, 2)):
                if chunk:
                    _rrect(slide, ML + c * (CW / 2 + 0.1), top + 0.1, CW / 2 - 0.1, 6.8 - top - 0.1,
                           _rgb(pal["surface"]), radius=0.06, line=INK, line_w=2.5)
                    _txt(slide, ML + c * (CW / 2 + 0.1) + 0.3, top + 0.35, CW / 2 - 0.7, 6.4 - top, chunk, 20,
                         font=T_FONT, bold=True, color=_on(pal["surface"]), after=8, marker="★ ",
                         marker_color=_rgb(pal["dark_primary"]))
            vals = []
        vals = vals[:6]
        if vals:
            ncol = 3 if len(vals) > 4 else max(min(len(vals), 2 if layout == "content" else 4), 1)
            rows_n = (len(vals) + ncol - 1) // ncol
            w = (CW - 0.4 * (ncol - 1)) / ncol
            h = min(2.3, (6.8 - top - 0.35 * (rows_n - 1)) / rows_n)
            for i, it in enumerate(vals):
                r, c = divmod(i, ncol)
                if layout == "kpi":
                    a, b = _kv(it)
                    size = 40 if len(a) <= 6 else 26
                else:
                    a, b = _split_card(it)
                    size = 20
                _pop_sticker(slide, ML + c * (w + 0.4), top + 0.1 + r * (h + 0.35), w, h, a, b, i, size=size)
        _txt(slide, SW_IN - MR - 2.0, 7.05, 2.0, 0.3, f"{page}/{total}", 12, bold=True, color=fg,
             align=PP_ALIGN.RIGHT)

    # ---------------------------------------------------------------- minimal
    def _mn_chrome(slide, page, label=""):
        _bg(slide)
        _rect(slide, ML, 0.55, CW, 0.006, SLATE)
        _txt(slide, ML, 0.25, 6.0, 0.25, f"{page:02d}  —  {label[:30]}" if label else f"{page:02d}", 9,
             color=MUTED, spc=2)
        _txt(slide, SW_IN - MR - 3.0, 0.25, 3.0, 0.25, "유비온", 9, color=MUTED, align=PP_ALIGN.RIGHT, spc=2)

    def _mn_title(slide, heading, body, page):
        _mn_chrome(slide, page)
        paras, items, _t = _collect(body)
        _rrect(slide, ML, 2.35, 0.16, 0.16, TEAL, radius=0.5)
        _txt(slide, ML, 2.75, CW * 0.8, 2.2, heading or "Presentation", 54, font=T_FONT, color=SLATE,
             line_spacing=1.0)
        sub = (paras or items)[:1]
        if sub:
            _txt(slide, ML, 5.2, CW * 0.6, 0.8, sub, 16, color=MUTED)

    def _mn_section(slide, heading, body, page):
        _mn_chrome(slide, page)
        paras, items, _t = _collect(body)
        _txt(slide, ML, 2.8, 2.0, 0.4, f"{page:02d}", 14, color=TEAL)
        _txt(slide, ML, 3.2, CW * 0.8, 1.4, heading or "Section", 40, font=T_FONT, color=SLATE)
        sub = (paras + items)[:1]
        if sub:
            _txt(slide, ML, 4.6, CW * 0.6, 0.8, sub, 14, color=MUTED)

    def _mn_content(slide, heading, body, page, layout):
        _mn_chrome(slide, page, heading)
        paras, items, tables = _collect(body)
        _txt(slide, ML, 1.1, CW * 0.85, 1.0, heading, 36, font=T_FONT, color=SLATE)
        top = 2.4
        if paras:
            ph = _est_h(paras[0], 15, CW * 0.6, 1.6)
            _txt(slide, ML, top, CW * 0.6, ph, paras[0], 15, color=MUTED, line_spacing=1.3)
            top += ph + 0.35
        for rows in tables:
            h = _table(slide, rows, ML, top, CW, size=12, row_h=0.42, head_fill=SLIDE_BG, head_color=MUTED,
                       body_fill=SLIDE_BG, color=SLATE, font=B_FONT)
            _rect(slide, ML, top + 0.42, CW, 0.006, SLATE)
            top += h + 0.3
        vals = items or (paras[1:] if layout in ("cards", "tiles", "kpi") else [])
        if layout == "kpi" and vals:
            vals = vals[:4]
            w = CW / len(vals)
            for i, it in enumerate(vals):
                v, lbl = _kv(it)
                _txt(slide, ML + i * w, top + 0.4, w - 0.4, 1.4, v, 54 if len(v) <= 5 else 36, font=N_FONT,
                     color=TEAL)
                _txt(slide, ML + i * w, top + 1.9, w - 0.4, 0.6, lbl, 12, color=MUTED, spc=1)
        elif layout in ("cards", "tiles") and vals:
            vals = vals[:6]
            ncol = 3
            w = (CW - 0.6 * (ncol - 1)) / ncol
            for i, it in enumerate(vals):
                r, c = divmod(i, ncol)
                x, y = ML + c * (w + 0.6), top + r * 1.9
                t1, t2 = _split_card(it)
                _rrect(slide, x, y + 0.12, 0.1, 0.1, TEAL, radius=0.5)
                _txt(slide, x + 0.3, y, w - 0.3, 0.5, t1, 18, color=SLATE)
                if t2:
                    _txt(slide, x + 0.3, y + 0.55, w - 0.3, 1.1, t2, 12, color=MUTED)
        elif vals:
            sparse = vals[:5]
            ncol = 2 if len(vals) > 5 or layout == "two_column" else 1
            if ncol == 2:
                sparse = vals[:8]
            w = (CW * (0.9 if ncol == 2 else 0.65) - 0.8 * (ncol - 1)) / ncol
            for c, chunk in enumerate(_chunks(sparse, ncol)):
                if chunk:
                    _txt(slide, ML + c * (w + 0.8), top, w, 6.9 - top, chunk, 18, color=SLATE, after=18)
        _txt(slide, SW_IN - MR - 2.0, 7.05, 2.0, 0.25, f"{page} / {total}", 9, color=MUTED,
             align=PP_ALIGN.RIGHT)

    _FAMILY_RENDERERS = {
        "magazine": (_mag_title, _mag_section, _mag_content),
        "research": (_res_title, _res_section, _res_content),
        "consulting": (_con_title, _con_section, _con_content),
        "billboard": (_bb_title, _bb_section, _bb_content),
        "dark_stage": (_ds_title, _ds_section, _ds_content),
        "bento": (_bt_title, _bt_section, _bt_content),
        "manifesto": (_mf_title, _mf_section, _mf_content),
        "pop": (_pop_title, _pop_section, _pop_content),
        "minimal": (_mn_title, _mn_section, _mn_content),
    }
    fam_r = _FAMILY_RENDERERS.get(FAMILY)

    for idx, spec in enumerate(slides_spec):
        page = idx + 1
        is_first = idx == 0
        if spec.get("force_title"):
            layout = "title"
        else:
            layout = _infer_layout(spec.get("layout"), is_first, spec["body"])
            if layout == "title" and idx != 0:
                layout = "content"
        slide = prs.slides.add_slide(blank)
        if fam_r is not None:
            if layout == "title":
                fam_r[0](slide, spec["heading"], spec["body"], page)
            elif layout == "section":
                fam_r[1](slide, spec["heading"], spec["body"], page)
            else:
                fam_r[2](slide, spec["heading"], spec["body"], page, layout)
        elif layout == "title":
            _render_title(slide, spec["heading"], spec["body"], page)
        elif layout == "section":
            _render_section(slide, spec["heading"], spec["body"], page)
        else:
            _render_content(slide, spec["heading"], spec["body"], page, layout)

    # overflow QA (SESSION_REV=2026-10-25-huashu-overflow-qa): fix in memory, then save.
    try:
        overflow_report = _pptx_fix_text_overflow(prs)
    except Exception as e:  # QA must never block delivery of the deck itself
        logger.warning("daou-workspace: pptx overflow QA failed: %s", e)
        overflow_report = {"fixed": 0, "truncated": 0, "grown": 0, "still_overflow": 0, "checked": 0,
                           "details": [], "error": f"{type(e).__name__}: {e}"}
    if not isinstance(overflow_report, dict):
        overflow_report = {"fixed": 0, "truncated": 0, "grown": 0, "still_overflow": 0, "checked": 0, "details": []}
    overflow_report["brand_id"] = pal.get("brand_id")
    overflow_report["theme"] = theme_slug
    overflow_report["style"] = style_id or pal.get("style_id")
    overflow_report["layout_family"] = pal.get("family") or style_family
    overflow_report["seed"] = _ubion_seed_used
    overflow_report["master"] = bool(pal.get("master"))
    prs.save(path)
    return overflow_report



_WRITERS = {"docx": _write_docx, "hwpx": _write_hwpx, "pdf": _write_pdf, "pptx": _write_pptx}


def make_document(params: dict, **_kw) -> str:
    fmt = params.get("format")
    path = _resolve(params.get("path"))
    blocks = [b for b in params.get("blocks") or [] if isinstance(b, dict)]
    compact, pages = _want_one_page(params)
    if fmt not in DOC_FORMATS:
        return json.dumps({"success": False, "error": f"format must be one of {sorted(DOC_FORMATS)}"})
    # Re-checked here too: the pre_tool_call guard already confined it to this room's folder.
    if path is None or not _under(path, WORK_ROOT) or path == WORK_ROOT or os.path.dirname(path) == WORK_ROOT:
        return json.dumps({"success": False, "error": f"path must be absolute and inside {WORK_ROOT}/<room>/"})
    if not path.lower().endswith("." + fmt):
        return json.dumps({"success": False, "error": f"path must end with .{fmt}"})
    page_count = None
    overflow_report = None
    page_qc = "content_budget" if (compact and fmt == "docx") else "none"
    try:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        use_blocks = _budget_blocks_one_page(blocks) if compact and fmt == "docx" else blocks
        if fmt == "docx":
            _write_docx(path, str(params.get("title") or ""), use_blocks, compact=compact)
            if compact:
                page_count = _count_docx_pages(path)
                if page_count is None:
                    page_qc = "content_budget_no_soffice"
                elif page_count > 1:
                    # Regenerate with more aggressive compact trim
                    use_blocks = _budget_blocks_one_page(blocks, aggressive=True)
                    _write_docx(path, str(params.get("title") or ""), use_blocks, compact=True)
                    page_count = _count_docx_pages(path) or page_count
                    page_qc = "soffice_retry_aggressive"
                    if page_count and page_count > 1:
                        # Last resort: keep only first intro + first table + <=2 bullets
                        ultra = []
                        for b in use_blocks:
                            if b.get("type") == "paragraph" and not any(x.get("type") == "paragraph" for x in ultra):
                                ultra.append(b)
                            elif b.get("type") == "table" and not any(x.get("type") == "table" for x in ultra):
                                rows = (b.get("rows") or [])[:4]
                                ultra.append({"type": "table", "rows": rows})
                        for b in use_blocks:
                            if b.get("type") == "bullets":
                                ultra.append({"type": "bullets", "items": (b.get("items") or [])[:2]})
                                break
                        _write_docx(path, str(params.get("title") or ""), ultra, compact=True)
                        page_count = _count_docx_pages(path) or page_count
                        page_qc = "soffice_retry_ultra"
                else:
                    page_qc = "soffice_ok"
        else:
            if fmt == "pptx":
                # Defense in depth: even if pre_tool_call missed, pull theme/style/extras from
                # pending stash / path room / history.
                forced = _inject_theme_into_args(params, "")
                forced_style = _inject_style_into_args(params, "")
                _inject_slide_extras_into_args(params, "")
                room_key = f"daou_{os.path.basename(os.path.dirname(path))}_x"
                if forced:
                    _read_pending_theme(room_key, consume=True)
                if forced_style:
                    _read_pending_style(room_key, consume=True)
                _read_pending_slide_kv(room_key, consume=True)
                ppt_blocks = use_blocks if compact else blocks
                engine = _effective_ppt_engine(params, "")
                overflow_report = None
                if engine == "quick":
                    # Hook A: Quick sidecar (Mac primary → 185 failover). Fail → legacy.
                    # Note: Huashu specimen themes already forced to legacy via _effective_ppt_engine.
                    pages_arg = pages
                    if pages_arg is None:
                        try:
                            pages_arg = int(params["pages"]) if params.get("pages") is not None else None
                        except (TypeError, ValueError):
                            pages_arg = None
                    overflow_report = _quick_sidecar(
                        path,
                        str(params.get("title") or ""),
                        ppt_blocks,
                        theme=params.get("theme"),
                        style=params.get("style"),  # pass-through; no _PPT_STYLE_FAMILY
                        layout=params.get("layout"),
                        canvas=params.get("canvas"),
                        pages=pages_arg,
                        deck=params.get("deck"),
                        session_id="",
                    )
                    _fb_reason = None
                    _fb_job = None
                    _fb_hop = None
                    if isinstance(overflow_report, dict) and overflow_report.get("quick_fallback"):
                        _fb_reason = overflow_report.get("quick_fallback_reason")
                        _fb_job = overflow_report.get("quick_job")
                        _fb_hop = overflow_report.get("hop")
                        overflow_report = None
                    if overflow_report is None:
                        logger.info(
                            "daou-workspace: quick sidecar miss → legacy _write_pptx (%s)",
                            _fb_reason or "None",
                        )
                        overflow_report = _write_pptx(
                            path,
                            str(params.get("title") or ""),
                            ppt_blocks,
                            theme=params.get("theme"),
                            style=params.get("style"),
                        )
                        if isinstance(overflow_report, dict):
                            overflow_report["engine"] = "legacy"
                            overflow_report["quick_fallback"] = True
                            if _fb_reason:
                                overflow_report["quick_fallback_reason"] = _fb_reason
                            if _fb_job:
                                overflow_report["quick_job"] = _fb_job
                            if _fb_hop:
                                overflow_report["hop"] = _fb_hop
                    # else: quick success; skip legacy family renderer
                else:
                    overflow_report = _write_pptx(
                        path,
                        str(params.get("title") or ""),
                        ppt_blocks,
                        theme=params.get("theme"),
                        style=params.get("style"),
                    )
                    if isinstance(overflow_report, dict):
                        overflow_report["engine"] = "legacy"
                        if _is_huashu_specimen_theme(params.get("theme")):
                            overflow_report["huashu_forced_legacy"] = True
                            overflow_report["huashu_force_reason"] = (
                                "huashu specimen slug → legacy DNA "
                                f"(SESSION_REV={SESSION_REV})"
                            )
            else:
                _WRITERS[fmt](path, str(params.get("title") or ""), use_blocks if compact else blocks)
        size = os.path.getsize(path)
        if size > MAX_DOC_BYTES:
            os.unlink(path)
            return json.dumps({"success": False, "error": "document too large"})
        os.chmod(path, 0o644)
    except Exception as e:
        logger.warning("daou-workspace: %s generation failed: %s", fmt, e)
        return json.dumps({"success": False, "error": f"{fmt} 생성 실패: {type(e).__name__}: {e}"})
    result = {"success": True, "path": path, "bytes": size, "compact": compact}
    if fmt == "pptx":
        eng = "legacy"
        if isinstance(overflow_report, dict) and overflow_report.get("engine"):
            eng = str(overflow_report.get("engine"))
        else:
            eng = _effective_ppt_engine(params, "")
        result["engine"] = eng
        if isinstance(overflow_report, dict):
            if overflow_report.get("quick_job"):
                result["quick_job"] = overflow_report.get("quick_job")
            if overflow_report.get("hop"):
                result["hop"] = overflow_report.get("hop")
            if overflow_report.get("quick_fallback"):
                result["quick_fallback"] = True
            if overflow_report.get("quick_fallback_reason"):
                result["quick_fallback_reason"] = overflow_report.get("quick_fallback_reason")
            if overflow_report.get("huashu_forced_legacy"):
                result["huashu_forced_legacy"] = True
                result["huashu_force_reason"] = overflow_report.get("huashu_force_reason")
            if overflow_report.get("layout_wired"):
                result["layout"] = overflow_report.get("layout_wired")
            if overflow_report.get("style_recipe"):
                result["style_recipe"] = overflow_report.get("style_recipe")
        theme_slug, theme_pal = _resolve_pptx_theme(params.get("theme"))
        if eng == "quick":
            # Quick path: report wire theme/style/layout/canvas as-is (no family remap).
            raw_theme = (
                str((overflow_report or {}).get("theme_wired") or "").strip()
                or str(params.get("theme") or "").strip()
                or theme_slug
            )
            result["theme"] = raw_theme
            if params.get("style"):
                result["style"] = str(params.get("style"))
            if params.get("layout"):
                result["layout"] = str(params.get("layout"))
            if params.get("canvas"):
                result["canvas"] = str(params.get("canvas"))
            result["brand_id"] = raw_theme
            if theme_slug != raw_theme:
                result["theme_resolved_local"] = theme_slug  # LMS palette id only; Mac used wire theme
        else:
            theme_pal, style_id, style_family = _apply_style_to_palette(theme_pal, params.get("style"))
            result["layout_family"] = theme_pal.get("family", "corporate")
            if style_id:
                result["style"] = style_id
            # brand-id-report: report requested/stashed theme, not local palette fallback.
            _req_theme = _normalize_theme_slug(params.get("theme")) or ""
            _local_bid = theme_pal.get("brand_id") or theme_slug
            if _req_theme:
                result["theme"] = _req_theme
                result["brand_id"] = _req_theme
                if _local_bid and _local_bid != _req_theme:
                    result["brand_id_resolved_local"] = _local_bid
            elif _local_bid:
                result["brand_id"] = _local_bid
            if theme_pal.get("master"):
                result["master"] = True
            raw_theme = str(params.get("theme") or "").strip()
            if raw_theme and theme_slug == "ubion" and raw_theme not in (
                "ubion", "ubion-corporate", "corporate", "navy",
            ):
                result["theme_warning"] = f"unknown theme {raw_theme!r}; fell back to ubion"
            raw_style = str(params.get("style") or "").strip()
            if raw_style and not style_id:
                result["style_warning"] = f"unknown style {raw_style!r}; family=corporate"
            if isinstance(overflow_report, dict):
                if overflow_report.get("seed"):
                    result["seed"] = overflow_report.get("seed")
                qa = {k: overflow_report.get(k, 0) for k in ("fixed", "truncated", "still_overflow", "grown", "checked")}
                qa["rev"] = "overflow-qa"
                if overflow_report.get("details"):
                    qa["details"] = overflow_report["details"][:12]
                if overflow_report.get("error"):
                    qa["error"] = overflow_report["error"]
                result["overflow_qa"] = qa
    if pages is not None:
        result["pages"] = pages
    if page_count is not None:
        result["page_count"] = page_count
    if compact:
        result["page_qc"] = page_qc
    return json.dumps(result, ensure_ascii=False)


def register(ctx):
    os.makedirs(WORK_ROOT, mode=0o755, exist_ok=True)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_tool(name="daou_make_document", toolset="daou_docs", schema=SCHEMA, handler=make_document, emoji="📄")
