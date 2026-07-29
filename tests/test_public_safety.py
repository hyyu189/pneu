import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "check_public_safety.py"
SPEC = importlib.util.spec_from_file_location("check_public_safety", MODULE_PATH)
SAFETY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SAFETY)


def test_forbidden_path_rejects_runtime_and_backup_paths():
    assert SAFETY.forbidden_path(".roundtable/agents.yaml")
    assert SAFETY.forbidden_path("bin.bak-20260623/tool")
    assert SAFETY.forbidden_path("project/inbox/codex/new/message.md")
    assert SAFETY.forbidden_path("registry/mail/project-uuid/message.md")
    assert SAFETY.forbidden_path("registry/layout-locks/project-uuid.lock")
    assert SAFETY.forbidden_path("bin/__pycache__/tool.pyc")


def test_forbidden_path_allows_product_sources_and_templates():
    assert SAFETY.forbidden_path("bin/rt-say") is None
    assert SAFETY.forbidden_path("templates/roundtable-gitignore.tmpl") is None
    assert SAFETY.forbidden_path("tests/test_rt_tooling.py") is None


def test_scan_text_finds_private_material_without_embedding_it_in_this_file():
    absolute_path = "/" + "Users" + "/developer/private/file"
    private_session = "https://claude.ai/code/" + "session_" + "private"
    secret = "sk-" + ("x" * 20)

    labels = "\n".join(
        SAFETY.scan_text(
            ROOT / "fixture.txt",
            "\n".join((absolute_path, private_session, secret)),
        )
    )

    assert "personal absolute path" in labels
    assert "private Claude session URL" in labels
    assert "OpenAI-style secret" in labels
