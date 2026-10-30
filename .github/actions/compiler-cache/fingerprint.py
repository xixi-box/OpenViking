"""Fingerprint Rust and C++ independently using the build's actual toolchains."""

import hashlib
import json
import os
import subprocess


def probe(command):
    try:
        return subprocess.check_output(command, stderr=subprocess.STDOUT, text=True).strip()
    except FileNotFoundError:
        return f"unavailable: {command[0]}"


def msvc_identity():
    # rustc/cc-rs discover MSVC without cl.exe being on the Bash PATH.
    return probe(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            r"""
            $ErrorActionPreference = 'Stop'
            $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
            $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
            if (!$vs) { throw 'MSVC installation not found' }
            $version = (Get-Content "$vs\VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt").Trim()
            $compiler = "$vs\VC\Tools\MSVC\$version\bin\Hostx64\x64\cl.exe"
            $version
            (Get-FileHash $compiler -Algorithm SHA256).Hash
            $sdk = "${env:ProgramFiles(x86)}\Windows Kits\10\Include"
            if (!(Test-Path $sdk)) { throw 'Windows SDK not found' }
            Get-ChildItem $sdk -Directory | Sort-Object Name | ForEach-Object { $_.Name }
            """,
        ]
    )


def fingerprints(env, rust, c, cxx, cmake, msvc=""):
    def flags(names):
        return {name: env.get(name, "") for name in names}

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    target = env.get("CACHE_TARGET", "host")
    rust_flags = flags(
        ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET", "RUSTC_WRAPPER")
    )
    native_flags = {
        name: value
        for name, value in env.items()
        if name
        in (
            "CC",
            "CXX",
            "CFLAGS",
            "CXXFLAGS",
            "SDKROOT",
            "MACOSX_DEPLOYMENT_TARGET",
            "INCLUDE",
            "LIB",
            "WindowsSDKVersion",
            "VCToolsVersion",
        )
        or (
            name != "CARGO_TARGET_DIR"
            and name.startswith(("CC_", "CXX_", "CFLAGS_", "CXXFLAGS_", "CARGO_TARGET_"))
        )
    }
    # MinGW is used by the Windows engine, not rustc's MSVC-target CLI.
    cli_native = (
        {"msvc": msvc}
        if env.get("RUNNER_OS") == "Windows" and target.endswith("-msvc")
        else {"c": c, "cxx": cxx}
    )
    cli = {"rust": rust, "target": target, "flags": rust_flags, "native": cli_native}
    cli["native_flags"] = native_flags
    ragfs = {
        **cli,
        "native": {"c": c, "cxx": cxx, "cmake": cmake, "msvc": msvc},
        "features": "default:extension-module,s3",
    }
    cpp = {
        "c": c,
        "cxx": cxx,
        "cmake": cmake,
        "flags": native_flags,
        "options": flags(("CMAKE_OSX_ARCHITECTURES", "ARCHFLAGS", "CMAKE_GENERATOR")),
    }
    return {"cli": digest(cli), "ragfs": digest(ragfs), "cpp": digest(cpp)}


def main():
    env = dict(os.environ)
    result = fingerprints(
        env,
        rust=probe(["rustc", "-vV"]),
        c=probe([env.get("CC") or "gcc", "--version"]),
        cxx=probe([env.get("CXX") or "g++", "--version"]),
        cmake=probe(["cmake", "--version"]),
        msvc=msvc_identity() if env.get("RUNNER_OS") == "Windows" else "",
    )
    component = env["CACHE_COMPONENT"]
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"cargo={result[component]}\ncpp={result['cpp']}\n")


if __name__ == "__main__":
    main()
