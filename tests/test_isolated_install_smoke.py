from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import isolated_install_smoke


def _add_file(
    archive: tarfile.TarFile,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o644,
) -> None:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _fake_release_archive(path: Path, version: str) -> Path:
    root = f"pneu-{version}"
    install = f"""#!/bin/sh
set -eu
if /usr/bin/env | /usr/bin/grep -E '^(ROUNDTABLE_INSTALL_PREFIX|RT_|PNEU_|CLAUDE_|CODEX_)=' >/dev/null; then
  echo 'ambient pneu environment leaked' >&2
  exit 73
fi
test -x "$ROUNDTABLE_BOOTSTRAP_PYTHON"
mkdir -p "$HOME/.pneu/versions/{version}" "$HOME/.local/bin"
ln -s "versions/{version}" "$HOME/.pneu/current"
printf '%s\n' '#!/bin/sh' 'printf "version: {version}\\ninstall prefix: fixture\\ncurrent target: versions/{version}\\n"' > "$HOME/.local/bin/pneu"
chmod +x "$HOME/.local/bin/pneu"
""".encode()
    with tarfile.open(path, mode="w:gz") as archive:
        _add_file(
            archive,
            f"{root}/BUILD-METADATA.json",
            f'{{"version":"{version}"}}\n'.encode(),
        )
        _add_file(archive, f"{root}/install", install, mode=0o755)
    return path


def _seed_real_prefix(prefix: Path) -> None:
    version = prefix / "versions" / "live"
    version.mkdir(parents=True)
    (prefix / "current").symlink_to(Path("versions") / "live")


def test_fake_archive_smoke_uses_a_strict_environment_allowlist(
    tmp_path,
    monkeypatch,
):
    real_prefix = tmp_path / "real-prefix"
    _seed_real_prefix(real_prefix)
    monkeypatch.setenv("ROUNDTABLE_INSTALL_PREFIX", str(real_prefix))
    monkeypatch.setenv("RT_FAKE_AMBIENT", "must-not-leak")
    monkeypatch.setenv("PNEU_FAKE_AMBIENT", "must-not-leak")
    monkeypatch.setenv("CLAUDE_FAKE_AMBIENT", "must-not-leak")
    monkeypatch.setenv("CODEX_FAKE_AMBIENT", "must-not-leak")
    home = tmp_path / "allowlist-home"
    home.mkdir()

    environment = isolated_install_smoke.hermetic_environment(
        home,
        Path(sys.executable),
    )
    assert frozenset(environment) == isolated_install_smoke.HERMETIC_ENVIRONMENT_KEYS
    assert environment["PATH"].split(os.pathsep) == list(
        dict.fromkeys(
            [
                str(Path(sys.executable).resolve().parent),
                *isolated_install_smoke.SYSTEM_PATH_DIRECTORIES,
            ]
        )
    )
    assert "ROUNDTABLE_INSTALL_PREFIX" not in environment
    assert not any(
        name.startswith(("RT_", "PNEU_", "CLAUDE_", "CODEX_"))
        for name in environment
    )

    version = "9.8.7"
    archive = _fake_release_archive(
        tmp_path / f"pneu-{version}-macos.tar.gz",
        version,
    )
    before = isolated_install_smoke.fingerprint_prefix(real_prefix)
    result = isolated_install_smoke.run_smoke(archive)

    assert result.version == version
    assert result.real_prefix == real_prefix.resolve()
    assert isolated_install_smoke.fingerprint_prefix(real_prefix) == before


def test_real_prefix_fingerprint_rejects_an_escaped_install_mutation(tmp_path):
    real_prefix = tmp_path / "real-prefix"
    _seed_real_prefix(real_prefix)
    before = isolated_install_smoke.fingerprint_prefix(real_prefix)

    (real_prefix / "versions" / "escaped-install").mkdir()

    with pytest.raises(
        isolated_install_smoke.IsolatedInstallSmokeError,
        match="real install prefix changed",
    ):
        isolated_install_smoke.assert_prefix_unchanged(real_prefix, before)
