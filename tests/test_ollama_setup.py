import os

import pytest

from mountfile import ollama_setup


def test_setup_launcher_requires_windows(monkeypatch):
    monkeypatch.setattr(ollama_setup, "is_windows", lambda: False)
    with pytest.raises(ollama_setup.OllamaSetupError, match="только в Windows"):
        ollama_setup.launch_windows_ollama_setup()


def test_setup_launcher_rejects_shell_metacharacters_before_launch(monkeypatch):
    monkeypatch.setattr(ollama_setup, "is_windows", lambda: True)
    monkeypatch.setattr(
        ollama_setup.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("must reject model input before launch"),
    )
    with pytest.raises(ollama_setup.OllamaSetupError, match="Некорректное имя модели"):
        ollama_setup.launch_windows_ollama_setup("model; Remove-Item C:\\")


def test_setup_launcher_uses_bundled_script_and_passes_model_as_separate_arg(
    monkeypatch,
):
    monkeypatch.setattr(ollama_setup, "is_windows", lambda: True)
    launched = {}

    class Process:
        pass

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(ollama_setup.subprocess, "Popen", fake_popen)
    result = ollama_setup.launch_windows_ollama_setup("llama3.2:3b")

    assert isinstance(result, Process)
    command = launched["command"]
    assert command[0] == "powershell.exe"
    assert command[command.index("-File") + 1].endswith(
        os.path.join("mountfile", "data", "install-ollama.ps1")
    )
    assert command[-2:] == ["-Model", "llama3.2:3b"]
    assert launched["kwargs"]["creationflags"] == getattr(
        ollama_setup.subprocess, "CREATE_NEW_CONSOLE", 0
    )


def test_bundled_script_is_present_and_checks_official_signature():
    path = ollama_setup.windows_ollama_script_path()
    with open(path, encoding="utf-8") as script:
        contents = script.read()
    assert "https://ollama.com/download/OllamaSetup.exe" in contents
    assert "Get-AuthenticodeSignature" in contents
    assert r"Ollama Inc\." in contents
    assert "ollama pull $Model" in contents
