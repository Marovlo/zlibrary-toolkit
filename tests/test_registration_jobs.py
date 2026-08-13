"""后台注册任务状态的离线自测，不访问真实网络。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp" / "backend"))

from app.registration_jobs import RegistrationJob


def test_public_job_does_not_expose_credentials() -> None:
    job = RegistrationJob(id="job-1", email="test@example.com")
    job.update(message="等待验证码")
    public = job.public()
    assert public["email"] == "test@example.com"
    assert "password" not in public
    assert "code" not in public
    assert public["phase"] == "preparing"
    assert public["message"] == "等待验证码"


if __name__ == "__main__":
    test_public_job_does_not_expose_credentials()
    print("registration jobs 自测通过")
