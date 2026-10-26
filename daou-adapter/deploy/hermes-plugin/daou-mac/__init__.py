"""daou-mac: hand content-production jobs from the 유비온봇 (daou profile) to a content worker.

Primary hop: company Mac Studio (Tailscale). Automatic failover: VPS 185 when Mac is
unhealthy (SSH/API). Each hop speaks the same Hermes-style API (Bearer /etc/hermes-mac/api.key,
POST/GET /v1/runs) and stores work under that hop's MAC_WORK/jobs/<job>/.

Flow per job:
1. Health-check Mac (short SSH `true` + optional API). If unhealthy → hop=vps185.
2. (optional) scp room inputs → hop ``jobs/<job>/in/``
3. POST /v1/runs, poll GET /v1/runs/<id> (pending approvals denied)
4. scp ``jobs/<job>/out/*`` → ``/var/lib/hermes-daou-work/<room>/mac-<job>/``

JSON responses include ``hop``: ``mac`` | ``vps185``. Env overrides: DAOU_MAC_HOST,
DAOU_MAC_API, DAOU_MAC_WORK, DAOU_FALLBACK_HOST, DAOU_FALLBACK_API, DAOU_FALLBACK_WORK,
DAOU_FORCE_HOP, DAOU_HEALTH_TIMEOUT.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

WORK_ROOT = os.path.realpath(os.environ.get("DAOU_WORK_ROOT", "/var/lib/hermes-daou-work"))
_SESSION_RE = re.compile(r"^daou_([A-Za-z0-9_-]+)_[a-z0-9]+$")
KEY_DIR = "/etc/hermes-mac"
STATE_DIR = os.path.join(WORK_ROOT, ".mac-jobs")
MAX_JOBS = 2
MAX_INPUT_BYTES = 200 * 1024 * 1024
DEFAULT_WAIT, MAX_WAIT = 90, 900
_JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SSH_BASE = ["-i", f"{KEY_DIR}/id_ed25519", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
HEALTH_TIMEOUT = float(os.environ.get("DAOU_HEALTH_TIMEOUT", "5"))

HOPS = {
    "mac": {
        "host": os.environ.get("DAOU_MAC_HOST", ""),
        "api": os.environ.get("DAOU_MAC_API", ""),
        "work": os.environ.get("DAOU_MAC_WORK", ""),
    },
    "vps185": {
        "host": os.environ.get("DAOU_FALLBACK_HOST", ""),
        "api": os.environ.get("DAOU_FALLBACK_API", "http://127.0.0.1:18650"),
        "work": os.environ.get("DAOU_FALLBACK_WORK", "/root/ubion-bot-work"),
    },
}
PRIMARY = "mac"
FALLBACK = "vps185"


def _room(session_id: str) -> tuple[str | None, str | None]:
    m = _SESSION_RE.match(session_id or "")
    return (m.group(1), os.path.join(WORK_ROOT, m.group(1))) if m else (None, None)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _key() -> str:
    with open(f"{KEY_DIR}/api.key") as f:
        return f.read().strip()


def _hop_cfg(hop: str) -> dict:
    if hop not in HOPS:
        raise ValueError(f"unknown hop: {hop}")
    return HOPS[hop]


def _api(hop: str, method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    api = _hop_cfg(hop)["api"]
    req = urllib.request.Request(
        api + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _ssh(hop: str, cmd: str, timeout: int = 60) -> str:
    host = _hop_cfg(hop)["host"]
    r = subprocess.run(["ssh", "-n", *_SSH_BASE, host, cmd], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"ssh 실패 ({r.returncode}): {r.stderr.strip()[-300:]}")
    return r.stdout


def _scp(hop: str, src: list[str], dst: str, timeout: int = 600) -> None:
    r = subprocess.run(["scp", "-q", "-p", *_SSH_BASE, *src, dst], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"scp 실패: {r.stderr.strip()[-300:]}")


def _scp_remote(hop: str, remote_path: str) -> str:
    """host:path for list-argv scp. Never shlex.quote — OpenSSH 9+ SFTP treats quotes as literal
    (broke Korean/CJK out/ filenames → bg-follow empty). UTF-8 path bytes are fine."""
    if any(ord(c) < 32 for c in remote_path):
        raise ValueError("unsafe remote path")
    return f"{_hop_cfg(hop)['host']}:{remote_path}"


def _health_check(hop: str) -> bool:
    """True when SSH works and (if reachable) API answers without auth failure on /health or /v1/models."""
    cfg = _hop_cfg(hop)
    try:
        r = subprocess.run(
            ["ssh", "-n", *_SSH_BASE, "-o", f"ConnectTimeout={max(1, int(HEALTH_TIMEOUT))}", cfg["host"], "true"],
            capture_output=True, text=True, timeout=HEALTH_TIMEOUT + 2,
        )
        if r.returncode != 0:
            logger.info("daou-mac: hop %s ssh unhealthy: %s", hop, (r.stderr or "")[-200:])
            return False
    except Exception as e:
        logger.info("daou-mac: hop %s ssh unhealthy: %s", hop, e)
        return False
    # API: prefer /health (185 worker); Mac may 404 — then try /v1/models with bearer
    for path in ("/health", "/v1/health", "/v1/models"):
        try:
            req = urllib.request.Request(
                cfg["api"] + path,
                method="GET",
                headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                logger.info("daou-mac: hop %s api auth fail on %s", hop, path)
                return False
            if e.code == 404:
                continue
            # other HTTP still means API process is up
            return True
        except Exception:
            continue
    # SSH ok but API unreachable — treat as unhealthy so we can fail over (Mac FileVault case: SSH also fails)
    logger.info("daou-mac: hop %s api unreachable after ssh ok", hop)
    return False


def _select_hop() -> str:
    force = (os.environ.get("DAOU_FORCE_HOP") or "").strip()
    if force in HOPS:
        logger.info("daou-mac: forced hop=%s", force)
        return force
    if _health_check(PRIMARY):
        logger.info("daou-mac: selected hop=%s (primary healthy)", PRIMARY)
        return PRIMARY
    logger.warning("daou-mac: primary %s unhealthy → failover %s", PRIMARY, FALLBACK)
    if _health_check(FALLBACK):
        logger.info("daou-mac: selected hop=%s (failover)", FALLBACK)
        return FALLBACK
    # last resort: still return fallback so start errors surface from 185
    logger.error("daou-mac: both hops unhealthy; using %s", FALLBACK)
    return FALLBACK


class _Slot:
    def __enter__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        for i in range(MAX_JOBS):
            f = open(os.path.join(STATE_DIR, f"slot.{i}.lock"), "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.f = f
                return self
            except BlockingIOError:
                f.close()
        self.f = None
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()


def _state_path(job: str) -> str:
    return os.path.join(STATE_DIR, f"{job}.json")


def _save(job: str, data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_state_path(job), "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _load(job: str) -> dict | None:
    try:
        with open(_state_path(job)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


_JOB_LOCKS: dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()
_BG_FOLLOWING: set[str] = set()
BG_FOLLOW_WAIT = 1500


def _job_lock(job: str) -> threading.Lock:
    with _JOB_LOCKS_GUARD:
        lk = _JOB_LOCKS.get(job)
        if lk is None:
            lk = threading.Lock()
            _JOB_LOCKS[job] = lk
        return lk


def _ensure_bg_follow(job: str) -> None:
    with _JOB_LOCKS_GUARD:
        if job in _BG_FOLLOWING:
            return
        _BG_FOLLOWING.add(job)

    def _run() -> None:
        try:
            st = _load(job)
            if not st or st.get("status") != "running":
                return
            with _job_lock(job):
                st = _load(job) or st
                if st.get("status") != "running":
                    return
                logger.info("daou-mac: bg-follow start %s hop=%s", job, st.get("hop"))
                _wait(job, st, BG_FOLLOW_WAIT)
                logger.info("daou-mac: bg-follow done %s status=%s", job, (_load(job) or {}).get("status"))
        except Exception as e:
            logger.warning("daou-mac: bg-follow %s failed: %s", job, e)
        finally:
            with _JOB_LOCKS_GUARD:
                _BG_FOLLOWING.discard(job)

    threading.Thread(target=_run, name=f"daou-mac-follow-{job}", daemon=True).start()


def _prompt(request: str, job: str, inputs: list[str], hop: str) -> str:
    work = _hop_cfg(hop)["work"]
    jd = f"{work}/jobs/{job}"
    lines = [request.strip(), "", f"[작업 폴더] {jd} — 최종 결과 파일은 {jd}/out/ 에만, 중간 파일은 {jd}/work/ 에."]
    if inputs:
        lines.append("[입력 파일] " + ", ".join(f"{jd}/in/{os.path.basename(p)}" for p in inputs))
    if hop == "vps185":
        lines.append(f"[환경] LEMO_OPUSCAR_HOME={os.environ.get('DAOU_FALLBACK_LEMO', '/root/grokbot-video/lemo-opuscar')} (VPS185 failover hop)")
        lines.append("[환경] VOX=/opt/voxcpm2/run.sh REF=/opt/voxcpm2/scott_ref6s.wav (로컬 VoxCPM2; edge-tts 금지)")
    # HARD policy injected every content job (2026-10-25-vox-default-1min)
    lines.append(
        "[TTS HARD] 한국어 사내·나레이션·영상 VO는 VoxCPM2만. "
        "호스트에 /opt/voxcpm2/run.sh 있으면 로컬(또는 tools/tts_vox.sh). "
        "없으면 VOX_HOST=<fallback-host> 로 hop. "
        "edge-tts/Edge Neural 사내·상업 금지. MiniMax는 키 있을 때만(키 값 출력 금지)."
    )
    req_l = (request or "").lower()
    # duration override if user already named a length in the request
    has_len = bool(re.search(
        r"(\d+\s*(분|초|min|mins|minute|minutes|sec|secs|second|seconds)|\d+\s*m\b|≤\s*\d+|<=\s*\d+|target\s*≤|target\s*<=)",
        request or "",
        re.I,
    ))
    if not has_len and any(k in req_l for k in ("영상", "video", "mp4", "나레이션", "숏폼", "화이트보드", "lemo", "news-package", "briefing")):
        lines.append("[LENGTH HARD] 기본 완본 ≤60초. 대본·장면을 맞춰 자른다. 요청에 분·초가 없으면 60초를 넘기지 말 것.")
    lines.append("맞는 스킬을 skill_view 로 읽고 그 스크립트로 만들어. 마지막 답은 결과 파일 절대 경로와 한두 줄 요약(길이·내용)만.")
    return "\n".join(lines)


def _wait(job: str, st: dict, wait: int) -> dict:
    hop = st.get("hop") or PRIMARY
    work = _hop_cfg(hop)["work"]
    host = _hop_cfg(hop)["host"]
    deadline = time.time() + wait
    while True:
        try:
            run = _api(hop, "GET", f"/v1/runs/{st['run_id']}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                st["status"] = "lost"
                _save(job, st)
                return {"job_id": job, "status": "lost", "hop": hop,
                        "error": "콘텐츠 게이트웨이가 재시작돼 작업 상태를 잃었어요. 다시 요청해 주세요."}
            raise
        status = run.get("status")
        if status == "waiting_for_approval":
            try:
                _api(hop, "POST", f"/v1/runs/{st['run_id']}/approval", {"choice": "deny"})
            except Exception as e:
                logger.info("daou-mac: approval deny for %s: %s", job, e)
        elif status in ("completed", "failed", "cancelled"):
            break
        # Early complete when Quick pptx already in out/ (agent may still be hung on soffice QC)
        if status == "running":
            out_mac_poll = f"{work}/jobs/{job}/out"
            try:
                poll_names = [
                    n for n in _ssh(
                        hop,
                        f"cd {shlex.quote(out_mac_poll)} 2>/dev/null && "
                        f"find . -maxdepth 2 -type f -name '*.pptx' | head -5",
                    ).split("\n")
                    if n.strip()
                ]
            except Exception:
                poll_names = []
            if poll_names:
                try:
                    rel = poll_names[0][2:] if poll_names[0].startswith("./") else poll_names[0].lstrip("/")
                    pptx_path = f"{out_mac_poll}/{rel}"
                    age = _ssh(
                        hop,
                        "stat -f %m " + shlex.quote(pptx_path) + " 2>/dev/null || stat -c %Y " + shlex.quote(pptx_path),
                    ).strip()
                    import time as _tmod
                    age_i = int(_tmod.time() - int(age)) if age.isdigit() else 99
                except Exception:
                    age_i = 99
                if age_i >= 12:
                    logger.info(
                        "daou-mac: out pptx ready age=%ss (%s) → complete job=%s while run=%s",
                        age_i, poll_names, job, status,
                    )
                    status = "completed"
                    run = dict(run or {})
                    run["status"] = "completed"
                    run["output"] = (run.get("output") or "") + " [early_harvest_pptx_poll]"
                    break
        if time.time() > deadline:
            # SESSION_REV=2026-10-26-ab-parity-fixes-l2: if out/ already has .pptx
            # (agent hung on LibreOffice PDF QC etc.), harvest and complete instead of
            # returning running → legacy fallback that drops the good Quick deck.
            out_mac_early = f"{work}/jobs/{job}/out"
            try:
                early = [
                    n for n in _ssh(
                        hop,
                        f"cd {shlex.quote(out_mac_early)} 2>/dev/null && "
                        f"find . -maxdepth 2 -type f -name '*.pptx' | head -10",
                    ).split("\n")
                    if n.strip()
                ]
            except Exception:
                early = []
            if early:
                logger.info(
                    "daou-mac: wait deadline but out has pptx (%s) → early harvest job=%s",
                    early, job,
                )
                status = "completed"
                run = dict(run or {})
                run["status"] = "completed"
                run["output"] = (run.get("output") or "") + " [early_harvest_pptx]"
                break
            return {"job_id": job, "status": "running", "hop": hop,
                    "message": f"아직 만드는 중이에요(영상은 수~십수 분 걸릴 수 있어요). 짧게 「만들고 있어요」만 남기고 턴을 끝내세요. 어댑터가 끝나면 파일을 방에 올립니다. 같은 턴에서 daou_mac_status를 반복 호출하지 마세요(답변이 끊깁니다)."}
        time.sleep(4)
    if status != "completed":
        st["status"] = status
        _save(job, st)
        return {"job_id": job, "status": status, "hop": hop,
                "error": str(run.get("error") or "콘텐츠 작업이 실패했어요.")[:800]}
    out_mac = f"{work}/jobs/{job}/out"
    try:
        names = [n for n in _ssh(hop, f"cd {shlex.quote(out_mac)} 2>/dev/null && find . -maxdepth 2 -type f ! -name '.*' | head -20").split("\n") if n.strip()]
    except Exception as e:
        st.update(status="failed", files=[], error=f"list_out:{e}")
        _save(job, st)
        return {"job_id": job, "status": "failed", "hop": hop, "error": f"결과 목록 실패: {e}"[:500]}
    dest = os.path.join(st["room_dir"], f"mac-{job}")
    os.makedirs(dest, exist_ok=True)
    files = []
    summary = str(run.get("output") or "").replace(out_mac + "/", "")
    if names:
        # n from find is "./name"; strip ./ — do not shlex.quote (SFTP literal quotes break CJK)
        try:
            remotes = []
            for n in names:
                rel = n[2:] if n.startswith("./") else n.lstrip("/")
                remotes.append(_scp_remote(hop, f"{out_mac}/{rel}"))
            _scp(hop, remotes, dest + "/")
            for n in names:
                p = os.path.realpath(os.path.join(dest, os.path.basename(n)))
                if _under(p, dest) and os.path.isfile(p):
                    files.append({"path": p, "bytes": os.path.getsize(p)})
        except Exception as e:
            st.update(status="failed", files=[], error=f"scp:{e}")
            _save(job, st)
            return {
                "job_id": job, "status": "failed", "hop": hop,
                "error": f"결과 복사 실패: {e}"[:500],
                "mac_summary": summary[:1500],
            }

    if not files:
        st.update(status="failed", files=[], error="empty_out")
        _save(job, st)
        return {
            "job_id": job, "status": "failed", "hop": hop,
            "error": "out/ 에 결과 파일이 없어요(길이·QC·승인대기 등으로 렌더 미완료). 목표 길이를 늘리거나 원고를 줄여 다시 요청하세요.",
            "mac_summary": summary[:1500],
        }
    st.update(status="completed", files=[f["path"] for f in files])
    _save(job, st)
    res = {"job_id": job, "status": "completed", "hop": hop, "files": files, "mac_summary": summary[:1500]}
    if any(f["bytes"] > 20 * 1024 * 1024 for f in files):
        res["note"] = "20MB가 넘는 파일은 메신저로 첨부되지 않아요 — 더 짧거나 낮은 화질로 다시 만들도록 요청하세요."
    res["how_to_deliver"] = "답변에 files 의 절대 경로를 그대로 적으면 그 파일이 방에 첨부돼요."
    return res


def _start_on_hop(hop: str, job: str, request: str, inputs: list[str], room: str, room_dir: str, wait: int) -> dict:
    work = _hop_cfg(hop)["work"]
    host = _hop_cfg(hop)["host"]
    jd = f"{work}/jobs/{job}"
    _ssh(hop, f"mkdir -p {shlex.quote(jd)}/in {shlex.quote(jd)}/out {shlex.quote(jd)}/work")
    if inputs:
        _scp(hop, inputs, f"{host}:{jd}/in/")
    run = _api(hop, "POST", "/v1/runs", {"input": _prompt(request, job, inputs, hop), "session_id": f"daou-{job}"})
    st = {
        "run_id": run["run_id"], "room": room, "room_dir": room_dir,
        "created": time.time(), "status": "running", "hop": hop,
    }
    _save(job, st)
    with _job_lock(job):
        res = _wait(job, st, wait)
    if res.get("status") == "running":
        _ensure_bg_follow(job)
    res["hop"] = hop
    return res


def mac_content(params: dict, session_id: str = "", **_kw) -> str:
    room, room_dir = _room(session_id)
    if not room:
        return _err("방 작업 폴더를 알 수 없어요.")
    request = str(params.get("request") or "").strip()
    if len(request) < 4:
        return _err("request 에 만들 내용을 구체적으로 적어 주세요.")
    inputs = []
    for raw in params.get("inputs") or []:
        p = os.path.realpath(str(raw))
        if not _under(p, room_dir) or not os.path.isfile(p) or os.path.getsize(p) > MAX_INPUT_BYTES:
            return _err(f"입력 파일은 이 방 작업 폴더({room_dir}/) 안의 200MB 이하 파일이어야 해요: {raw}")
        inputs.append(p)
    wait = max(30, min(int(params.get("wait_seconds") or DEFAULT_WAIT), MAX_WAIT))
    with _Slot() as slot:
        if slot.f is None:
            return _err(f"콘텐츠 작업이 이미 {MAX_JOBS}개 진행 중이에요(공용이라 동시 {MAX_JOBS}개까지). 잠시 뒤 다시 시도하세요.")
        job = f"{room}-{datetime.now().strftime('%m%d%H%M%S')}"
        hop = _select_hop()
        try:
            res = _start_on_hop(hop, job, request, inputs, room, room_dir, wait)
            return json.dumps(res, ensure_ascii=False)
        except Exception as e1:
            logger.warning("daou-mac: job %s hop=%s start failed: %s", job, hop, e1)
            if hop == PRIMARY and (os.environ.get("DAOU_FORCE_HOP") or "") not in HOPS:
                # mid-start failover
                logger.warning("daou-mac: mid-start failover %s → %s", PRIMARY, FALLBACK)
                try:
                    res = _start_on_hop(FALLBACK, job, request, inputs, room, room_dir, wait)
                    res["failover_from"] = PRIMARY
                    return json.dumps(res, ensure_ascii=False)
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        return _err("콘텐츠 작업자가 바빠요(동시 작업 한도). 잠시 뒤 다시 시도하세요.")
                    return _err(f"콘텐츠 API 오류 {e.code} (hop={FALLBACK})")
                except Exception as e2:
                    logger.warning("daou-mac: failover hop failed: %s", e2)
                    return _err(f"콘텐츠 작업 실패(mac+failover): {e2}")
            if isinstance(e1, urllib.error.HTTPError):
                if e1.code == 429:
                    return _err("콘텐츠 작업자가 바빠요(동시 작업 한도). 잠시 뒤 다시 시도하세요.")
                return _err(f"콘텐츠 API 오류 {e1.code} (hop={hop})")
            return _err(f"콘텐츠 작업 실패 (hop={hop}): {e1}")


def mac_status(params: dict, session_id: str = "", **_kw) -> str:
    room, _ = _room(session_id)
    job = str(params.get("job_id") or "").strip()
    if not room or not _JOB_RE.match(job):
        return _err("job_id 가 올바르지 않아요.")
    st = _load(job)
    if not st or st.get("room") != room:
        return _err("이 방의 작업이 아니에요.")
    hop = st.get("hop") or PRIMARY
    if st.get("status") == "completed":
        return json.dumps({"job_id": job, "status": "completed", "hop": hop, "files": st.get("files", []),
                           "how_to_deliver": "답변에 files 의 절대 경로를 그대로 적으면 방에 첨부돼요."}, ensure_ascii=False)
    try:
        wait = max(10, min(int(params.get("wait_seconds") or 240), MAX_WAIT))
        with _job_lock(job):
            res = _wait(job, st, wait)
        if res.get("status") == "running":
            _ensure_bg_follow(job)
        res["hop"] = hop
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return _err(f"상태 확인 실패: {e}")


def mac_hop_health(params: dict, session_id: str = "", **_kw) -> str:
    """Ops/smoke: report which hop would be chosen and per-hop health."""
    force = (os.environ.get("DAOU_FORCE_HOP") or "").strip()
    mac_ok = _health_check("mac")
    fb_ok = _health_check("vps185")
    selected = _select_hop()
    return json.dumps({
        "mac": {"healthy": mac_ok, **{k: HOPS["mac"][k] for k in ("host", "api", "work")}},
        "vps185": {"healthy": fb_ok, **{k: HOPS["vps185"][k] for k in ("host", "api", "work")}},
        "selected": selected,
        "force_hop": force or None,
    }, ensure_ascii=False)


CONTENT_SCHEMA = {
    "name": "daou_mac_content",
    "description": (
        "콘텐츠 작업자(맥스튜디오 우선, 장애 시 VPS185 자동 폴백)에게 파일 제작을 맡긴다: 한국어 음성/나레이션(TTS), "
        "음성·영상 받아쓰기(자막 srt), 숏폼 세로 영상(쇼츠), 이미지 생성(포스터·삽화·썸네일), 영상 편집·자막 입히기, "
        "한글 hwpx 문서, 로컬 LLM 요약·번역(기밀 자료). 결과 파일은 이 방 작업 폴더의 mac-<job>/ 로 받아 와서 경로를 돌려준다 — "
        "답변에 그 절대 경로를 적으면 방에 첨부된다. 응답 JSON의 hop 필드로 mac|vps185 확인. "
        "음성 1분 이내 ~1분, 이미지 ~1분, 숏폼 30초 ~5~8분 걸림. "
        "기본 wait 90초. running이면 job_id만 주고 턴 종료—어댑터 follow가 배달. 같은 턴에서 daou_mac_status 반복 금지(메시지가 끊김)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "만들 것을 구체적으로 (종류, 길이, 말투·목소리, 형식, 원고가 있으면 원고 전문)."},
            "inputs": {"type": "array", "items": {"type": "string"}, "description": "함께 보낼 이 방 작업 폴더 안 파일의 절대 경로"},
            "wait_seconds": {"type": "integer", "description": "기다릴 최대 초 (기본 90, 최대 900)"},
        },
        "required": ["request"],
    },
}

STATUS_SCHEMA = {
    "name": "daou_mac_status",
    "description": "이미 끝난 job만 확인할 때 사용. 긴 영상 running 중이면 호출하지 말고 턴을 끝낸다(어댑터 follow가 배달). 반복 poll 금지.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "wait_seconds": {"type": "integer", "description": "기다릴 최대 초 (기본 240)"},
        },
        "required": ["job_id"],
    },
}

HEALTH_SCHEMA = {
    "name": "daou_mac_hop_health",
    "description": "맥스튜디오/VPS185 콘텐츠 hop 헬스와 이번 선택 hop(mac|vps185)을 보고한다. 장애 진단·폴백 확인용.",
    "parameters": {"type": "object", "properties": {}},
}


def register(ctx):
    ctx.register_tool(name="daou_mac_content", toolset="daou_docs", schema=CONTENT_SCHEMA, handler=mac_content, emoji="🎬")
    ctx.register_tool(name="daou_mac_status", toolset="daou_docs", schema=STATUS_SCHEMA, handler=mac_status, emoji="⏳")
    ctx.register_tool(name="daou_mac_hop_health", toolset="daou_docs", schema=HEALTH_SCHEMA, handler=mac_hop_health, emoji="🩺")
