"""Execute the real release YAML offline, in throw-away Git repositories.

Run: python3 -m unittest discover -s test_suite -p 'test_release_workflow.py' -v
PyYAML is optional locally: absent YAML support skips these tests explicitly.
The regression step runs tiny fixture tests, not this suite recursively or the
application. Only release.yml and release_version.py are read from the project;
no user data, repository remotes, credentials, or real gh executable are used.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


class WorkflowSandbox:
    """A checkout, a bare file:// origin, and an allowlisted child environment."""

    def __init__(self, case, workflow, helper, version="1.0.4", tags=()):
        self.case = case
        self.workflow = workflow
        self.temp = tempfile.TemporaryDirectory(prefix="tg-release-workflow-")
        case.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "origin.git"
        self.seed = self.base / "seed"
        self.repo = self.base / "checkout"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.git_log = self.base / "git.jsonl"
        self.gh_log = self.base / "gh.jsonl"
        self.pip_log = self.base / "pip.jsonl"
        self.summary = self.base / "summary.md"
        self.outputs = {}
        self.results = []
        self.steps = workflow["jobs"]["release"]["steps"]
        self.real_git = shutil.which("git")
        self.env = {
            "PATH": os.pathsep.join((str(self.bin), str(Path(sys.executable).parent),
                                      "/usr/bin", "/bin")),
            "HOME": str(self.base / "home"),
            "XDG_CONFIG_HOME": str(self.base / "home" / "config"),
            "TMPDIR": str(self.base), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Offline Release Test",
            "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
            "GIT_COMMITTER_NAME": "Offline Release Test",
            "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "offline-fixture/TG-Persona",
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GH_TOKEN": "offline-fake-token-not-a-secret",
            "MOCK_GIT_LOG": str(self.git_log), "MOCK_GH_LOG": str(self.gh_log),
            "MOCK_ORIGIN": str(self.remote), "REAL_GIT": self.real_git,
        }
        Path(self.env["HOME"]).mkdir()
        self.git("init", "--bare", "-q", str(self.remote))
        self.seed.mkdir()
        self.git("init", "-q", "-b", "main", str(self.seed))
        files = {
            "VERSION": version + "\n",
            "main.py": "FIXTURE = 'main fixture, no application imports'\n",
            "updater.py": "FIXTURE = 'updater fixture, no network'\n",
            "test_suite/test_fixture.py": (
                "import unittest\nfrom pathlib import Path\n"
                "class FixtureTest(unittest.TestCase):\n"
                "    def test_fixture(self):\n"
                "        Path('.regression-python-ok').write_text('ok')\n"
                "        self.assertTrue(True)\n"),
            "test_suite/test_frontend.js": (
                "require('fs').writeFileSync('.regression-node-ok', 'ok');\n"),
        }
        for name, content in files.items():
            path = self.seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        target = self.seed / "release_tools/release_version.py"
        target.parent.mkdir()
        target.write_bytes(helper)
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-qm", "fixture: initial source", cwd=self.seed)
        for tag in tags:
            self.git("tag", "-a", tag, "-m", "previous fixture release", cwd=self.seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "-q", "origin", "main", "--tags", cwd=self.seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        # Equivalent to checkout@v4 ref=main, fetch-depth=0; no live checkout.
        self.git("clone", "-q", "--branch", "main", str(self.remote), str(self.repo))
        self.initial_sha = self.git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        self.initial_tags = self.remote_tags()
        self._write_mocks()

    def git(self, *args, cwd=None):
        return subprocess.run([self.real_git, *args], cwd=cwd or self.base,
                              env=self.env, text=True, capture_output=True,
                              check=True, timeout=30)

    def _write_mocks(self):
        git_mock = '''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['MOCK_GIT_LOG'], 'a') as stream:
    stream.write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\\n')
if args and args[0] == 'push':
    if any(arg == '-f' or arg.startswith(('--force', '+')) for arg in args):
        sys.exit('offline mock: force pushes are forbidden')
    if 'origin' not in args:
        sys.exit('offline mock: only local origin is allowed')
    remote = subprocess.check_output([os.environ['REAL_GIT'], 'remote', 'get-url', 'origin'], text=True).strip()
    if Path(remote).resolve() != Path(os.environ['MOCK_ORIGIN']).resolve():
        sys.exit('offline mock: non-fixture remote is forbidden')
sys.exit(subprocess.call([os.environ['REAL_GIT'], *args]))
'''
        gh_mock = '''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['MOCK_GH_LOG'], 'a') as stream:
    stream.write(json.dumps({'argv': args, 'token': os.environ.get('GH_TOKEN'), 'cwd': os.getcwd()}) + '\\n')
if args[:2] != ['release', 'create'] or '--verify-tag' not in args:
    sys.exit('offline mock: expected release create --verify-tag')
tag = args[2]
remote = os.environ['MOCK_ORIGIN']
def ref(name):
    return subprocess.check_output([os.environ['REAL_GIT'], '--git-dir', remote, 'rev-parse', name], text=True).strip()
if ref('refs/tags/' + tag + '^{}') != ref('refs/heads/main'):
    sys.exit('offline mock: remote tag/main SHA mismatch')
assets = ['dist/TG-Persona-' + tag + '.tar.gz', 'dist/TG-Persona-' + tag + '.zip', 'dist/SHA256SUMS.txt']
if not all(asset in args and Path(asset).is_file() for asset in assets):
    sys.exit('offline mock: missing assets')
if args[args.index('--repo') + 1] != 'offline-fixture/TG-Persona':
    sys.exit('offline mock: wrong repository')
if os.environ.get('GH_TOKEN') != 'offline-fake-token-not-a-secret':
    sys.exit('offline mock: fake token missing')
print('offline mock: release created; no HTTP requests')
'''
        for name, code in (("git", git_mock), ("gh", gh_mock)):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + code, encoding="utf-8")
            path.chmod(0o755)

    def resolve(self, expression, requested):
        if expression == "inputs.version":
            return requested
        if expression == "github.token":
            return self.env["GH_TOKEN"]
        if expression == "github.repository":
            return self.env["GITHUB_REPOSITORY"]
        if expression == "github.ref":
            return self.env["GITHUB_REF"]
        match = re.fullmatch(r"steps\.([\w-]+)\.outputs\.([\w-]+)", expression)
        if match:
            return self.outputs[match[1]][match[2]]
        raise AssertionError("Unsupported workflow expression: " + expression)

    def execute(self, requested="", ref="refs/heads/main", before_step=None):
        self.env["GITHUB_REF"] = ref
        for index, step in enumerate(self.steps):
            if "run" not in step:
                action = step.get("uses", "")
                self.case.assertTrue(action.startswith(("actions/checkout@", "actions/setup-python@")))
                if action.startswith("actions/setup-python@"):
                    self.case.assertEqual(step["with"]["python-version"], "3.12")
                continue
            if before_step:
                before_step(self, step)
            env = dict(self.env)
            for key, value in {**self.workflow["jobs"]["release"].get("env", {}),
                               **step.get("env", {})}.items():
                env[key] = EXPRESSION.sub(lambda m: self.resolve(m[1], requested), str(value))
            # Output expressions in run are converted to env references, never
            # interpolated into executable shell text. Inputs must be in env.
            def output_env(match):
                expression = match[1]
                self.case.assertRegex(expression, r"^steps\.[\w-]+\.outputs\.[\w-]+$")
                key = "WF_OUTPUT_" + re.sub(r"\W", "_", expression).upper()
                env[key] = self.resolve(expression, requested)
                return '${' + key + '}'
            script = EXPRESSION.sub(output_env, step["run"])
            output = self.base / ("step-%d.out" % index)
            output.write_text("")
            env["GITHUB_OUTPUT"] = str(output)
            if step.get("name") == "安装轻量测试依赖":
                # 明确 mock 基础设施安装，绝不让离线测试发起 pip 网络请求。
                argv = shlex.split(script)
                self.case.assertEqual(argv, ["python3", "-m", "pip", "install", "fastapi", "telethon", "httpx", "uvicorn", "pyyaml"])
                with self.pip_log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"argv": argv, "mock": True}) + "\n")
                result = subprocess.CompletedProcess(argv, 0, "offline mock: test dependencies\n", "")
            else:
                result = subprocess.run(
                    [shutil.which("bash"), "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                    cwd=self.repo, env=env, text=True, capture_output=True, timeout=30)
            self.results.append((step, result))
            if "id" in step:
                self.outputs[step["id"]] = dict(line.split("=", 1) for line in
                                              output.read_text().splitlines() if "=" in line)
            if result.returncode:
                return result
        return result

    def remote_sha(self):
        return self.git("rev-parse", "refs/heads/main", cwd=self.remote).stdout.strip()

    def remote_tags(self):
        return self.git("tag", "--list", cwd=self.remote).stdout.splitlines()

    def calls(self, kind):
        path = self.git_log if kind == "git" else self.gh_log
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def push_concurrent_main(self, tag=None):
        actor = self.base / "concurrent-checkout"
        self.git("clone", "-q", "--branch", "main", str(self.remote), str(actor))
        (actor / "concurrent.txt").write_text("new commit while release is packaging\n")
        self.git("add", ".", cwd=actor)
        self.git("commit", "-qm", "concurrent: main advanced", cwd=actor)
        if tag:
            self.git("tag", "-a", tag, "-m", "concurrent release", cwd=actor)
        self.git("push", "-q", "origin", "main", "--tags", cwd=actor)
        return self.remote_sha()


@unittest.skipUnless(yaml is not None, "PyYAML missing; workflow CI must install pyyaml to execute offline release tests")
class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [name for name in ("git", "bash", "node", "gzip", "tar", "sha256sum")
                   if not shutil.which(name)]
        if missing:
            raise unittest.SkipTest("Offline workflow tools missing: " + ", ".join(missing))
        # BaseLoader keeps the YAML 1.1 key 'on' and every env value a string.
        cls.workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        cls.helper = (ROOT / "release_tools/release_version.py").read_bytes()

    def sandbox(self, **kwargs):
        return WorkflowSandbox(self, self.workflow, self.helper, **kwargs)

    def assert_success(self, box, result):
        self.assertEqual(result.returncode, 0, "\n".join(
            step.get("name", "") + "\n" + run.stdout + run.stderr for step, run in box.results))
        self.assertEqual(len(box.results), sum("run" in step for step in box.steps))
        self.assertTrue((box.repo / ".regression-python-ok").is_file())
        self.assertTrue((box.repo / ".regression-node-ok").is_file())

    def assert_release(self, box, version, changed):
        tag = "v" + version
        sha = box.outputs["commit"]["sha"]
        self.assertEqual(box.outputs["ver"], {"version": version, "tag": tag})
        self.assertEqual(box.remote_sha(), sha)
        self.assertEqual(box.git("rev-parse", tag + "^{}", cwd=box.remote).stdout.strip(), sha)
        self.assertEqual(box.git("cat-file", "-t", "refs/tags/" + tag, cwd=box.remote).stdout.strip(), "tag")
        self.assertEqual(box.git("show", "main:VERSION", cwd=box.remote).stdout, version + "\n")
        if changed:
            self.assertNotEqual(sha, box.initial_sha)
            self.assertEqual(box.git("diff", "--name-only", box.initial_sha, sha, cwd=box.repo).stdout.splitlines(), ["VERSION"])
        else:
            self.assertEqual(sha, box.initial_sha)
        dist = box.repo / "dist"
        tar_path = dist / ("TG-Persona-" + tag + ".tar.gz")
        zip_path = dist / ("TG-Persona-" + tag + ".zip")
        with tarfile.open(tar_path, "r:gz") as archive:
            self.assertEqual(archive.extractfile("tg-persona/VERSION").read(), (version + "\n").encode())
            tar_main = archive.extractfile("tg-persona/main.py").read()
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.read("tg-persona/VERSION"), (version + "\n").encode())
            self.assertEqual(archive.read("tg-persona/main.py"), tar_main)
        self.assertEqual(tar_main, box.git("show", sha + ":main.py", cwd=box.repo).stdout.encode())
        checksums = dict(line.split(None, 1)[::-1] for line in (dist / "SHA256SUMS.txt").read_text().splitlines())
        self.assertEqual(checksums, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (tar_path, zip_path)})
        gh = box.calls("gh")
        self.assertEqual(len(gh), 1)
        self.assertEqual(gh[0]["argv"][:3], ["release", "create", tag])
        self.assertIn("--verify-tag", gh[0]["argv"])
        self.assertEqual(gh[0]["token"], box.env["GH_TOKEN"])
        args = gh[0]["argv"]
        self.assertEqual(args[args.index("--notes-file") + 1], "dist/CHANGELOG.md")
        self.assertEqual(args[args.index("--title") + 1], "TG Persona " + tag)
        self.assertIn("releases/tag/" + tag, box.summary.read_text())
        pushes = [call["argv"] for call in box.calls("git") if call["argv"][:1] == ["push"]]
        self.assertEqual(len(pushes), 1)
        self.assertIn("--atomic", pushes[0])
        self.assertIn("HEAD:refs/heads/main", pushes[0])
        self.assertIn("refs/tags/" + tag, pushes[0])
        self.assertFalse(any(arg.startswith(("--force", "+")) or arg == "-f" for arg in pushes[0]))

    def test_explicit_prefixed_version_full_workflow(self):
        box = self.sandbox(tags=("v1.0.3",))
        self.assert_success(box, box.execute("v1.2.3"))
        self.assert_release(box, "1.2.3", changed=True)

    def test_explicit_unprefixed_version_full_workflow(self):
        box = self.sandbox(tags=("v1.0.3",))
        self.assert_success(box, box.execute("1.2.3"))
        self.assert_release(box, "1.2.3", changed=True)

    def test_blank_uses_unreleased_version_without_extra_commit(self):
        box = self.sandbox(tags=("v1.0.3",))
        self.assert_success(box, box.execute())
        self.assert_release(box, "1.0.4", changed=False)

    def test_blank_increments_already_published_version(self):
        box = self.sandbox(tags=("v1.0.4",))
        self.assert_success(box, box.execute())
        self.assert_release(box, "1.0.5", changed=True)

    def test_blank_uses_highest_published_version_when_file_lags(self):
        box = self.sandbox(version="1.0.3", tags=("v1.1.8",))
        self.assert_success(box, box.execute())
        self.assert_release(box, "1.1.9", changed=True)

    def test_blank_sorts_versions_numerically(self):
        box = self.sandbox(version="1.0.9", tags=("v1.0.9", "v1.0.10"))
        self.assert_success(box, box.execute())
        self.assert_release(box, "1.0.11", changed=True)

    def test_first_release_without_tags_uses_version_file(self):
        box = self.sandbox()
        self.assert_success(box, box.execute())
        self.assert_release(box, "1.0.4", changed=False)

    def test_explicit_equal_unpublished_version_is_allowed(self):
        box = self.sandbox(tags=("v1.0.3",))
        self.assert_success(box, box.execute("v1.0.4"))
        self.assert_release(box, "1.0.4", changed=False)

    def test_archives_use_locked_commit_not_dirty_worktree(self):
        box = self.sandbox()
        def dirty_after_commit(sandbox, step):
            if "git archive" in step.get("run", ""):
                (sandbox.repo / "VERSION").write_text("9.9.9\n")
                (sandbox.repo / "main.py").write_text("DIRTY = True\n")
        self.assert_success(box, box.execute("v1.2.3", before_step=dirty_after_commit))
        self.assert_release(box, "1.2.3", changed=True)
        self.assertEqual((box.repo / "VERSION").read_text(), "9.9.9\n")

    def assert_concurrent_rejection(self, requested):
        box = self.sandbox(tags=("v1.0.3",))
        advanced = []
        def race(sandbox, step):
            if "git push" in step.get("run", ""):
                advanced.append(sandbox.push_concurrent_main())
        result = box.execute(requested, before_step=race)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(advanced), 1)
        self.assertIn("git push", box.results[-1][0]["run"])
        self.assertEqual(box.remote_sha(), advanced[0])
        self.assertNotEqual(box.remote_sha(), box.outputs["commit"]["sha"])
        self.assertEqual(box.remote_tags(), box.initial_tags)
        self.assertEqual(box.git("show", "main:VERSION", cwd=box.remote).stdout, "1.0.4\n")
        self.assertEqual(box.calls("gh"), [])
        self.assertFalse(box.summary.exists())
        # A local tag may exist in the discarded runner. It must not leak to origin.
        pushes = [c["argv"] for c in box.calls("git") if c["argv"][:1] == ["push"]]
        self.assertEqual(len(pushes), 1)
        self.assertIn("--atomic", pushes[0])
        self.assertFalse(any(arg.startswith(("--force", "+")) or arg == "-f" for arg in pushes[0]))

    def test_concurrent_main_rejects_divergent_version_commit_without_remote_tag(self):
        self.assert_concurrent_rejection("v1.2.3")

    def test_concurrent_main_rejects_old_unchanged_snapshot_without_remote_tag(self):
        self.assert_concurrent_rejection("")

    def test_tag_created_concurrently_causes_atomic_main_rollback(self):
        box = self.sandbox()
        old_tag_sha = []
        def create_conflicting_tag(sandbox, step):
            if "git push" in step.get("run", ""):
                sandbox.git("tag", "-a", "v1.2.3", "-m", "concurrent tag", cwd=sandbox.seed)
                sandbox.git("push", "-q", "origin", "refs/tags/v1.2.3", cwd=sandbox.seed)
                old_tag_sha.append(sandbox.git("rev-parse", "v1.2.3^{tag}", cwd=sandbox.remote).stdout)
        result = box.execute("v1.2.3", before_step=create_conflicting_tag)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(box.remote_sha(), box.initial_sha)
        self.assertEqual(box.git("show", "main:VERSION", cwd=box.remote).stdout, "1.0.4\n")
        self.assertEqual(box.git("rev-parse", "v1.2.3^{tag}", cwd=box.remote).stdout, old_tag_sha[0])
        self.assertEqual(box.calls("gh"), [])

    def test_non_main_dispatch_is_rejected_before_version_or_push(self):
        for ref in ("refs/heads/dev", "refs/tags/v1.0.4", "refs/pull/1/merge", "main"):
            with self.subTest(ref=ref):
                box = self.sandbox()
                result = box.execute("v1.2.3", ref=ref)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(box.results), 1)
                self.assertEqual(box.outputs, {})
                self.assertEqual((box.repo / "VERSION").read_text(), "1.0.4\n")
                self.assertEqual(box.remote_sha(), box.initial_sha)
                self.assertEqual(box.remote_tags(), box.initial_tags)
                self.assertEqual(box.calls("gh"), [])
                self.assertEqual(box.calls("git"), [])

    def test_malicious_and_malformed_versions_rejected_without_shell_execution(self):
        box = self.sandbox()
        sentinel = box.base / "INJECTION_EXECUTED"
        payloads = (
            "v1.2.3; touch " + str(sentinel),
            "v1.2.3$(touch " + str(sentinel) + ")",
            "v1.2.3`touch " + str(sentinel) + "`",
            "v1.2.3\ntouch " + str(sentinel),
            "v1.2.3'; touch " + str(sentinel) + "; #",
            'v1.2.3"; touch ' + str(sentinel) + '; #',
            "${{ inputs.version }}", "--help", "../../oops", "1.2", "1.2.3.4",
            "v01.2.3", "1.2.-3", "1.2.3-beta", "1.2.3\ninvalid",
        )
        for requested in payloads:
            with self.subTest(requested=requested):
                box.results = []
                box.outputs = {}
                result = box.execute(requested)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(box.results[-1][0].get("id"), "ver")
                self.assertFalse(sentinel.exists(), "user input was executed as shell code")
                self.assertEqual((box.repo / "VERSION").read_text(), "1.0.4\n")
                self.assertEqual(box.remote_sha(), box.initial_sha)
                self.assertEqual(box.remote_tags(), box.initial_tags)
                self.assertEqual(box.calls("gh"), [])
                self.assertFalse((box.repo / "dist").exists())
                self.assertFalse(any(c["argv"][:1] == ["push"] for c in box.calls("git")))

    def test_existing_and_downgrade_versions_fail_before_commit(self):
        box = self.sandbox(tags=("v1.0.3", "v1.1.0"))
        for requested in ("v1.0.3", "v1.1.0", "0.9.9", "1.0.2", "1.0.5"):
            with self.subTest(requested=requested):
                box.results = []
                box.outputs = {}
                result = box.execute(requested)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(box.results[-1][0].get("id"), "ver")
                self.assertEqual(box.remote_sha(), box.initial_sha)
                self.assertEqual(box.remote_tags(), box.initial_tags)
                self.assertEqual((box.repo / "VERSION").read_text(), "1.0.4\n")
                self.assertEqual(box.calls("gh"), [])

    def test_workflow_is_manual_main_checkout_with_complete_history(self):
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        version = self.workflow["on"]["workflow_dispatch"]["inputs"]["version"]
        self.assertEqual(version["required"], "false")
        self.assertEqual(version["default"], "")
        self.assertEqual(self.workflow["permissions"]["contents"], "write")
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(self.workflow["concurrency"]["group"], "tg-persona-release-main")
        steps = self.workflow["jobs"]["release"]["steps"]
        checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]["with"]["ref"], "main")
        self.assertEqual(checkouts[0]["with"]["fetch-depth"], "0")
        self.assertIn('"$GITHUB_REF"', steps[0]["run"])
        self.assertIn("refs/heads/main", steps[0]["run"])

    def test_workflow_never_interpolates_dispatch_inputs_into_shell(self):
        steps = self.workflow["jobs"]["release"]["steps"]
        version_steps = [step for step in steps if step.get("id") == "ver"]
        self.assertEqual(len(version_steps), 1)
        self.assertEqual(version_steps[0]["env"]["RELEASE_VERSION"], "${{ inputs.version }}")
        for step in steps:
            if "run" in step:
                self.assertNotRegex(step["run"], r"\$\{\{\s*(?:inputs\.|github\.event\.inputs\.)")
                self.assertNotIn("eval ", step["run"])
                self.assertNotRegex(step["run"], r"git\s+push[^\n]*(?:--force|\s-f(?:\s|$))")


if __name__ == "__main__":
    unittest.main()
