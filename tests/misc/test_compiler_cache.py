"""Check cache compatibility and trusted-main write boundaries."""

import ast
import os
import runpy
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FINGERPRINT = runpy.run_path(str(ROOT / ".github/actions/compiler-cache/fingerprint.py"))
ACTION = yaml.safe_load((ROOT / ".github/actions/compiler-cache/action.yml").read_text())


def hashes(env=None, **overrides):
    toolchains = {"rust": "rust-1", "c": "gcc-1", "cxx": "g++-1", "cmake": "cmake-1"}
    toolchains.update(overrides)
    return FINGERPRINT["fingerprints"](
        {"RUNNER_OS": "Windows", "CACHE_TARGET": "x86_64-pc-windows-msvc", **(env or {})},
        **toolchains,
        msvc="msvc-1",
    )


@pytest.mark.parametrize("tool", ["c", "cxx", "cmake"])
def test_windows_cli_cache_survives_engine_toolchain_changes(tool):
    before = hashes()
    after = hashes(**{tool: "new-version"})
    assert before["cli"] == after["cli"]
    assert before["ragfs"] != after["ragfs"]
    assert before["cpp"] != after["cpp"]


def test_rust_upgrade_does_not_invalidate_cpp_cache():
    before = hashes()
    after = hashes(rust="rust-2")
    assert before["cpp"] == after["cpp"]
    assert before["cli"] != after["cli"]
    assert before["ragfs"] != after["ragfs"]


def test_rust_caches_preserve_msvc_identity():
    args = {"rust": "rust-1", "c": "gcc-1", "cxx": "g++-1", "cmake": "cmake-1"}
    env = {"RUNNER_OS": "Windows", "CACHE_TARGET": "x86_64-pc-windows-msvc"}
    before = FINGERPRINT["fingerprints"](env, **args, msvc="msvc-1")
    after = FINGERPRINT["fingerprints"](env, **args, msvc="msvc-2")
    assert before["cli"] != after["cli"]
    assert before["ragfs"] != after["ragfs"]
    assert before["cpp"] == after["cpp"]


@pytest.mark.parametrize(
    "env",
    [
        {"RUSTFLAGS": "-C target-cpu=native"},
        {"CARGO_ENCODED_RUSTFLAGS": "-C\x1ftarget-cpu=native"},
        {"CACHE_TARGET": "aarch64-pc-windows-msvc"},
    ],
)
def test_rust_target_and_flags_invalidate_only_rust_caches(env):
    before = hashes()
    after = hashes(env)
    assert before["cli"] != after["cli"]
    assert before["ragfs"] != after["ragfs"]
    assert before["cpp"] == after["cpp"]


def test_native_flags_and_non_windows_compilers_are_not_ignored():
    before = hashes({"RUNNER_OS": "Linux", "CACHE_TARGET": "host"})
    after = hashes({"RUNNER_OS": "Linux", "CACHE_TARGET": "host"}, c="gcc-2")
    assert all(before[key] != after[key] for key in before)
    assert hashes({"CFLAGS": "-O1"})["ragfs"] != hashes()["ragfs"]
    assert hashes({"CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER": "custom"}) != hashes()


def test_output_directory_does_not_change_compatibility():
    assert hashes({"CARGO_TARGET_DIR": "/runner-a/target/cli"}) == hashes(
        {"CARGO_TARGET_DIR": "/runner-b/target/cli"}
    )


@pytest.mark.parametrize(
    "repository,ref,event,writes",
    [
        ("volcengine/OpenViking", "refs/heads/main", "push", True),
        ("volcengine/OpenViking", "refs/heads/main", "workflow_dispatch", True),
        ("volcengine/OpenViking", "refs/heads/main", "pull_request", False),
        ("volcengine/OpenViking", "refs/pull/1/merge", "pull_request", False),
        ("volcengine/OpenViking", "refs/heads/feature", "push", False),
        ("volcengine/OpenViking", "refs/tags/v1.0", "push", False),
        ("other/OpenViking", "refs/heads/main", "push", False),
    ],
)
def test_pr_fork_and_tag_caches_only_restore(repository, ref, event, writes):
    selected = []
    for step in ACTION["runs"]["steps"]:
        if not step.get("uses", "").startswith("actions/cache"):
            continue
        expression = step["if"]
        for name, value in {
            "github.repository": repository,
            "github.ref": ref,
            "github.event_name": event,
            "inputs.cpp": "true",
        }.items():
            expression = expression.replace(name, repr(value))
        expression = expression.replace("&&", " and ").replace("||", " or ")
        if eval(expression, {"__builtins__": {}}):
            selected.append(step["uses"])
    assert selected == ["actions/cache@v5" if writes else "actions/cache/restore@v5"] * 2


def test_components_do_not_restore_overlapping_build_output():
    cache_steps = [
        step for step in ACTION["runs"]["steps"] if step.get("uses", "").startswith("actions/cache")
    ]
    for step in cache_steps[:2]:
        assert "target/${{ inputs.component }}" in step["with"]["path"]
        assert "${{ inputs.component }}" in step["with"]["key"]
        assert "${{ steps.toolchain.outputs.cargo }}" in step["with"]["key"]
    for step in cache_steps[2:]:
        assert step["with"]["path"] == ".cache/ccache"
        assert "${{ steps.toolchain.outputs.cpp }}" in step["with"]["key"]
        assert "outputs.cargo" not in step["with"]["key"]


def test_warmup_reuses_the_real_windows_wheel_without_publishing():
    workflow = yaml.safe_load((ROOT / ".github/workflows/windows-cache-warmup.yml").read_text())
    job = workflow["jobs"]["warm"]
    assert job["uses"] == "./.github/workflows/_build.yml"
    assert job["with"] == {
        "os_json": '["windows-latest"]',
        "python_json": '["3.10"]',
        "build_sdist": False,
        "build_wheels": True,
    }
    assert job["if"] == (
        "github.repository == 'volcengine/OpenViking' && github.ref == 'refs/heads/main'"
    )
    assert set(workflow["jobs"]) == {"warm"}
    # PyYAML's YAML 1.1 loader parses the workflow's `on` key as True.
    triggers = workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers


@pytest.mark.parametrize("component", ["cli", "ragfs"])
def test_setup_build_subprocess_uses_its_component_directory(tmp_path, monkeypatch, component):
    """Run the actual build methods with fake compilers, checking env and output."""
    tree = ast.parse((ROOT / "setup.py").read_text())
    build_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OpenVikingBuildExt"
    )
    method_name = (
        "_build_ov_cli_artifact_impl" if component == "cli" else "build_ragfs_python_artifact"
    )
    method = next(
        node
        for node in build_class.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    namespace = {
        "Path": Path,
        "os": os,
        "sys": sys,
        "shutil": shutil,
        "subprocess": subprocess,
        "importlib": types.SimpleNamespace(util=types.SimpleNamespace(find_spec=lambda name: True)),
        "_sanitize_native_build_env": lambda env: env,
        "resolve_openviking_version": lambda **kwargs: "1.0.0",
        "SETUP_DIR": tmp_path,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), "setup.py", "exec"), namespace)
    target = tmp_path / "target" / component
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "target" / "other"))
    monkeypatch.setenv(f"OV_{component.upper()}_CARGO_TARGET_DIR", str(target))
    monkeypatch.delenv("OV_SKIP_RAGFS_BUILD", raising=False)
    monkeypatch.delenv("OV_PREBUILT_BIN_DIR", raising=False)
    monkeypatch.delenv("CARGO_BUILD_TARGET", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    artifact = tmp_path / "openviking/bin/ov"
    project = tmp_path / ("crates/ov_cli" if component == "cli" else "crates/ragfs-python")
    project.mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        assert kwargs["env"]["CARGO_TARGET_DIR"] == str(target)
        calls.append(command)
        if component == "cli":
            binary = target / "release" / "ov"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"cli")
        else:
            output = Path(command[command.index("--out") + 1])
            with zipfile.ZipFile(output / "ragfs_python-1.0.0.whl", "w") as wheel:
                wheel.writestr("ragfs_python/ragfs_python.abi3.so", b"ragfs")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    build = types.SimpleNamespace(
        _should_require_ragfs_artifact=lambda: True,
        _copy_artifacts_to_build_lib=lambda **kwargs: None,
        _resolve_cargo_target_dir=lambda project, env: Path(env["CARGO_TARGET_DIR"]),
        _require_artifact=lambda path, *args: path.read_bytes(),
        _copy_artifact=lambda source, destination: destination.write_bytes(source.read_bytes()),
    )
    artifact.parent.mkdir(parents=True)
    if component == "cli":
        namespace[method_name](build, project, "ov", artifact)
        assert artifact.read_bytes() == b"cli"
    else:
        namespace[method_name](build)
        assert (tmp_path / "openviking/lib/ragfs_python.abi3.so").read_bytes() == b"ragfs"
    assert len(calls) == 1
