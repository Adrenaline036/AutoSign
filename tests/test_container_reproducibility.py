from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_docker_runtime_and_browser_are_pinned() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    lock = (PROJECT_ROOT / "requirements.docker.lock").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert (
        "docker/dockerfile:1.7@"
        "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
        in dockerfile
    )
    assert (
        "python:3.11.15-slim-bookworm@"
        "sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3"
        in dockerfile
    )
    assert "ARG PLAYWRIGHT_VERSION=1.61.0" in dockerfile
    assert "playwright==1.61.0" in lock
    assert '"playwright==1.61.0"' in pyproject
    assert "playwright>=" not in dockerfile
    assert "playwright>=" not in lock
