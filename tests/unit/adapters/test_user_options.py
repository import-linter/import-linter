import pytest

from importlinter.adapters.user_options import (
    IniFileUserOptionReader,
    TomlFileUserOptionReader,
)
from importlinter.application.app_config import settings
from importlinter.application.user_options import UserOptions
from tests.adapters.filesystem import FakeFileSystem


@pytest.mark.parametrize("filename", ("setup.cfg", ".importlinter"))
@pytest.mark.parametrize(
    "contents, expected_options",
    (
        (
            """
            [something]
            # This file has no import-linter section.
            foo = 1
            bar = hello
            """,
            None,
        ),
        (
            """
            [something]
            foo = 1
            bar = hello

            [importlinter]
            foo = hello
            bar = 999
            """,
            UserOptions(session_options={"foo": "hello", "bar": "999"}, contracts_options=[]),
        ),
        (
            """
            [importlinter]
            foo = hello

            [importlinter:contract:contract-one]
            name=Contract One
            key=value
            multiple_values=
                one
                two
                three
                foo.one -> foo.two

            [importlinter:contract:contract-two];
            name=Contract Two
            baz=3
            """,
            UserOptions(
                session_options={"foo": "hello"},
                contracts_options=[
                    {
                        "name": "Contract One",
                        "id": "contract-one",
                        "key": "value",
                        "multiple_values": [
                            "one",
                            "two",
                            "three",
                            "foo.one -> foo.two",
                        ],
                    },
                    {"name": "Contract Two", "id": "contract-two", "baz": "3"},
                ],
            ),
        ),
    ),
)
def test_ini_file_reader(filename, contents, expected_options):
    settings.configure(
        FILE_SYSTEM=FakeFileSystem(
            content_map={f"/path/to/folder/{filename}": contents},
            working_directory="/path/to/folder",
        )
    )

    options = IniFileUserOptionReader().read_options()

    assert expected_options == options


@pytest.mark.parametrize(
    "passed_filename, expected_foo_value",
    (
        (None, "green"),
        ("custom.ini", "blue"),
        ("deeper/custom.ini", "purple"),
        ("nonexistent.ini", FileNotFoundError()),
    ),
)
def test_respects_passed_filename(passed_filename, expected_foo_value):
    settings.configure(
        FILE_SYSTEM=FakeFileSystem(
            content_map={
                "/path/to/folder/.importlinter": """
                        [importlinter]
                        foo = green
                    """,
                "/path/to/folder/custom.ini": """
                        [importlinter]
                        foo = blue
                    """,
                "/path/to/folder/deeper/custom.ini": """
                        [importlinter]
                        foo = purple
                    """,
            },
            working_directory="/path/to/folder",
        )
    )
    expected_options = UserOptions(
        session_options={"foo": expected_foo_value}, contracts_options=[]
    )

    reader = IniFileUserOptionReader()

    if isinstance(expected_foo_value, Exception):
        with pytest.raises(
            expected_foo_value.__class__, match=f"Could not find {passed_filename}."
        ):
            reader.read_options(config_filename=passed_filename)
    else:
        options = reader.read_options(config_filename=passed_filename)
        assert expected_options == options


@pytest.mark.parametrize(
    "contents, expected_options",
    (
        (
            """
            [something]
            # This file has no import-linter section.
            foo = 1
            bar = "hello"
            """,
            None,
        ),
        (
            """
            # Flat keys in pyproject.toml should be ignored without [tool.importlinter].
            root_package = "mypackage"
            """,
            None,
        ),
        (
            """
            [something]
            foo = 1
            bar = "hello"

            [tool.importlinter]
            foo = "hello"
            bar = 999
            """,
            UserOptions(session_options={"foo": "hello", "bar": 999}, contracts_options=[]),
        ),
        (
            """
            [tool.importlinter]
            foo = "hello"
            include_external_packages = true
            exclude_type_checking_imports = true

            [[tool.importlinter.contracts]]
            id = "contract-one"
            name = "Contract One"
            key = "value"
            multiple_values = [
                "one",
                "two",
                "three",
                "foo.one -> foo.two",
            ]

            [[tool.importlinter.contracts]]
            name = "Contract Two"
            baz = 3
            """,
            UserOptions(
                session_options={
                    "foo": "hello",
                    "include_external_packages": "True",
                    "exclude_type_checking_imports": "True",
                },
                contracts_options=[
                    {
                        "name": "Contract One",
                        "id": "contract-one",
                        "key": "value",
                        "multiple_values": [
                            "one",
                            "two",
                            "three",
                            "foo.one -> foo.two",
                        ],
                    },
                    {"name": "Contract Two", "baz": 3},
                ],
            ),
        ),
    ),
)
def test_toml_file_reader(contents, expected_options):
    settings.configure(
        FILE_SYSTEM=FakeFileSystem(
            content_map={"/path/to/folder/pyproject.toml": contents},
            working_directory="/path/to/folder",
        )
    )

    options = TomlFileUserOptionReader().read_options()
    assert expected_options == options


@pytest.mark.parametrize("filename", (".importlinter.toml", "importlinter.toml"))
@pytest.mark.parametrize(
    "contents, expected_options",
    (
        ("# Empty TOML file.", None),
        (
            """
            [tool.importlinter]
            """,
            None,
        ),
        (
            """
            foo = "hello"
            include_external_packages = true
            exclude_type_checking_imports = false

            [[contracts]]
            id = "contract-one"
            name = "Contract One"
            key = "value"
            as_packages = false
            multiple_values = [
                "one",
                "two",
                "three",
                "foo.one -> foo.two",
            ]

            [[contracts]]
            name = "Contract Two"
            baz = 3
            """,
            UserOptions(
                session_options={
                    "foo": "hello",
                    "include_external_packages": "True",
                    "exclude_type_checking_imports": "False",
                },
                contracts_options=[
                    {
                        "name": "Contract One",
                        "id": "contract-one",
                        "key": "value",
                        "as_packages": "False",
                        "multiple_values": [
                            "one",
                            "two",
                            "three",
                            "foo.one -> foo.two",
                        ],
                    },
                    {"name": "Contract Two", "baz": 3},
                ],
            ),
        ),
        (
            """
            [tool.importlinter]
            foo = "hello"
            include_external_packages = true

            [[tool.importlinter.contracts]]
            name = "Contract One"
            baz = 3
            """,
            UserOptions(
                session_options={
                    "foo": "hello",
                    "include_external_packages": "True",
                },
                contracts_options=[
                    {"name": "Contract One", "baz": 3},
                ],
            ),
        ),
    ),
)
def test_dedicated_toml_file_reader(filename, contents, expected_options):
    settings.configure(
        FILE_SYSTEM=FakeFileSystem(
            content_map={f"/path/to/folder/{filename}": contents},
            working_directory="/path/to/folder",
        )
    )

    options = TomlFileUserOptionReader().read_options()
    assert expected_options == options


@pytest.mark.parametrize("filename", (".importlinter.toml", "importlinter.toml"))
def test_toml_reader_falls_back_from_pyproject_without_importlinter_section(filename):
    settings.configure(
        FILE_SYSTEM=FakeFileSystem(
            content_map={
                "/path/to/folder/pyproject.toml": """
                    [project]
                    name = "mypackage"
                """,
                f"/path/to/folder/{filename}": """
                    root_package = "mypackage"
                """,
            },
            working_directory="/path/to/folder",
        )
    )

    options = TomlFileUserOptionReader().read_options()
    assert options == UserOptions(
        session_options={"root_package": "mypackage"},
        contracts_options=[],
    )


@pytest.mark.parametrize(
    "passed_filename, expected_foo_value",
    (
        ("flat.toml", "blue"),
        ("nested.toml", "purple"),
        ("nonexistent.toml", FileNotFoundError()),
    ),
)
def test_toml_reader_respects_passed_filename(passed_filename, expected_foo_value):
    settings.configure(
        FILE_SYSTEM=FakeFileSystem(
            content_map={
                "/path/to/folder/flat.toml": """
                    foo = "blue"
                """,
                "/path/to/folder/nested.toml": """
                    [tool.importlinter]
                    foo = "purple"
                """,
            },
            working_directory="/path/to/folder",
        )
    )
    reader = TomlFileUserOptionReader()

    if isinstance(expected_foo_value, Exception):
        with pytest.raises(
            expected_foo_value.__class__, match=f"Could not find {passed_filename}."
        ):
            reader.read_options(config_filename=passed_filename)
    else:
        options = reader.read_options(config_filename=passed_filename)
        assert options == UserOptions(
            session_options={"foo": expected_foo_value},
            contracts_options=[],
        )
