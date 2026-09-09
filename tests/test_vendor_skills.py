"""provision/vendor-skills.sh through its real interface: pins in, files out.
Network fetches are replaced by local tarballs shaped like the real ones
(`npm pack` → package/…, codeload.github.com → <repo>-<ref>/…)."""
import json
import os
import subprocess
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "provision" / "vendor-skills.sh"


def tarball(tmp_path: Path, name: str, root: str, files: dict[str, str]) -> Path:
    src = tmp_path / f"{name}-src" / root
    for rel, body in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    out = tmp_path / f"{name}.tgz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(src, arcname=root)
    return out


def rig(tmp_path: Path, jesdi_skills=("to-spec",), mp_skills=("tdd",)):
    npm = tarball(tmp_path, "npm", "package", {
        "skills/to-spec/SKILL.md": "to-spec fork",
        "skills/to-spec/reference.md": "extra file",
        "skills-manifest.json": "{}",
    })
    gh = tarball(tmp_path, "gh", "skills-abc123", {
        "skills/engineering/tdd/SKILL.md": "tdd upstream",
        "skills/engineering/tdd/tests.md": "how to test",
        "skills/productivity/grill-me/SKILL.md": "not pinned",
    })
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({
        "jesdi": {"package": "@jesdi/skills", "version": "0.0.14", "skills": list(jesdi_skills)},
        "mattpocock": {"repo": "mattpocock/skills", "ref": "abc123", "skills": list(mp_skills)},
    }))
    dest = tmp_path / "seed" / "skills"
    dest.mkdir(parents=True)
    (dest / ".gitkeep").touch()
    env = dict(os.environ, AGENT_OPS_SKILLS_PINS=str(pins), AGENT_OPS_SKILLS_DEST=str(dest),
               AGENT_OPS_SKILLS_NPM_TARBALL=str(npm), AGENT_OPS_SKILLS_GH_TARBALL=str(gh))
    return dest, env


def run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_pinned_skills_are_materialized_with_a_record(tmp_path):
    dest, env = rig(tmp_path)
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert (dest / "to-spec" / "SKILL.md").read_text() == "to-spec fork"
    assert (dest / "to-spec" / "reference.md").is_file()
    assert (dest / "tdd" / "SKILL.md").read_text() == "tdd upstream"
    assert not (dest / "grill-me").exists()
    assert json.loads((dest / "VENDORED.json").read_text()) == {"skills": {
        "to-spec": "@jesdi/skills@0.0.14", "tdd": "mattpocock/skills@abc123"}}
    assert (dest / ".gitkeep").exists()


def test_unpinned_skills_are_deleted_on_refresh(tmp_path):
    dest, env = rig(tmp_path)
    (dest / "stale").mkdir(); (dest / "stale" / "SKILL.md").write_text("old")
    assert run(env).returncode == 0
    assert not (dest / "stale").exists()


def test_missing_pinned_skill_fails_loudly(tmp_path):
    dest, env = rig(tmp_path, mp_skills=("tdd", "wizard"))
    r = run(env)
    assert r.returncode != 0 and "wizard" in r.stderr
    assert not (dest / "VENDORED.json").exists()   # nothing partial lands
