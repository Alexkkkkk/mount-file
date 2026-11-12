#!/usr/bin/env python3
"""Bounded, fail-closed CI repair proposals for Mount File."""
from __future__ import annotations
import argparse, ast, hashlib, json, logging, os, re, shutil, subprocess, tempfile
from pathlib import Path
from typing import Any
from openai import OpenAI

LOG = logging.getLogger("ci-healer-v3")
ROOT = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
CACHE = ROOT / ".ci_healer_cache.json"
IMAGE = os.environ.get("CI_HEALER_IMAGE", "ci-healer-sandbox:latest")
PATCHABLE = {"missing_dependency", "assertion_error"}
ALLOWED = ("mountfile/", "scripts/", "tools/", "tests/")
FORBIDDEN = (".github/", "Dockerfile", "pyproject.toml", "setup.cfg")
FORBIDDEN_IMPORTS = {"os", "subprocess", "socket", "ctypes", "multiprocessing", "signal", "resource", "pty", "shutil"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "system", "popen", "spawn", "call", "run", "Popen", "connect", "urlopen"}


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def classify(log: str) -> str:
    if re.search(r"TimeoutError|ConnectionResetError|ETIMEDOUT|flaky|intermittent", log, re.I):
        return "flaky"
    if re.search(r"ModuleNotFoundError|ImportError|No module named", log):
        return "missing_dependency"
    if re.search(r"AssertionError|assert .* ==|pytest\.fail", log):
        return "assertion_error"
    if re.search(r"permission denied|no space left|disk quota", log, re.I):
        return "env_config_error"
    return "unknown"


def fingerprint(job: str, category: str, log: str) -> str:
    value = json.dumps({"job": job, "category": category, "log": log[-4000:]}, sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def load_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE.read_text()) if CACHE.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data: dict[str, Any]) -> None:
    CACHE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def get_logs(run_id: str) -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN/GH_TOKEN is required")
    result = run(["gh", "run", "view", run_id, "--log"], env={**os.environ, "GH_TOKEN": token})
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return result.stdout[-120_000:]


def ask_llm(prompt: str, *, max_tokens: int) -> str:
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY or OPENAI_API_KEY is required")
    kwargs = {"api_key": key}
    if os.environ.get("LLM_BASE_URL"):
        kwargs["base_url"] = os.environ["LLM_BASE_URL"]
    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"), temperature=0,
        max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


def validate_diff(diff: str, allowed_files: set[str]) -> None:
    if not diff or len(diff) > 80_000 or "```" in diff:
        raise ValueError("empty, oversized, or fenced diff")
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            path = line[6:]
            if path == "/dev/null":
                continue
            paths.add(path)
            if not path.startswith(ALLOWED) or path.startswith(FORBIDDEN) or ".." in Path(path).parts:
                raise ValueError(f"path outside policy: {path}")
    if not paths or not paths.issubset(allowed_files):
        raise ValueError(f"diff paths {sorted(paths)} are not policy-approved")
    for block in re.split(r"(?=^diff --git )", diff, flags=re.M):
        additions = "\n".join(x[1:] for x in block.splitlines() if x.startswith("+") and not x.startswith("+++"))
        if additions and any(p.endswith(".py") for p in paths):
            try:
                tree = ast.parse(additions)
            except SyntaxError as exc:
                raise ValueError(f"added Python is invalid: {exc}") from exc
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(a.name.split(".")[0] in FORBIDDEN_IMPORTS for a in node.names):
                    raise ValueError("forbidden import")
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
                    raise ValueError("forbidden import")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                    raise ValueError("forbidden call")
    patch = Path(tempfile.mkstemp(prefix="ci-healer-check-", suffix=".diff")[1])
    try:
        patch.write_text(diff)
        check = run(["git", "apply", "--check", "--whitespace=error", str(patch)], cwd=ROOT, env={**os.environ})
    finally:
        patch.unlink(missing_ok=True)
    if check.returncode:
        raise ValueError(f"git apply check failed: {check.stderr[-1000:]}")


def docker_workspace(diff: str, *, apply: bool) -> tuple[int, str]:
    temp = Path(tempfile.mkdtemp(prefix="ci-healer-"))
    try:
        shutil.copytree(ROOT, temp / "repo", ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".ruff_cache", "__pycache__"))
        repo = temp / "repo"
        if apply:
            patch = temp / "candidate.diff"; patch.write_text(diff)
            result = run(["git", "apply", "--whitespace=error", str(patch)], cwd=repo)
            if result.returncode:
                return result.returncode, result.stderr
        result = run(["docker", "run", "--rm", "--network", "none", "--user", "1000:1000", "-v", f"{repo}:/workspace:rw", IMAGE, "python", "-m", "pytest", "-q"], cwd=repo)
        return result.returncode, (result.stdout + "\n" + result.stderr)[-12_000:]
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def ensure_image() -> None:
    result = run(["docker", "build", "-f", str(ROOT / "tools/ci-healer-v3/Dockerfile"), "-t", IMAGE, "."], cwd=ROOT)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])


def publish_pr(repo_name: str, run_id: str, diff: str) -> str:
    branch = f"ci-heal/run-{run_id}"
    patch = ROOT / ".ci_healer_patch.diff"; patch.write_text(diff)
    commands = [
        ["git", "checkout", "-b", branch], ["git", "apply", "--whitespace=error", str(patch)],
        ["git", "add", "mountfile", "scripts", "tools", "tests"],
        ["git", "-c", "user.name=ci-healer[bot]", "-c", "user.email=ci-healer@users.noreply.github.com", "commit", "-m", f"chore(ci-heal): repair run {run_id}"],
        ["git", "push", "--set-upstream", "origin", branch],
    ]
    for cmd in commands:
        result = run(cmd, cwd=ROOT)
        if result.returncode: raise RuntimeError(result.stderr[-3000:])
    result = run(["gh", "pr", "create", "--repo", repo_name, "--draft", "--base", "main", "--head", branch, "--title", f"chore(ci-heal): repair run {run_id}", "--body", "Generated by CI Healer v3; requires human review."])
    if result.returncode: raise RuntimeError(result.stderr[-3000:])
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--run-id", required=True); parser.add_argument("--repo", required=True); parser.add_argument("--job-name", default="")
    args = parser.parse_args(); logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logs = get_logs(args.run_id); category = classify(logs); LOG.info("category=%s job=%s", category, args.job_name)
    if category not in PATCHABLE: LOG.warning("fail closed: category is not patchable"); return 0
    key = fingerprint(args.job_name, category, logs); cached = load_cache().get(key)
    diagnosis_text = ask_llm(f"Return JSON only with root_cause, confidence, suggested_files. CI log:\n{logs[-8000:]}", max_tokens=500)
    try: suggested = json.loads(diagnosis_text).get("suggested_files", [])
    except json.JSONDecodeError: suggested = []
    allowed = {p for p in suggested if isinstance(p, str) and p.startswith(ALLOWED)}
    if not allowed: LOG.warning("fail closed: no policy-approved files"); return 0
    diff = cached.get("diff") if cached else ask_llm(f"Create only a unified git diff for {sorted(allowed)}. Diagnosis: {diagnosis_text}. Log:\n{logs[-6000:]}", max_tokens=4000)
    validate_diff(diff, allowed); ensure_image()
    red_code, _ = docker_workspace(diff, apply=False); green_code, green_log = docker_workspace(diff, apply=True)
    if red_code == 0 or green_code != 0: LOG.error("verification failed red=%s green=%s %s", red_code, green_code, green_log[-2000:]); return 0
    cache = load_cache(); cache[key] = {"diff": diff}; save_cache(cache); print(publish_pr(args.repo, args.run_id, diff)); return 0


if __name__ == "__main__": raise SystemExit(main())
