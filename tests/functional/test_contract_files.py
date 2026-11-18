"""Real-file coverage of configuration composition and source identity."""

import pytest

from importlinter import api, configuration
from importlinter.application import use_cases
from importlinter.application.user_options import InvalidUserOptions


CONTRACT = """
[[tool.importlinter.contracts]]
id = "boundary"
name = "Domain boundary"
type = "forbidden"
source_modules = ["importlinter.domain"]
forbidden_modules = ["importlinter.ui"]
"""
INI_CONTRACT = """
[importlinter:contract:boundary]
name = Domain boundary
type = forbidden
source_modules = importlinter.domain
forbidden_modules = importlinter.ui
"""


@pytest.fixture(autouse=True)
def real_configuration():
    configuration.configure()
    yield
    configuration.configure()


def parent(tmp_path, files='["rules.toml"]'):
    path = tmp_path / "parent.toml"
    path.write_text(
        '[tool.importlinter]\nroot_package = "importlinter"\ncontract_files = ' + files
    )
    return str(path)


@pytest.mark.parametrize("extension, contents", [("toml", CONTRACT), ("ini", INI_CONTRACT)])
def test_composition_api_and_lint(tmp_path, monkeypatch, extension, contents):
    (tmp_path / f"rules.{extension}").write_text(contents)
    filename = parent(tmp_path, f'["rules.{extension}", "rules.{extension}"]')
    monkeypatch.chdir(tmp_path.parent)
    result = api.read_configuration(filename)
    assert result["session_options"] == {"root_packages": ["importlinter"]}
    assert [c["id"] for c in result["contracts_options"]] == ["boundary"]
    assert use_cases.lint_imports(
        filename,
        no_logo=True,
        cache_dir=None,
        limit_to_contracts=(f"{tmp_path / f'rules.{extension}'}:boundary",),
        is_debug_mode=True,
    )


def test_ini_parent_and_default_discovery(tmp_path, monkeypatch):
    (tmp_path / ".importlinter").write_text(
        "[importlinter]\nroot_package = importlinter\ncontract_files =\n    rules.toml\n"
    )
    (tmp_path / "rules.toml").write_text(CONTRACT)
    monkeypatch.chdir(tmp_path)
    assert len(api.read_configuration()["contracts_options"]) == 1


@pytest.mark.parametrize(
    "contents",
    [
        "",
        '[tool.importlinter]\nroot_package = "other"\n' + CONTRACT,
        '[tool.importlinter]\ncontract_files = ["nested.toml"]\n' + CONTRACT,
        '[tool.importlinter]\ncontracts = "invalid"',
    ],
)
def test_invalid_fragment_names_file(tmp_path, contents):
    (tmp_path / "rules.toml").write_text(contents)
    with pytest.raises(InvalidUserOptions, match="rules.toml"):
        api.read_configuration(parent(tmp_path))


def test_missing_fragment(tmp_path):
    with pytest.raises(InvalidUserOptions, match="rules.toml"):
        api.read_configuration(parent(tmp_path))


@pytest.mark.parametrize("reverse", [False, True])
def test_fragment_cannot_also_be_independent(tmp_path, reverse):
    rule = tmp_path / "rules.toml"
    rule.write_text(CONTRACT)
    paths = [parent(tmp_path), str(rule)]
    if reverse:
        paths.reverse()
    with pytest.raises(InvalidUserOptions, match="independent"):
        use_cases.lint_imports(config_filenames=paths, is_debug_mode=True)


def test_fragment_cannot_have_two_parents(tmp_path):
    (tmp_path / "rules.toml").write_text(CONTRACT)
    first = parent(tmp_path)
    second = tmp_path / "second.toml"
    second.write_text((tmp_path / "parent.toml").read_text())
    with pytest.raises(InvalidUserOptions, match="multiple parents"):
        use_cases.lint_imports(config_filenames=[first, str(second)], is_debug_mode=True)


def test_duplicate_ids_remain_per_file(tmp_path):
    (tmp_path / "rules.toml").write_text(CONTRACT)
    (tmp_path / "second.toml").write_text(CONTRACT)
    filename = parent(tmp_path, '["rules.toml", "second.toml"]')
    assert len(api.read_configuration(filename)["contracts_options"]) == 2
    with pytest.raises(ValueError, match="Multiple contracts"):
        use_cases.lint_imports(filename, limit_to_contracts=("boundary",), is_debug_mode=True)


def test_malformed_reference_list(tmp_path):
    with pytest.raises(InvalidUserOptions, match="contract_files"):
        api.read_configuration(parent(tmp_path, '"rules.toml"'))


def test_inheritance_graph_sharing_and_inline_order(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from importlinter.application.app_config import settings

    (tmp_path / "rules.toml").write_text(CONTRACT.replace('"forbidden"', '"custom"'))
    filename = parent(tmp_path)
    with open(filename, "a") as stream:
        stream.write(
            "\ninclude_external_packages = true\n"
            'contract_types = ["custom: importlinter.contracts.forbidden.ForbiddenContract"]\n'
            + CONTRACT.replace('"boundary"', '"inline"')
        )
    builder = Mock(wraps=settings.GRAPH_BUILDER.build)
    monkeypatch.setattr(settings.GRAPH_BUILDER, "build", builder)
    assert use_cases.lint_imports(filename, cache_dir=None, no_logo=True, is_debug_mode=True)
    assert builder.call_count == 1
    assert builder.call_args.kwargs["include_external_packages"] is True
    options = api.read_configuration(filename)
    assert [c["id"] for c in options["contracts_options"]] == ["inline", "boundary"]


def test_symlink_reference_is_deduplicated(tmp_path):
    (tmp_path / "rules.toml").write_text(CONTRACT)
    try:
        (tmp_path / "alias.toml").symlink_to(tmp_path / "rules.toml")
    except OSError:
        pytest.skip("Symlinks unavailable")
    filename = parent(tmp_path, '["rules.toml", "alias.toml"]')
    assert len(api.read_configuration(filename)["contracts_options"]) == 1


def test_duplicate_ids_in_fragment_fail_before_selection(tmp_path):
    (tmp_path / "rules.toml").write_text(CONTRACT + CONTRACT)
    with pytest.raises(InvalidUserOptions, match="unique within a file"):
        api.read_configuration(parent(tmp_path))


def test_unrelated_tables_are_ignored(tmp_path):
    (tmp_path / "rules.toml").write_text("[tool.other]\nvalue = true\n" + CONTRACT)
    assert len(api.read_configuration(parent(tmp_path))["contracts_options"]) == 1
