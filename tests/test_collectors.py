from __future__ import annotations

from envforge import collectors
from envforge.collectors import (
    capture_payload,
    collect_env,
    collect_path,
    collect_projects,
    collect_runtimes,
    collect_terminals,
    filter_developer_software,
    normalize_software_id,
    scan_software,
)


class Provider:
    def __init__(self, scope, values):
        self.scope = scope
        self.values = dict(values)

    def list_vars(self):
        return dict(self.values)

    def get(self, name):
        return self.values.get(name)

    def write(self, updates):  # pragma: no cover - 采集不调用写
        raise AssertionError("只读采集不应写入")


def test_collect_env_masks_sensitive_values():
    providers = {
        "user": Provider("user", {"PATH": r"C:\A;C:\B", "API_KEY": "abc", "EDITOR": "vim"}),
        "system": Provider("system", {"PATH": r"C:\Windows"}),
    }
    entries, warnings = collect_env(("user", "system"), providers)
    assert warnings == []
    by_key = {item["key"]: item for item in entries}
    assert by_key["API_KEY"]["secret"] is True
    assert by_key["API_KEY"]["value"] is None
    assert by_key["EDITOR"]["value"] == "vim"
    assert by_key["PATH"]["scope"] in ("user", "system")


def test_collect_env_treats_url_credentials_as_secret():
    providers = {
        "user": Provider(
            "user",
            {
                "SERVICE_URL": "https://user:pass@example.com",
                "EDITOR": "vim",
            },
        )
    }
    entries, warnings = collect_env(("user",), providers)
    assert warnings == []
    by_key = {item["key"]: item for item in entries}
    assert by_key["SERVICE_URL"]["secret"] is True
    assert by_key["SERVICE_URL"]["value"] is None
    assert by_key["EDITOR"]["value"] == "vim"


def test_collect_env_reports_unreadable_scope():
    class BrokenProvider:
        def list_vars(self):
            raise OSError("no registry")

    entries, warnings = collect_env(("system",), {"system": BrokenProvider()})
    assert entries == []
    assert warnings and "system" in warnings[0]


def test_collect_path_splits_entries():
    entries = [
        {"scope": "user", "key": "PATH", "value": r'C:\A;"C:\B"'},
        {"scope": "user", "key": "EDITOR", "value": "vim"},
    ]
    blocks = collect_path(entries)
    assert blocks == [{"scope": "user", "entries": [r"C:\A", r"C:\B"]}]


def test_collect_runtimes_reads_database(db):
    db.execute(
        "INSERT INTO runtimes(kind, version, exe_path, source, notes, created_at)"
        " VALUES('python','3.12.3','C:\\Python\\python.exe','detected','','t')"
    )
    items = collect_runtimes(db)
    assert items[0]["kind"] == "python" and items[0]["version"] == "3.12.3"
    assert collect_runtimes(None) == []


def test_filter_developer_software_keywords_and_dedupe():
    raw = [
        {"name": "Python 3.12", "version": "3.12.3", "publisher": "PSF"},
        {"name": "python 3.12", "version": "3.12.4", "publisher": "PSF"},
        {"name": "Git", "version": "2.44", "publisher": "Git"},
        {"name": "Mozilla Firefox", "version": "128", "publisher": "Mozilla"},
    ]
    items = filter_developer_software(raw)
    names = [item["name"] for item in items]
    assert names == ["Git", "Python 3.12"]
    assert all(item["source"] == "registry" for item in items)


def test_scan_software_uses_injected_reader():
    items = scan_software(reader=lambda: [{"name": "Node.js", "version": "22.1"}])
    assert items[0]["name"] == "Node.js"
    assert scan_software(reader=lambda: []) == []


def test_scan_software_survives_reader_failure():
    def boom():
        raise RuntimeError("registry unavailable")

    assert scan_software(reader=boom) == []


def test_collect_terminals_with_injected_environment(tmp_path):
    profile = tmp_path / "Documents" / "PowerShell" / "profile.ps1"
    profile.parent.mkdir(parents=True)
    profile.write_text("# demo", encoding="utf-8")
    which = lambda name: {"pwsh": r"C:\Program Files\PowerShell\pwsh.exe", "bash": None}.get(name)
    result = collect_terminals(which=which, home=tmp_path, exists=lambda path: path == str(profile))
    assert result["powershell"]["pwsh"].endswith("pwsh.exe")
    profiles = result["powershell"]["profiles"]
    assert any(item["exists"] for item in profiles)
    assert result["git_bash"]["bash"] == ""


def test_collect_terminals_includes_host_profiles(tmp_path):
    result = collect_terminals(home=tmp_path, exists=lambda path: False)
    paths = [item["path"] for item in result["powershell"]["profiles"]]
    assert any(path.endswith("Microsoft.PowerShell_profile.ps1") for path in paths)


def test_collect_projects_survives_partial_query_failure():
    class BrokenDB:
        def __init__(self):
            self.calls = 0

        def query(self, sql, params=()):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("profiles 表不可读")
            return [{"id": 1, "name": "demo", "path": "x"}]

    assert collect_projects(BrokenDB()) == []


def test_capture_payload_composition(db):
    providers = {
        "user": Provider("user", {"PATH": r"C:\Python312", "JAVA_HOME": r"C:\Java", "TOKEN": "x"}),
        "system": Provider("system", {"PATH": r"C:\Windows"}),
    }
    payload = capture_payload(
        db=db,
        scopes=("user", "system"),
        providers=providers,
        include_software=True,
        soft_reader=lambda: [{"name": "Git", "version": "2.44"}],
        include_projects=True,
    )
    assert payload["machine"]["os"]
    assert payload["path"]
    assert payload["software"][0]["name"] == "Git"
    assert any(item["key"] == "TOKEN" and item["secret"] for item in payload["env"])
    assert payload["projects"] == []
    assert isinstance(payload["warnings"], list)


def test_normalize_software_id():
    assert normalize_software_id("Visual Studio Code") == "visual-studio-code"
    assert normalize_software_id("Node.js") == "node.js"


def test_current_machine_shape():
    machine = collectors.current_machine()
    assert {"os", "arch", "hostname", "python"} <= set(machine)
