"""OC-JOBSTATE tests: mac fail → vps185 fallback, late mac result, gateway restart per host. Exactly one delivery each."""
import importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile, time
import pytest

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN = os.environ.get("PLUGIN", str(HERE.parent / "__init__.py"))
DECK = os.environ.get("OC_TEST_DECK", str(HERE / "fixture-deck.pptx"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    roots = {"mac": tmp_path / "mac", "vps185": tmp_path / "vps185"}
    for r in roots.values():
        r.mkdir()
    bins = {}
    for t in roots:
        b = tmp_path / f"oc-{t}"
        bins[t] = b
    st = tmp_path / "state"
    monkeypatch.setenv("UBM_GATE_HOME", os.environ.get("UBM_GATE_HOME", "/opt/ubm-template-gate"))
    monkeypatch.setenv("UBM_GATE_STATE", str(st))
    monkeypatch.setenv("UBM_GATE_RECOVERY", "0")
    monkeypatch.setenv("DAOU_OPENCODE_TARGET", "mac")
    monkeypatch.setenv("DAOU_MAC_HOST", "fake-mac")
    monkeypatch.setenv("DAOU_OPENCODE_HOST", "fake-185")
    monkeypatch.setenv("DAOU_OPENCODE_MAC_WORK", str(roots["mac"]))
    monkeypatch.setenv("DAOU_OPENCODE_WORK", str(roots["vps185"]))
    monkeypatch.setenv("DAOU_OPENCODE_MAC_BIN", str(bins["mac"]))
    monkeypatch.setenv("DAOU_OPENCODE_BIN", str(bins["vps185"]))
    monkeypatch.setenv("PPT_OPENCODE_WAIT", "60")
    sys.modules.pop("ubm_template_gate", None)
    spec = importlib.util.spec_from_file_location(f"dw{time.time_ns()}", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    import ubm_template_gate as g
    g.STATE_DIR = st
    g.PENDING_DIR = st / "pending"
    m._GATE_MOD = g
    down = set()

    def ssh(cmd, timeout=120, target=None):
        t = target or m._opencode_target()
        if t in down:
            raise RuntimeError("opencode-ssh fail (255): fake host down")
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"opencode-ssh fail ({r.returncode})")
        return r.stdout

    def scp_to(paths, rdir, timeout=120, target=None):
        for p in paths:
            shutil.copy(p, rdir)

    def scp_from(rp, lp, timeout=120, target=None):
        t = target or m._opencode_target()
        if t in down:
            raise RuntimeError("down")
        shutil.copy(rp, lp)

    monkeypatch.setattr(m, "_opencode_ssh", ssh)
    monkeypatch.setattr(m, "_opencode_scp_to", scp_to)
    monkeypatch.setattr(m, "_opencode_scp_from", scp_from)
    _prof = m._opencode_profile
    monkeypatch.setattr(m, "_opencode_profile", lambda t=None: {**_prof(t), "dsenv": str(tmp_path / "no-ds.env")})
    monkeypatch.setattr(m, "_opencode_build_prompt", lambda **k: ("prompt", "", {}))
    sent = []
    g.room_id_for_folder = lambda f: "room-1"
    deliver = lambda rid, path, name, txt="": sent.append((rid, path)) or True

    def mkbin(t, script):
        bins[t].write_text("#!/bin/bash\n" + script)
        bins[t].chmod(0o755)

    out = tmp_path / "out" / "room-1"
    out.mkdir(parents=True)
    return dict(m=m, g=g, roots=roots, mkbin=mkbin, sent=sent, deliver=deliver, down=down, out=out, st=st)


def ok_bin(delay=0):
    return f"sleep {delay}; cp {DECK} \"$PWD/../out/deck.pptx\"\n"


def deliveries(e):
    """in-turn deliveries (claims with how=in_turn) + recovery sends."""
    d = e["st"] / "oc-delivered"
    claims = [json.loads(p.read_text()) for p in d.glob("*.json")] if d.is_dir() else []
    return claims


def run(e):
    return e["m"]._opencode_sidecar(str(e["out"] / "deck.pptx"), "T", [], theme="ubion", pages=3)


def recover(e):
    return e["m"]._oc_recover_pending(deliver=e["deliver"])


def test_mac_ok_single_delivery(env):
    env["mkbin"]("mac", ok_bin())
    env["mkbin"]("vps185", "exit 99\n")
    r = run(env)
    assert r.get("path_a") and r["opencode_target"] == "mac"
    assert recover(env) == [] and env["sent"] == []
    c = deliveries(env)
    assert len(c) == 1 and c[0]["target"] == "mac" and c[0]["how"] == "in_turn"


def test_mac_fail_fallback_185_single_delivery(env):
    env["mkbin"]("mac", "exit 1\n")
    env["mkbin"]("vps185", ok_bin())
    r = run(env)
    assert r.get("path_a") and r["opencode_target"] == "vps185" and "no_deck" in r["opencode_mac_fail"]
    st = env["m"]._oc_jobstate(r["opencode_lj"])
    assert [a["target"] for a in st["attempts"]] == ["mac", "vps185"]
    assert [a["status"] for a in st["attempts"]] == ["superseded", "delivered"]
    assert st["owner"] == st["attempts"][1]["job"]
    for _ in range(2):
        recover(env)
    assert env["sent"] == []
    assert len(deliveries(env)) == 1


def test_mac_host_down_fallback(env):
    env["down"].add("mac")
    env["mkbin"]("vps185", ok_bin())
    r = run(env)
    assert r.get("path_a") and r["opencode_target"] == "vps185"
    recover(env)
    assert env["sent"] == [] and len(deliveries(env)) == 1


def test_late_mac_result_after_fallback_discarded(env):
    # mac opencode exits 1 but leaves a child that writes the deck 25 s later (same process group)
    env["mkbin"]("mac", f"(sleep 25; cp {DECK} \"$PWD/../out/deck.pptx\") & exit 1\n")
    env["mkbin"]("vps185", ok_bin())
    r = run(env)
    assert r["opencode_target"] == "vps185"
    time.sleep(28)
    mac_job = env["m"]._oc_jobstate(r["opencode_lj"])["attempts"][0]["remote_job"]
    assert pathlib.Path(mac_job, "CANCELLED").exists()
    assert not pathlib.Path(mac_job, "out/deck.pptx").exists(), "group kill should stop the late writer"
    # even if a late deck appears anyway (kill failed) and a stale pending record resurfaces, it is discarded
    shutil.copy(DECK, pathlib.Path(mac_job, "out/deck.pptx"))
    closed = env["st"] / "pending-closed"
    rec = json.loads(next(p for p in closed.glob("*.json") if json.loads(p.read_text())["target"] == "mac").read_text())
    rec["pid"] = 999999
    (env["st"] / "pending").mkdir(exist_ok=True)
    (env["st"] / "pending" / f"{rec['job']}.json").write_text(json.dumps(rec))
    res = recover(env)
    assert res and res[0]["state"] == "discarded_superseded"
    assert env["sent"] == [] and len(deliveries(env)) == 1


@pytest.mark.parametrize("host", ["mac", "vps185"])
def test_gateway_restart_mid_job(env, host, monkeypatch):
    """Turn dies mid-job (owner pid dead); recovery on *any* gateway delivers once from the recorded host."""
    monkeypatch.setenv("DAOU_OPENCODE_TARGET", host)  # recovery runs with the *other* default target
    m, g = env["m"], env["g"]
    lj, job = f"lj-{host}", f"oc-test-{host}"
    rj = env["roots"][host] / job
    (rj / "out").mkdir(parents=True)
    m._oc_attempt(lj, host, job, str(rj), "running")
    g.pending_add({"job": job, "remote_job": str(rj), "remote_pptx": str(rj / "out/deck.pptx"),
                   "path": str(env["out"] / "deck.pptx"), "room": "room-1", "theme": None, "theme_raw": "ubion",
                   "lj": lj, "target": host, "attempt": 1})
    p = g.PENDING_DIR / f"{job}.json"
    rec = json.loads(p.read_text()); rec["pid"] = 999999; p.write_text(json.dumps(rec))
    other = "vps185" if host == "mac" else "mac"
    monkeypatch.setenv("DAOU_OPENCODE_TARGET", other)
    assert recover(env)[0]["state"] == "running"  # still running on its host
    shutil.copy(DECK, rj / "out/deck.pptx")
    res = recover(env)
    assert res[0]["state"] == "delivered" and res[0]["target"] == host
    recover(env); recover(env)
    assert len(env["sent"]) == 1 and len(deliveries(env)) == 1


def test_restart_during_185_retry(env):
    """mac superseded, gateway dies during the vps185 attempt → only the 185 deck is delivered."""
    m, g = env["m"], env["g"]
    lj = "lj-x"
    mj, vj = env["roots"]["mac"] / "oc-m", env["roots"]["vps185"] / "oc-v"
    for d in (mj, vj):
        (d / "out").mkdir(parents=True)
        shutil.copy(DECK, d / "out/deck.pptx")
    m._oc_attempt(lj, "mac", "oc-m", str(mj), "running")
    for job, d, t in (("oc-m", mj, "mac"), ("oc-v", vj, "vps185")):
        g.pending_add({"job": job, "remote_job": str(d), "remote_pptx": str(d / "out/deck.pptx"),
                       "path": str(env["out"] / "deck.pptx"), "room": "room-1", "theme": None, "lj": lj, "target": t})
        if t == "vps185":
            m._oc_attempt(lj, t, job, str(d), "running")
        pp = g.PENDING_DIR / f"{job}.json"; r = json.loads(pp.read_text()); r["pid"] = 999999; pp.write_text(json.dumps(r))
    res = {x["job"]: x["state"] for x in recover(env)}
    assert res == {"oc-m": "discarded_superseded", "oc-v": "delivered"}
    recover(env)
    assert len(env["sent"]) == 1 and env["sent"][0][1].endswith(".pptx")
    assert deliveries(env)[0]["target"] == "vps185"
